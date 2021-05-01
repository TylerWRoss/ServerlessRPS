# Application Implementation Details

The Application Stack's interactions with the Toolchain are discussed in Section 2, "Architecture", of the [whitepaper](whitepaper.pdf).

Here, we provide a high-level walk-through of the source code (i.e., "what's implemented where?"), and explain several notable behaviors (e.g., locking) and datastructures (e.g., database document formats).

<!-- MarkdownTOC autolink="true" levels="1,2,3" -->

- [Source Code Walk-through](#source-code-walk-through)
- [DynamoDB Table Formats](#dynamodb-table-formats)
	- [Idempotency Table](#idempotency-table)
	- [GameState Table](#gamestate-table)
	- [Nickname Table](#nickname-table)
- [Message Handling Flow](#message-handling-flow)
- [Idempotence](#idempotence)
- [Locking](#locking)

<!-- /MarkdownTOC -->

## Source Code Walk-through

The source code of SRPS is located in the `serverless_rps/` directory of this repository.

`requirements.txt` is a standard Python dependency listing.

`app.py` defines the event handler (`lambda_handler()`) invoked when an instance is spawned, and is therefore responsible for startup operations (e.g., reading environment variables, instantiating database connections, etc.). `app.py` also implements the routing logic responsible for selecting and invoking an appropriate command function, based on the body of an incoming message.

`commands.py` defines the aforementioned "command functions." Each method defined in `commands.py` correlates to a keyword-identified command a user may invoke: "help", "throw", "quit", etc..

`utils.py` defines helper/wrapper methods for common operations (e.g., player record locking), game logic (e.g., calculating a rock-paper-scissors winner), etc.. These methods are called by both `commands.py` and `app.py`. The contents of `utils.py` could be separated more granularly – for example, by category: locking, idempotency, game logic, etc..

## DynamoDB Table Formats

### Idempotency Table


The Idempotency Table is keyed by the UUID of the respective message (`messageId` attribute).

Records include a TTL attribute (`TTLEpochTimestamp`) which is dual-purposed to support expiring idempotence records (for retry purposes), and DynamoDB's TTL mechanism (for resource conservation purposes).

For more information, see [Idempotence](#idempotence).

```
{
  "messageId": "<message uuid>",
  "TTLEpochTimestamp": <unix epoch timestamp>
}
```

### GameState Table

The GameState Table is keyed by the E.164-formatted phone number of the respective player (`phone_number` attribute).

If the player has registered a nickname (canonically stored in the Nickname Table), it is denormalized onto the GameState Table as the `nickname` (lowercase, for lookup/logical purposes) and `display_name` (original case, for display purposes only) attributes.

The `games` attribute stores *this player's throws* against other players, while the system awaits the other player's throw. As games are completed, entries are removed.

Finally, the `user_locked` attribute is used to pessimistically lock this player record (see [Locking](#locking)). When a lock is released, the attribute is removed.

```
{
  "phone_number": "<E.164 phone number>",
  "display_name": "<denormalized display name>",
  "nickname": "<denormalized nickname>,
  "games": {
    "<other player nickname>": "<rock/paper/scissors>",
    "<another player nickname>": "<rock/paper/scissors>"
  },
  "user_locked": {
    "lock_uuid": "<lock uuid>",
    "expiration_epoch_timestamp": <unix epoch timestamp>
  },
}
```

### Nickname Table

The Nickname Table is keyed by the ***lowercase*** nickname of the player (`nickname` attribute). DynamoDB does not support case-insensitive uniqueness, so it is up to the application to enforce this constraint. This constraint is important, because having "Tyler" and "tyler" be unique player nicknames is obviously not desirable.

The Nickname Table's purpose is to allow for indexed lookups by nickname without requiring a second, expensive index on the GameState Table. As such, the Nickname Table includes a `phone_number` attribute which is the E.164-formatted phone number of the player to whom this nickname is registered. For convenience, and efficient reverse-lookups, a player’s nickname is also denormalized onto their player record in the GameState Table.

Finally, because the nickname is cast to lowercase for logical purposes, the original case is retained as the `display_name` attribute (also denormalized onto GameState) for display purposes only.

```
{
  "nickname": "<nickname>",
  "display_name": "<display name>",
  "phone_number": "<E.164 phone number>"
}
```


## Message Handling Flow

The `lambda_handler()` function, defined in`app.py`, is the entrypoint of the application – called when the Lambda Function is invoked.

As the application's entry point, startup operations are performed here: reading configuration from environment variables, setting up `boto3` resource instances, etc.. After startup, execution loops over the message(s) passed into the `lambda_handler()` function.

The application attempts to atomically insert an idempotence record in the DynamoDB IdempotencyTable. If a record for the given message's UUID already exists, processing is skipped. Otherwise, the message is parsed, and the requestor's GameStateTable record is pessimistically locked. The player's request is then handled, and the result returned over SMS.

If the message was successfully processed, it is explicitly removed from the IncomingMessages SQS Queue. Using a `finally` clause, the requestor's GameStateTable record is unlocked (ensuring a stale lock is not left in the event of an uncaught exception).

---
**NOTE**

There is actually a neglected edge-case in this unlocking scheme! See Section 3.1.2 in the [whitepaper](whitepaper.pdf), or the "Where to Start?" section of the [README](../README.md#where-to-start), for more information.

---

This "idempotence-check, process, reply, remove" loop is repeated for each message.

Finally, if any messages failed to process (due to exception, existing lock, etc.) or had an existing idempotence record, the Lambda Function raises a RuntimeError. This marks the messages for which the function was invoked as having failed to process, and they are returned to the IncomingMessages queue and retried. The logic behind this is explained in [Idempotence](#idempotence).

## Idempotence

Idempotence is handled using a DynamoDB Table keyed by message UUIDs. When an instance begins to process a message, it first attempts a conditioned `put` against the IdempotencyTable. This put is conditioned on the nonexistence of a record with the same key (message UUID). In the event of a "ConditionalCheckFailedException" (i.e., an existing record), the message is skipped.

Under this scheme, the existence of an idempotence record only guarantees that an instance *started* to process a message. In the event an instance failed to process the message, and another should try, we remove the idempotence record, so that another instance will proceed. This is why the application explicitly removes messages on success, and raises an exception (after processing all other messages) on idempotency skips (see [Message Handling Flow](#message-handling-flow)).

When an instance raises an exception, instead of returning gracefully, the messages it was invoked with are returned to the queue to be retried. Because successfully processed messages have been explicitly removed, they will not be retried.

This leaves one more edge-case to consider, though: crashed/timed-out instances. In this case, messages will not have been explicitly deleted, and the instance's failure will have returned the messages to the queue. An instance which then attempts to process such a message will encounter an idempotence record and not proceed. To handle this, idempotence records include a dual-purposed expiration timestamp.

The first purpose of this timestamp is for DynamoDB's TTL mechanism to automatically remove old records, to conserve resources ((typically) occurs within a few minutes, or (officially) "within 48 hours"). The second purpose of this TTL is to allow for retry of messages in spite of an idempotence record.


## Locking

Locking occurs at the player/record/document level of the GameStateTable, by means of the `user_locked` attribute (see [GameState Table](#gamestate-table)).

A test-and-set scheme, using conditioned DynamoDB updates, is used to provide atomicity.

When an instance begins processing a message, the requestor's record is pessimistically locked. If the record could not be locked, the message is marked failed (returned to the queue, to be retried).

In the event of messages which involve another player (i.e., the "throw" command), the other player's record is also pessimistically locked. If the other player's lock could not be acquired, the requestor's lock is released, and the message is marked failed. This release-and-retry scheme ensures liveness at the small cost of requiring a message retry (currently, "a couple" milliseconds of Lambda execution time).

Stale locks are *avoided* (but not precluded!) using `finally` clauses, which release held locks even in the event of uncaught exceptions.

---
**NOTE**

There is actually a neglected edge-case in the implemented unlocking scheme! See Section 3.1.2 in the [whitepaper](whitepaper.pdf), or the "Where to Start?" section of the [README](../README.md#where-to-start), for more information.

---
