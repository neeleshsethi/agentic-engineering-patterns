---
summary: "The two AWS primitives the next steps lean on: conditional writes and FIFO queues."
---

# Bridge · DynamoDB conditional writes and SQS

Steps 6 and 7 are built from two AWS services. DynamoDB stores the run's state, and SQS hands approved work to a background worker. Both steps assume you already know how these services behave when two writers collide or when a worker dies. This page teaches that behavior. The lock, the fence and the worker ritual come later.

The running example is the same retail analytics agent. A user asked for BRAND_A sales by market, approved a plan that queries SOURCE_A and SOURCE_B, and the run is now executing on thread `abc`.

## A DynamoDB item and its address

DynamoDB stores **items**. An item is a bag of named attributes, like a Python dict. Every item has an address made of two parts, the partition key and the sort key ([PK / SK](glossary.md#pk-sk)). The PK groups related items. The SK orders items inside that group. Together they name exactly one item.

This series uses two tables:

| Table | Holds | Example address |
|-------|-------|-----------------|
| `{env}-agent-events` | the run's feed lines, its run-state row, its run lock | `PK=abc, SK=12` is feed line 12 of thread `abc` |
| `{env}-agent-checkpoints` | the graph's saved state, and the resume claim | `PK=CLAIM#abc, SK=RESUME` |

DynamoDB enforces no schema beyond the key. So one table can hold several kinds of item, told apart by a prefix on the PK. In the events table, `abc` is the feed, `run#abc` is the run-state row, and `lock#abc` is the lock. Step 6 shows the full layout.

Writing an item looks like this:

```python
import boto3

table = boto3.resource("dynamodb").Table("prod-agent-events")

table.put_item(Item={
    "PK": "abc",
    "SK": 12,
    "event": "Querying SOURCE_A for BRAND_A sales by market",
})
```

One fact about `put_item` matters more than any other. **If an item already exists at that address, `put_item` replaces it completely.** It does not merge, and it does not warn you. Whatever was in slot `abc / 12` is gone.

## Why read-then-write races

Suppose two workers both think they should write feed line 12. The obvious way to avoid clobbering is to look first.

```python
existing = table.get_item(Key={"PK": "abc", "SK": 12}).get("Item")
if existing is None:
    table.put_item(Item={"PK": "abc", "SK": 12, "event": my_line})
```

This is broken. Each line is a separate request, and the other worker can run between them.

```text
worker A: get_item(abc, 12)  -> empty
worker B: get_item(abc, 12)  -> empty
worker A: put_item(abc, 12)  -> writes A's line
worker B: put_item(abc, 12)  -> overwrites A's line
```

Both workers checked, both saw an empty slot, and both wrote. A's line is lost and nobody got an error. The gap between the read and the write is the bug, and faster code or a retry does not close it.

## A conditional write closes the gap

A [conditional write](glossary.md#conditional-write) sends the check and the write as one request. DynamoDB evaluates the condition against the item currently at that address and applies the write only if the condition is true. It does both as one atomic step, so no other writer can act between them.

The condition goes in the [`ConditionExpression`](glossary.md#conditionexpression) parameter. The most common one is [`attribute_not_exists(PK)`](glossary.md#attribute_not_existspk).

```python
table.put_item(
    Item={"PK": "abc", "SK": 12, "event": my_line},
    ConditionExpression="attribute_not_exists(PK)",
)
```

Read it as "write only if no item lives at this address." The condition is checked against the one item at `PK=abc, SK=12`. It does not ask whether partition `abc` has any items at all. Every item has a PK, so "this item has no PK attribute" can only be true when the item does not exist.

Now replay the race. A's write finds the slot empty and succeeds. B's write finds A's item and is rejected. Exactly one worker wins.

Conditions can say more than "empty." `expires_at < :now` and `claim_token = :token` are both valid conditions, and Step 6 uses them. Values from your code go in `ExpressionAttributeValues`, with names that start with a colon.

## A failed condition is a normal outcome

When the condition is false, DynamoDB writes nothing and boto3 raises a `ClientError` whose code is `ConditionalCheckFailedException`. That error does not mean the database is broken. It means another writer got there first, and that is information your code asks for on purpose.

```python
from botocore.exceptions import ClientError

try:
    table.put_item(
        Item={"PK": "abc", "SK": 12, "event": my_line},
        ConditionExpression="attribute_not_exists(PK)",
        ReturnValuesOnConditionCheckFailure="ALL_OLD",
    )
except ClientError as err:
    if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
        raise                              # a real failure: throttling, permissions, network
    occupant = err.response.get("Item")    # the item that was already in the slot
```

`ReturnValuesOnConditionCheckFailure="ALL_OLD"` asks DynamoDB to put the existing item in the error response. Without it, you know you lost but not to whom, and finding out takes a second read. That second read reopens a gap, because the item can change before you read it. Step 6 inspects the returned occupant to decide what kind of collision it hit.

## TTL is cleanup, not a timer

DynamoDB can delete old items for you. You pick one Number attribute (this series calls it `ttl`), store a Unix timestamp in seconds, and turn on [TTL](glossary.md#ttl-time-to-live) for the table. A background process deletes items whose timestamp has passed.

That process is best-effort. It can delete an item long after its timestamp, and until it does, the expired item still shows up in reads and queries and still blocks an `attribute_not_exists` write. So TTL only keeps old rows from piling up. Any rule about time that your code depends on, such as "this lease has expired," must be a timestamp your own condition compares against the current time. Step 6 keeps these two fields apart on purpose.

## SQS FIFO in five behaviors

SQS is a message queue. The API sends a message, a worker receives it, does the work, and deletes it. This series uses a [FIFO queue](glossary.md#fifo-queue), whose name ends in `.fifo`. Five behaviors matter.

**1. Message groups serialize work per thread.** Every message sent to a FIFO queue carries a `MessageGroupId`. While one message from a group is in flight, SQS will not deliver the next message from that same group. Messages from other groups still flow to other workers in parallel. With `MessageGroupId=thread_id`, two runs of thread `abc` never execute at once, while threads `abc` and `xyz` run side by side.

```python
import json

sqs = boto3.client("sqs")

sqs.send_message(
    QueueUrl=queue_url,
    MessageBody=json.dumps({"thread_id": "abc", "run_id": "run-001"}),
    MessageGroupId="abc",
)
```

FIFO queues also drop duplicate sends. Each message needs a deduplication id, or the queue can have content-based deduplication turned on, in which case SQS uses a hash of the body. A second send with the same id within five minutes is accepted but not delivered. Step 7 relies on this for double-clicked approvals.

**2. Receiving a message hides it rather than removing it.** When a worker receives a message, SQS makes it invisible to other consumers for the [visibility timeout](glossary.md#visibility-timeout). The message is removed only when the worker calls `delete_message` with the receipt handle it got on receive. A worker that needs more time calls `change_message_visibility` to push the deadline out.

**3. Silence means redelivery.** If the timeout runs out before the worker deletes the message, SQS makes the message visible again and another worker receives it. SQS cannot tell a crashed worker from a slow one. It only sees that the message was not deleted in time.

**4. SQS counts receives.** Each delivery increments the message's `ApproximateReceiveCount`. A worker sees it only if it asks.

```python
resp = sqs.receive_message(
    QueueUrl=queue_url,
    MessageSystemAttributeNames=["ApproximateReceiveCount"],
)
msg = resp["Messages"][0]
attempt = int(msg["Attributes"]["ApproximateReceiveCount"])   # 1 on first delivery
```

**5. Too many receives sends the message to the DLQ.** A redrive policy on the queue names a [dead-letter queue](glossary.md#dlq-dead-letter-queue) and a `maxReceiveCount`. When a message has been received that many times without being deleted, SQS moves it to the DLQ instead of delivering it again. A FIFO queue's DLQ must also be FIFO. This stops one poison message from looping forever and blocking its group.

## At-least-once means replay-safe

AWS describes FIFO queues as exactly-once processing. That claim covers duplicate sends within the deduplication window. It cannot cover a worker that finishes half the work and then dies.

Walk through thread `abc`. The worker receives the message, queries SOURCE_A, writes three feed lines, and crashes before it deletes the message. The visibility timeout lapses. A second worker receives the same message and starts again. The SOURCE_A query runs twice, and the feed-line writes are attempted twice.

So from your code's point of view, every message can be delivered more than once. Any work a message triggers has to be safe to run again, with the same end state whether it ran once or three times. That property is [idempotency](glossary.md#idempotency). The queue will not give it to you. Your writes have to provide it, and conditional writes are the main tool for that.

## What Steps 6 and 7 build on top

You now have every piece the next two steps assemble.

[Step 6 · Distributed locks](step-6-distributed-locks.md) turns `attribute_not_exists` plus an expiry condition into a claim that only one process can hold. It then guards every renew and release with a condition on a per-tenure token. Its write fence is the conditional put on a feed slot from this page, with `ALL_OLD` used to tell a harmless replay from a zombie writer.

[Step 7 · Queue and worker execution](step-7-queue-and-worker-execution.md) uses `MessageGroupId=thread_id` for one worker per thread and extends the visibility timeout on each checkpoint as a heartbeat. It copies `ApproximateReceiveCount` onto the run-state row and treats the DLQ as the only path that pages a human. Because redelivery means replay, it gives every feed line an idempotency key that comes out the same on the second attempt.

*Next: [Step 6 · Distributed locks](step-6-distributed-locks.md).*
