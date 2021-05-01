import json
import os
import boto3
import logging
import commands, utils


def lambda_handler(event, context):

    # Check for LOGLEVEL from env, and default to WARNING for production.
    LOGLEVEL = os.environ.get('LOGLEVEL', 'WARNING').upper()
    logging.getLogger().setLevel(LOGLEVEL)

    region = os.environ['AWS_REGION']
    pinpoint_appid = os.environ['PINPOINT_APPID']
    dynamodb_idempotencytable = os.environ['DYNAMODB_IDEMPOTENCYTABLE']
    dynamodb_gamestatetable = os.environ['DYNAMODB_GAMESTATETABLE']
    dynamodb_nicknametable = os.environ['DYNAMODB_NICKNAMETABLE']
    sqs_incomingmessagequeue = os.environ['SQS_INCOMINGMESSAGEQUEUE']

    dynamodb = boto3.resource('dynamodb')
    sqs = boto3.client('sqs')

    pinpoint_client = boto3.client('pinpoint', region_name=region)

    idempotency_table = dynamodb.Table(dynamodb_idempotencytable)
    gamestate_table = dynamodb.Table(dynamodb_gamestatetable)
    nickname_table = dynamodb.Table(dynamodb_nicknametable)

    idempotency_skips = 0 # Number of records not processed due to messageId being in Idempotency Table
    failed_messages = 0 # Number of messages which failed to process. If ultimately nonzero, an exception will be raised.
    for record in event['Records']:

        messageId = record['messageId']

        # We scope these above the try, as we'll need them in the finally for lock-clearing
        lock_uuid = None
        user_number = None
        try:
            # insertIdempotencyRecord() returns False if it failed to insert a record, because of an existing (and not expired) record
            # (In the event of another failure, it raises an Exception)
            if not utils.insertIdempotencyRecord(idempotency_table, messageId):
                idempotency_skips += 1
                continue

            # NOTE: Behavior here seems inconsistent. Examples suggest event record body is a dict, but in-practice it seems to be string.
            record_body = record['body']
            if isinstance(record['body'], str):
                record_body = json.loads(record_body)

            message = record_body['Message']
            message = json.loads(message)

            message_content = message['messageBody']
            user_number = message['originationNumber']
            outgoing_number = message['destinationNumber']

            lock_uuid = utils.lockUsersGameState(gamestate_table, user_number)
            if lock_uuid is None:
                err = "Failed to lock '{}'".format(user_number)
                logging.error(err)
                raise RuntimeError(err)
            else:
                logging.info("Successfully acquired lock '{}' on requestor ('{}')".format(lock_uuid, user_number))

            result = routeRequest(gamestate_table, nickname_table, user_number, message_content)
            utils.sendResultToRequestor_SMS(user_number, result.message, pinpoint_client, pinpoint_appid, outgoing_number)

            if result.other_user_number is not None and result.other_user_message is not None:
                utils.sendResultToRequestor_SMS(result.other_user_number, result.other_user_message, pinpoint_client, pinpoint_appid, outgoing_number)

        except Exception as e:
            logging.error("Failed to process messageId '{}'".format(messageId), exc_info=True)
            failed_messages += 1
            # Remove the idempotency record, so another execution may (re)try without waiting out the record expiration
            utils.deleteIdempotencyRecord(idempotency_table, messageId)

        else:
            # NOTE: Messages which are not deleted (due to an Exception) will remain in the queue and be retried
            # after the lambda returns a RuntimeError (due to the failed message(s))
            # This scheme allows a lambda to _partially_ fail a batch.
            sqs.delete_message(
                QueueUrl=sqs_incomingmessagequeue,
                ReceiptHandle=record['receiptHandle']
            )

        finally:
            if lock_uuid:
                unlocked = utils.unlockUsersGameState(gamestate_table, user_number, lock_uuid)
                if not unlocked:
                    err = "Failed to unlock '{}'".format(user_number)
                    logging.error(err)
                    raise RuntimeError(err)
                else:
                    logging.info("Successfully cleared lock '{}' on requestor ('{}')".format(lock_uuid, user_number))


    if failed_messages:
        raise RuntimeError("Failed to process {} of {} messages.".format(failed_messages, len(event['Records'])))

    # NOTE: We raise an exception here so that skipped messages are retried (if they weren't processed (and therefore
    # deleted) by another lambda execution). This ensures our successful return doesn't mark those messages "processed"
    # because we skipped them while the instance who set the idempotency record actually failed!
    elif idempotency_skips:
        raise RuntimeError("Skipped {} messages with idempotency records.".format(idempotency_skips))

    else:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Processed {} messages.".format(len(event['Records']))
            }),
        }


def routeRequest(gamestate_table, nickname_table, requestor_number, message):
    """
    Attempt to parse and route message from requestor
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param requestor_number: E.164 phone number of user
    @param message: Message to be parsed and routed
    """
    split = message.split(None, 1)
    command = split[0].lower()
    params = None
    if len(split) == 2: # Handle 'help'-like case(s) (no split)
        params = split[1]

    if command == 'nick' or command == 'n':
        return commands.setNick(nickname_table, gamestate_table, requestor_number, params)

    elif command == 'throw' or command == 't' or command == 'play' or command == 'p':
        return commands.throw(nickname_table, gamestate_table, requestor_number, params)

    elif command == 'quit' or command == 'stop':
        return commands.quitGame(nickname_table, gamestate_table, requestor_number)

    elif command == 'help' or command == '?':
        return commands.helpDoc(params)

    else:
        return commands.unknownCommand(gamestate_table, requestor_number, message)
