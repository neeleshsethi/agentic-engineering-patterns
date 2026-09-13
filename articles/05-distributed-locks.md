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

## The Lease Must Be Short, The Work Is Long

These two facts pull in opposite directions:

- If the lease is long, a crashed owner wedges the thread until a human deletes a database item. Every later worker finds a "live" claim and backs off forever.
- If the lease is short, a healthy owner whose real work takes longer than the lease loses the claim mid-run.

The resolution is renewal. Keep the lease short (minutes) and have the living owner push `expires_at` forward while it works. The natural place to renew is wherever the work already proves it is alive and making progress. In a checkpointing graph, that is each checkpoint write, not a wall-clock timer.

A timer renews a process that is alive but stuck. A progress-tied renewal only extends a run that is actually advancing, so a hung worker stops renewing and the claim is reclaimed.

```python
# renew, piggybacked on each checkpoint, guarded by the same token
table.update_item(
    Key={"PK": f"CLAIM#{thread_id}", "SK": "RESUME"},
    UpdateExpression="SET expires_at = :new",
    ConditionExpression="claim_token = :token",   # only my claim
    ExpressionAttributeValues={":new": now + LEASE, ":token": token},
)
```

Every write to the claim — acquire, renew, release — carries the token condition. The size rule that falls out: the lease floor must exceed the longest single unit of work between renewals (one model call or one tool call), never the whole run.

## Tenure Token, Not Worker Identity

The claim identifies a *tenure*, not a worker. Every acquisition mints a brand-new random token, even if the same worker reacquires later.

Think of a hotel key card. At check-in the desk programs a new card for the room; your name is not on it. When the next guest checks in, the door is reprogrammed for their card and your old card simply stops opening the door. Nobody has to find you and revoke anything.

Takeover works the same way. A new owner overwrites the expired claim with its own token. There is no separate "break the old lock" step, and the old owner is never consulted. Its token was never revoked — it just no longer matches what is in the door.

```text
t=0    A acquires, token "aaa". Writes claim if absent or expired.
t=0-40 A renews on every checkpoint. expires_at slides forward.
t=45   A freezes (GC pause, network partition). No checkpoints, no renews.
t=55   expires_at passes. Nothing happens in the table. Expiry is not an event.
t=60   B takes over: the same conditional write now sees an expired claim, succeeds,
       overwrites the item with token "bbb".
t=75   A unfreezes, still believing it owns the thread.
```

The last line is the real problem. Expiry is silent, so a frozen owner has no idea it was replaced. This is the zombie.

## The Write Fence: When Even the Lock Check Races

A lock check at the top of a critical section is a check-then-act gap: the owner can lose the claim in the window between checking and writing. So the lock is not the last line of defense. The *data write itself* is.

Make every real write conditional on a slot that only the rightful owner can fill. If a per-item sequence number must be unique, write it with "this slot must be empty." A zombie whose sequence counter has fallen behind the new owner collides on its very next write.

```python
try:
    table.put_item(
        Item={"PK": thread_id, "SK": seq, "idem_key": key, ...},
        ConditionExpression="attribute_not_exists(PK)",   # this exact slot must be empty
        ReturnValuesOnConditionCheckFailure="ALL_OLD",
    )
except ClientError:                       # collision — inspect the occupant
    existing = read_returned_item()

    if existing["idem_key"] == my_write.idem_key:
        # Branch 1: the occupant is my own logical write. A retry or replay already
        # landed it. The write is satisfied; continue.
        return seq

    lock = get_claim(thread_id)
    if lock is None or lock["owner"] != token or lock["expires_at"] < now:
        # Branch 2: I am the zombie. My claim was taken while I was frozen.
        # Stop writing entirely. Never renumber past the collision.
        raise ZombieWriter(thread_id)

    # Branch 3: foreign occupant, but I still hold the claim — a dead predecessor's
    # in-flight write landed after I started. Re-read the max, hop over it, retry.
    seq = reseed()
    retry_bounded()
```

Three things make this work:

- **The corruption attempt is the detection.** The zombie never has to be told it lost; the very write that would corrupt the log is the atomic operation that reveals it lost. The storage layer is the final arbiter of who is alive.
- **"Mine" is by content, not identity.** Branch 1 compares a deterministic idempotency key, not a worker id. If the key is derived from execution position (not from the sequence number, which changes on replay), an equal key means the logical write already exists and who physically wrote it is irrelevant.
- **The lock read is lazy.** Healthy workers never poll the claim. It is read only on the rare collision path, so the common case pays nothing.

## Guardrails

- Use unique ownership identifiers
- Verify ownership on release
- Size leases from observed worst-case work, not optimistic averages
- Instrument lock acquisition, renewal, and expiry paths
- Treat conditional write failures as normal contention, not infrastructure failure
