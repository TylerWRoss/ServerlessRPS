import logging
import re
import time
import uuid
from botocore.exceptions import ClientError

def insertIdempotencyRecord(table, messageId, expires_in_sec=10):
    """
    Attempt to insert an idempotency record, expiring in 'expires_in_sec' seconds, for UUID 'messageId' into given Boto3 DynamoDB Table Resource 'table'.
    Implicitly replaces records with expired TTL timestamps.
    @param table: Boto3 DynamoDB Table Resource
    @param messageId: UUID of message/event
    @param expires_in_sec: Seconds from current unix epoch timestamp to expire this idempotency record (default 5 seconds)
    @rtype: Boolean: True if a record was successfully added. False if an idempotence record already exists.
    """

    CurrentEpochTimestamp = int(time.time())

    try:
        table.put_item(
            Item={
                'messageId': messageId,
                'TTLEpochTimestamp': CurrentEpochTimestamp + expires_in_sec
            },
            ConditionExpression="attribute_not_exists(messageId) OR (attribute_exists(TTLEpochTimestamp) AND TTLEpochTimestamp < :CurrentEpochTimestamp)",
            ExpressionAttributeValues={':CurrentEpochTimestamp': CurrentEpochTimestamp}
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':  # ConditionalCheckFailedException => Idempotence Record Exists
            return False
        else:
            raise e

    return True


def deleteIdempotencyRecord(table, messageId):
    """
    Remove the idempotency record for UUID 'messageId' in the given Boto3 DynamoDB Table Resource 'table'.
    @param table: Boto3 DynamoDB Table Resource
    @param messageId: UUID of message/event
    """
    table.delete_item(
        Key={
            'messageId': messageId,
        }
    )


def userExistsInGameStateTable(gamestate_table, user_number):
    """
    Check the given phone number has a record in the GameState table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param user_number: UUID of message/event
    @return: Boolean existence of user GameState record
    """
    resp = gamestate_table.get_item(
        Key={
            'phone_number': user_number,
        },
        ConsistentRead=True
    )

    return 'Item' in resp


def nicknameExists(nickname_table, nickname):
    """
    Check if the given nickname exists in the Nickname Table
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param nickname: Nickname to query for
    @return: Boolean existence of nickname record
    """
    resp = nickname_table.get_item(
        Key={
            'nickname': nickname.lower(),
        },
        ConsistentRead=True
    )

    return 'Item' in resp


def lockUsersGameState(gamestate_table, user_number, lock_attribute='user_locked', expires_in_sec=10):
    """
    Test-and-set a lock attribute (with UUID and expiration timestamp (unix epoch)) on the given user in the GameState Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param user_number: E.164 phone number of user
    @param lock_attribute: Name of lock attribute (default 'user_locked')
    @param expires_in_sec: Seconds from current unix epoch timestamp to consider this lock expired (default 10 seconds)
    @return: Returns UUID string of lock iff lock was successfully acquired. Returns None on failure (i.e., user already locked)
    """

    lock_uuid = uuid.uuid1().hex

    lock = {
        'lock_uuid': lock_uuid,
        'expiration_epoch_timestamp': int(time.time() + expires_in_sec)
    }

    try:
        response = gamestate_table.update_item(
            Key={'phone_number': user_number},
            UpdateExpression="set {} = :lock_dict".format(lock_attribute),
            ConditionExpression="attribute_not_exists({})".format(lock_attribute),
            ExpressionAttributeValues={':lock_dict': lock}
        )

    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException': # ConditionalCheckFailedException => Lock Exists
            return None
        else:
            raise e

    return lock_uuid


def unlockUsersGameState(gamestate_table, user_number, lock_uuid, lock_attribute='user_locked'):
    """
    Release lock (with specified UUID) on given user in the GameState Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param user_number: E.164 phone number of user
    @param lock_uuid: UUID of lock to be removed.
    @param lock_attribute: Name of lock attribute (default 'user_locked')
    @return: Returns True iff lock with given UUID was successfully removed. Otherwise, returns False
    """

    # This handles the case where we've just deleted the user (invalidating the lock we held)
    if not userExistsInGameStateTable(gamestate_table, user_number):
        return True

    try:
        response = gamestate_table.update_item(
            Key={'phone_number': user_number},
            UpdateExpression="remove {}".format(lock_attribute),
            ConditionExpression="{}.lock_uuid = :lock_uuid".format(lock_attribute),
            ExpressionAttributeValues={':lock_uuid': lock_uuid}
        )

    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':  # ConditionalCheckFailedException => Lock Exists
            return False
        else:
            raise e

    return True


def updateUserGameState(gamestate_table, user_number, games_dict):
    """
    Update the given user's games dict in the GameState table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param user_number: E.164 phone number of user
    @param games_dict: Pending games dict (keyed by nickname of other player and valued by 'rock', 'paper' or 'scissors'
    """
    response = gamestate_table.update_item(
        Key={'phone_number': user_number},
        UpdateExpression="set games = :games_dict",
        ExpressionAttributeValues={':games_dict': games_dict}
    )


def sendResultToRequestor_SMS(destination_number, message, pinpoint_client, pinpoint_appid, origination_number, debug=False):
    """
    Send message string over SMS using AWS Pinpoint
    @param destination_number: E.164 phone number to receive SMS
    @param message: Message string to be sent
    @param pinpoint_client: Boto3 Pinpoint Client instance
    @param pinpoint_appid: AWS Pinpoint AppId to be used for outgoing SMS
    @param origination_number: E.164 origination phone number for outgoing SMS
    @param debug: Boolean debug flag to prevent sending of SMS messages.
    """
    if not debug:
        response = pinpoint_client.send_messages(
            ApplicationId=pinpoint_appid,
            MessageRequest={
                'Addresses': {
                    destination_number: {
                        'ChannelType': 'SMS'
                    }
                },
                'MessageConfiguration': {
                    'SMSMessage': {
                        'Body': message,
                        'MessageType': 'TRANSACTIONAL',
                        'OriginationNumber': origination_number
                    }
                }
            }
        )
    else:
        logging.info("Would have sent SMS message '{}' to '{}' via Pinpoint AppId '{}' using phone number '{}'".format(message, destination_number, pinpoint_appid, origination_number))


def deleteUser(nickname_table, gamestate_table, user_number):
    """
    Delete user and de-register their nickname
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param user_number: E.164 phone number of user
    """

    user_gamestate = getUserGameState(gamestate_table, user_number)

    if user_gamestate is None:
        raise RuntimeError("No gamestate record for phone number '{}'".format(user_number))

    gamestate_table.delete_item(
        Key={
            'phone_number': user_number,
        }
    )

    if 'nickname' in user_gamestate.keys():
        nickname = user_gamestate['nickname']

        nickname_table.delete_item(
            Key={
                'nickname': nickname.lower(),
            }
        )


def setUserNickname(nickname_table, gamestate_table, user_number, nickname):
    """
    Set player nickname. Raises RuntimeError if nickname is taken, or ValueError if it is invalid.
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param user_number: E.164 phone number of user
    @param nickname: String nickname to be set
    """

    if not re.match(r"^\w+$", nickname):
        raise ValueError("Nickname '{}' is invalid")

    # NOTE: We key the table by the lowercase nickname (for uniqueness), and store the original as 'display_name'
    nickname_lowercase = nickname.lower()

    try:
        nickname_table.put_item(
            Item={'nickname': nickname_lowercase, 'phone_number': user_number, 'display_name': nickname},
            ConditionExpression="attribute_not_exists(nickname)"
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':  # ConditionalCheckFailedException => Duplicate Nickname
            raise RuntimeError("Nickname '{}' is taken".format(nickname))
        else:
            raise e

    # Denormalize nickname onto gamestate record for efficient phone_number -> nickname lookups (without an extra index on nickname table)
    gamestate_table.update_item(
        Key={'phone_number': user_number},
        UpdateExpression="SET nickname = :nickname, display_name = :display_name",
        ExpressionAttributeValues={
            ':nickname': nickname_lowercase,
            ':display_name': nickname
        }
    )


def getNicknameRecord(nickname_table, nickname):
    """
    Get a nickname record
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param nickname: String nickname to be get
    @return: Returns record dict if found, otherwise None
    """

    resp = nickname_table.get_item(
        Key={
            'nickname': nickname.lower(),
        },
        ConsistentRead=True
    )

    if 'Item' in resp:
        return resp['Item']
    else:
        return None


def getUserGameState(gamestate_table, user_number):
    """
    Get player's GameState record
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param user_number: E.164 phone number of user
    @return: Returns record dict if found, otherwise None
    """

    resp = gamestate_table.get_item(
        Key={
            'phone_number': user_number,
        },
        ConsistentRead=True
    )

    if 'Item' in resp:
        return resp['Item']
    else:
        return None


def getUserGameStateByNickname(nickname_table, gamestate_table, nickname):
    """
    Get player's GameState record, using their nickname
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param nickname: User nickname to query
    @return: Returns record dict if found, otherwise None
    """

    nick_record = getNicknameRecord(nickname_table, nickname)

    if nick_record is None:
        return None

    resp = gamestate_table.get_item(
        Key={
            'phone_number': nick_record['phone_number'],
        },
        ConsistentRead=True
    )

    if 'Item' in resp:
        return resp['Item']
    else:
        return None


def removeAbandonedGames(nickname_table, games):
    """
    Checks games dict against the nickname table and removes entries for which nickname is not registered (user quit)
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param games: Dict of in-progress/pending games (keyed by other player nickname)
    @return: Returns dict sans games for which other player nickname does not exist
    """
    games_cleaned = games.copy()

    for nickname in games.keys():
        if not nicknameExists(nickname_table, nickname):
            games_cleaned.pop(nickname)

    return games_cleaned


def getRockPaperScissorsPlayFromLeftSubstring(play):
    """
    Determine if the play is one of rock, paper, scissors (or any substring from the left/start)
    @param play: One of 'rock', 'paper', or 'scissors' (or any substring from the left/start)
    @return: Returns one of 'rock', 'paper', or 'scissors' if matched, otherwise returns None
    """
    if "rock".startswith(play.lower()):
        return 'rock'
    elif "paper".startswith(play.lower()):
        return 'paper'
    elif "scissors".startswith(play.lower()):
        return 'scissors'
    else:
        return None



def isPlayerWinner(play, other_player_play):
    """
    Determine the winner of rock, paper, scissors
    @param play: One of 'rock', 'paper', or 'scissors' (or any substring from the left/start)
    @param other_player_play: One of 'rock', 'paper', or 'scissors' (or any substring from the left/start)
    @return: Returns True if 'play' beats 'other_player_play', False if 'other_player_play' beats 'play', and None if tie.
                Raises ValueError on bad input
    """

    play_str = getRockPaperScissorsPlayFromLeftSubstring(play)
    other_player_play_str = getRockPaperScissorsPlayFromLeftSubstring(other_player_play)

    if play_str is None:
        raise ValueError("Bad 'play': not one of rock, paper, or scissors.")
    if other_player_play_str is None:
        raise ValueError("Bad 'other_player_play': not one of rock, paper, or scissors.")

    # PAPER, SCISSORS, ROCK  <=> 0, 1, 2
    # if i == j: tie
    # if j == ((i+1) % 3): j beats i
    # else: i beats j

    psr = ['paper', 'scissors', 'rock']
    play_int = psr.index(play_str)
    other_play_int = psr.index(other_player_play_str)

    if play_int == other_play_int:
        return None
    elif other_play_int == ((play_int + 1) % 3 ):
        return False
    else:
        return True
