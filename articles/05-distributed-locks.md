# Distributed Locks

Distributed locks look straightforward until timing, retries, and partial failures are introduced. At that point, a lock can appear to work while still allowing duplicate execution or dead ownership.

## Why Agent Systems Need Them

LangGraph does not support two concurrent runs mutating the same thread safely.

If two browser tabs both approve the same plan, both requests can resume the graph. The research plan may execute twice, charge twice, and leave the final checkpoint to whichever run writes last.

An in-process Python lock is not enough when you have multiple API replicas. The lock must live where every replica can see it.

```text
Browser tab A -> API replica 1 -> DynamoDB claim
Browser tab B -> API replica 2 -> same DynamoDB claim
```

## Failure Modes

- Lease expiry shorter than real work duration
- Unlock operations that do not verify ownership
- Retry loops that reacquire locks without reconciling prior state
- Lock state stored only in one process while other replicas keep running
- Claim items that use a different key shape from release code

## DynamoDB-Specific Concerns

With DynamoDB-backed coordination, conditional writes help, but they are not enough by themselves. You still need clear ownership tokens, lease renewal strategy, and recovery behavior for crashed workers.

## `attribute_not_exists` In Plain English

In DynamoDB, a conditional write only succeeds if its condition is true at write time.

`attribute_not_exists(PK)` means "only write this item if there is not already an item with this partition key attribute."

For a lock, that means "acquire the claim only if nobody currently owns it."

```python
table.put_item(
    Item={
        "PK": f"CLAIM#{thread_id}",
        "SK": "RESUME",
        "claim_token": token,
        "expires_at": now + 1800,
    },
    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
    ExpressionAttributeValues={":now": now},
)
```

The second half, `expires_at < :now`, allows recovery from a crashed worker whose lease expired.

If another request already owns an unexpired claim, DynamoDB rejects the write with `ConditionalCheckFailedException`. That exception is the expected "lock busy" signal.

## Ownership Token Diagram

```text
Acquire
  token = random UUID
  write CLAIM#thread_id if absent or expired

Run
  stream graph resume

Release
  delete CLAIM#thread_id only if claim_token == token
```

The token matters because leases expire. A slow old worker must not delete a newer worker's claim.

```python
table.delete_item(
    Key={"PK": f"CLAIM#{thread_id}", "SK": "RESUME"},
    ConditionExpression="claim_token = :token",
    ExpressionAttributeValues={":token": token},
)
```

## Guardrails

- Use unique ownership identifiers
- Verify ownership on release
- Size leases from observed worst-case work, not optimistic averages
- Instrument lock acquisition, renewal, and expiry paths
- Treat conditional write failures as normal contention, not infrastructure failure
