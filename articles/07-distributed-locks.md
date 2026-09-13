# Step 6 · Distributed Locks

## The problem: two clicks, one plan, two runs

A user opens an approved research plan in two browser tabs and clicks **Approve** in both. Or clicks once, the page is slow, and they click again. Two requests now arrive to run the same plan.

Nothing in the plain design stops them. Both requests start the graph, the plan executes twice — two sets of database queries, double the cost — and the final saved state is left to whichever request happens to finish last. The app returned `200 OK` both times. Nobody sees an error.

We need one rule: **only one request may run a given conversation at a time.** Everyone else must be turned away. That rule is a *lock*.

> **[Lock](00-glossary.md#lock)** — a rule enforced through data: "only one process may act on X at a time." **[Claim](00-glossary.md#claim)** — this series' word for a lock that guards a resumable run: a small marker a request writes before running that says *"I'm running this thread — hands off."*

## Why an in-process lock is not enough

Your API does not run as one process. It runs as several identical copies — [replicas](00-glossary.md#replica) — behind a load balancer. The two approve clicks can land on two different replicas.

```text
Browser tab A  ->  API replica 1  ┐
                                  ├─>  the lock must live HERE, where both can see it
Browser tab B  ->  API replica 2  ┘
```

A Python `threading.Lock` lives inside one replica's memory and is invisible to the other. The lock has to live somewhere every replica can see: a database. We use one DynamoDB item.

## `attribute_not_exists` in plain English

The whole lock rests on one database feature: the [**conditional write**](00-glossary.md#conditional-write) — a write that only succeeds if a condition is true *at the moment of writing*, with no gap for a second writer to slip in.

`attribute_not_exists(PK)` is the condition **"only if there is no item with this key yet"** — in plain words, *"only if this slot is empty."* For a lock, that means *"acquire only if nobody holds it."*

```python
table.put_item(
    Item={
        "PK": f"CLAIM#{thread_id}",   # the slot this claim lives in
        "SK": "RESUME",
        "claim_token": token,          # who holds it (explained below)
        "expires_at": now + 1800,      # when it lapses (explained below)
    },
    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
    ExpressionAttributeValues={":now": now},
)
```

Read the condition as one sentence: **"write this claim only if the slot is empty, OR the claim already there has expired."** Two racing approves both attempt this write; DynamoDB evaluates the condition and performs the write as one atomic step, so exactly one wins. The winner runs the plan. The loser's write is rejected with `ConditionalCheckFailedException` — which is not an error, it is the expected **"someone else got there first"** signal. Return a 409 and stop.

## The lease must be short, the work is long

Why is there an `expires_at` at all? Because of a dilemma:

- **If the claim never expires**, a request that crashes while holding it wedges the conversation *forever*. Every later request finds a live claim and backs off, until a human manually deletes a database row.
- **If the claim expires quickly**, a healthy request whose real work runs longer than the expiry loses the claim mid-run.

The way out is renewal. Keep the expiry short (minutes) and have the *living* holder keep pushing it forward while it works. This sliding expiry is the claim's [**lease**](00-glossary.md#lease).

The natural place to renew is wherever the work already proves it is both alive and making progress. In a checkpointing graph, that is each [checkpoint](00-glossary.md#checkpoint) write — not a wall-clock timer. (A timer keeps renewing a process that is alive but stuck; a progress-tied renewal only extends a run that is actually advancing.)

```python
# renew, piggybacked on each checkpoint write
table.update_item(
    Key={"PK": f"CLAIM#{thread_id}", "SK": "RESUME"},
    UpdateExpression="SET expires_at = :new",
    ConditionExpression="claim_token = :token",   # only if it is still MY claim
    ExpressionAttributeValues={":new": now + 1800, ":token": token},
)
```

**Size rule:** the lease must be longer than the longest single unit of work between renewals (one model call or one tool call) — never the whole run, which can be far longer.

## Tenure token, not worker identity

Look at that `claim_token = :token` condition. Every write to the claim — acquire, renew, release — is guarded by *"only if the stored token still equals mine."* The [**token**](00-glossary.md#token) is a fresh random string minted each time the claim is acquired. It identifies **this specific tenure**, not the worker.

The mental model is a hotel key card. At check-in the desk programs a *new* card for the room; your name is not on it. When the next guest checks in, the door is reprogrammed for their card, and your old card simply stops working. Nobody has to find you and take your card away — it just no longer matches the door.

Takeover works exactly this way. A new holder overwrites the expired claim with its own token. There is no separate "break the old lock" step, and the old holder is never told. Its token was never revoked — it just stops matching what is in the slot.

Watch one full takeover play out. Each state is one real DynamoDB item; step through it:

<div class="scrubber" data-scrubber markdown="0">
  <div class="scrubber-stage">
    <div class="scrubber-step">
      <span class="scrubber-time">t = 0 · A acquires</span>
      <div class="scrubber-caption">Worker A mints token "aaa" and writes the claim — the slot is empty, so the conditional write succeeds.</div>
      <pre class="scrubber-item">CLAIM#abc { owner: "aaa", expires_at: 600 }   <span class="ok">← A holds it</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">t = 0…40 · A renews</span>
      <div class="scrubber-caption">On every checkpoint, A renews. The lease slides forward. A is healthy.</div>
      <pre class="scrubber-item">CLAIM#abc { owner: "aaa", expires_at: 640 }   <span class="ok">← lease pushed forward</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">t = 45 · A freezes</span>
      <div class="scrubber-caption">A hits a GC pause or a network partition. No checkpoints, so no renewals. The table does not change.</div>
      <pre class="scrubber-item">CLAIM#abc { owner: "aaa", expires_at: 640 }   <span class="warn">← frozen, not renewing</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">t = 55 · lease lapses</span>
      <div class="scrubber-caption">expires_at passes. Nothing happens in the table — expiry is not an event. It only means the next acquire's condition will now pass.</div>
      <pre class="scrubber-item">CLAIM#abc { owner: "aaa", expires_at: 640 }   <span class="warn">← now < 'now'; reclaimable</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">t = 60 · B takes over</span>
      <div class="scrubber-caption">Worker B runs the same acquire. The "OR expires_at < now" half is now true, so the write succeeds and overwrites the whole item. That overwrite IS the takeover.</div>
      <pre class="scrubber-item">CLAIM#abc { owner: "bbb", expires_at: 660 }   <span class="ok">← B holds it now</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">t = 75 · the zombie wakes</span>
      <div class="scrubber-caption">A unfreezes, still believing it owns the thread. Its next renew is guarded by owner = "aaa", but the item says "bbb". The write fails. A is fenced out — its card no longer opens the door.</div>
      <pre class="scrubber-item">renew if owner = "aaa"  vs  item owner "bbb"   <span class="bad">✗ ConditionalCheckFailed</span></pre>
    </div>
  </div>
</div>

The last frame is the danger the token defends against: a frozen holder has no idea it was replaced, because expiry is silent. A process in that state is a [**zombie**](00-glossary.md#zombie).

## The write fence: when even the lock check races

A lock check at the top of a critical section is a *check-then-act* gap: a holder can lose the claim in the window between checking it and writing. So the lock is not the last line of defense — the **data write itself** is.

Make every real write conditional on a slot only the rightful owner can fill. If each event carries a unique, ever-increasing number, write it with *"this slot must be empty."* A zombie whose counter has fallen behind the new holder collides on its very next write. This is a [**write fence**](00-glossary.md#fencing).

```python
try:
    table.put_item(
        Item={"PK": thread_id, "SK": seq, "idem_key": key, ...},
        ConditionExpression="attribute_not_exists(PK)",   # this exact slot must be empty
        ReturnValuesOnConditionCheckFailure="ALL_OLD",     # on failure, hand me the occupant
    )
except ClientError:
    existing = read_returned_item()   # someone is already in my slot — who?

    if existing["idem_key"] == my_write.idem_key:
        # Branch 1 — the occupant is my OWN logical write. A retry or a replay already
        # landed it. The write is already satisfied; carry on.
        return seq

    lock = get_claim(thread_id)
    if lock is None or lock["owner"] != token or lock["expires_at"] < now:
        # Branch 2 — I am the zombie. My claim was taken while I was frozen.
        # Stop writing entirely. Never renumber past the collision.
        raise ZombieWriter(thread_id)

    # Branch 3 — foreign occupant, but I DO still hold the claim: a dead predecessor's
    # in-flight write landed after I started. Re-read the max, hop over it, retry.
    seq = reseed()
    retry_bounded()
```

Three ideas make this hold:

- **The corruption attempt is the detection.** The zombie is never told it lost. The very write that would have corrupted the log is the atomic operation that reveals it lost. The storage layer is the final arbiter of who is alive.
- **"Mine" is decided by content, not identity.** Branch 1 compares an [idempotency key](00-glossary.md#idempotency-key) — a value derived from *execution position*, identical on replay — not a worker id. Equal key means the logical write already exists; who physically wrote it is irrelevant. (This is why the key must not be built from the sequence number, which changes on replay — see [Durable Async Runs](08-queue-and-worker-execution.md#sequence-number-versus-idempotency-key).)
- **The lock read is lazy.** Healthy workers never poll the claim. It is read only on the rare collision path, so the common case pays nothing.

## Release must prove ownership

The same lesson returns at cleanup. Releasing the claim is a *token-guarded* delete:

```python
table.delete_item(
    Key={"PK": f"CLAIM#{thread_id}", "SK": "RESUME"},
    ConditionExpression="claim_token = :token",   # only delete MY claim
    ExpressionAttributeValues={":token": token},
)
```

Without the guard, a slow request that already lost its lease would, on teardown, delete the *next* holder's claim — reopening the exact double-run window the lock exists to close. "Release a lock" and "release *your* lock" are different operations.

## Why acquire and release stay atomic

First, "atomic" means **all-or-nothing, with no visible halfway state**.

For a database write, that means the database does the check and the change as one indivisible operation. No other worker can slip into the tiny gap between "I checked the row" and "I updated the row," because there is no gap exposed to your application.

The unsafe version looks like this:

```text
1. read the lock row
2. decide it looks free
3. write my claim
```

Two workers can both complete step 1 before either reaches step 3. Both think the lock is free, and both write. That is a race.

The atomic version looks like this:

```text
write my claim only if the row is still free
```

The database evaluates the condition and commits the write together. The lock is safe because acquire and release use that atomic conditional-write shape instead of read-then-write application logic.

Acquire is atomic because the condition and the write happen together:

```text
write claim only if:
  slot is empty
  OR existing lease has expired
```

Two workers can race into that line at the same time. DynamoDB does not let both observe "empty" and then both write. It evaluates the condition against the current item and commits one winning write as one indivisible operation. The loser receives `ConditionalCheckFailedException` and never owns the claim.

Release is atomic for the same reason, but the condition is different:

```text
delete claim only if:
  stored claim_token == my claim_token
```

The release does not read the row, decide in Python, and then delete later. The ownership check and delete are one operation. If another worker reclaimed the expired lock between those moments, the stored token has changed, so the old holder's delete fails instead of deleting the new holder's claim.

That is the rule interns should remember:

```text
Acquire atomically creates ownership.
Release atomically proves ownership before deleting it.
```

If either side becomes "read, then decide, then write," the lock has a race window.

## Capacity and table design

A lock this cheap is easy to under-think. Here is exactly what it costs to store and run.

### One table, three kinds of item

The lock is not its own table. It is one item in the table that already holds the run's events and run-state, kept apart by a [`PK`](00-glossary.md#pk-sk) prefix:

| Item kind | PK | SK | Fields |
|-----------|----|----|--------|
| event | `abc` | `seq` (1, 2, 3…) | `idem_key`, `event`, `ttl` |
| run-state | `run#abc` | `0` | `status`, `reason`, `receive_count` |
| **lock** | `lock#abc` | `0` | `owner`, `expires_at`, `ttl` |

DynamoDB is schemaless beyond the key, so the three coexist for free. Each thread's lock lives under its *own* partition key (`lock#<thread_id>`), so lock writes spread across partitions — there is no single hot lock row to bottleneck on.

### How much memory a lock takes

A lock item carries a partition key, a sort key, an owner token, and two timestamps:

```text
lock#3f9c…  (PK, ~41 B)   0 (SK)   owner "…uuid4…" (~36 B)   expires_at (N)   ttl (N)
```

Counting attribute names and values, a lock item is well under **200 bytes** — comfortably inside DynamoDB's 1 KB write unit and 4 KB read unit. Storage is negligible: even 100,000 simultaneously-locked threads is roughly **20 MB**. And locks are transient — the [`ttl`](00-glossary.md#ttl-time-to-live) sweeps abandoned rows, so the steady-state count tracks *concurrent active runs*, not total runs ever started.

### What it costs to run

Per run, the lock does a small, fixed number of writes:

| Operation | When | Cost |
|-----------|------|------|
| Acquire | once, at pickup | 1 conditional write |
| Renew | once per checkpoint | 1 write per checkpoint |
| Release | once, at completion | 1 write |
| Check | only on a write-fence collision | 1 read (rare) |

Reads are essentially free — the healthy path never polls the lock. Writes are dominated by renewals. A 45-minute run that checkpoints ~50 times spends about **52 lock writes total**.

*Worked example.* 200 concurrent runs, each checkpointing every ~45 s → 200 ÷ 45 ≈ **4.4 renew writes/sec**, plus a trickle of acquires and releases. Call it ~5 writes/sec. On-demand DynamoDB absorbs that without thought; provisioned capacity needs single-digit WCU. The lock is not where your bill lives.

### The two "time to live"s — they are different

The word "expiry" hides two unrelated fields on the lock item. Confusing them is a real bug.

| Field | Who enforces it | Typical value | Purpose |
|-------|-----------------|---------------|---------|
| `expires_at` | **your code**, inside the acquire/renew condition | the lease, e.g. 10–30 min | The **enforced** lease and the *acquired time-to-live*: how long a fresh acquisition stays valid before another worker may reclaim it (`acquire only if empty OR expires_at < now`). A healthy holder keeps pushing it forward. |
| `ttl` | **DynamoDB's sweeper** | ~24 h | Janitorial only. Deletes abandoned rows so a crashed run's lock eventually vanishes without a human. Best-effort; deletion can lag hours. No lock logic may depend on it. |

**Sizing the lease (`expires_at`):** it must exceed the longest single unit of work *between renewals* — one model call or one tool call, on the order of 30–120 s — with margin, because that is the window during which no renewal arrives. A 10–30 minute lease over a per-checkpoint renewal is comfortable. Do not size it to the whole run; that is what renewal is for. And never let `ttl` double as the lease — a sweeper that runs hours late would leave a dead lock wedging the thread long past its intended life.

## Guardrails

- Put the lock where every replica can see it — a database item, never process memory
- Treat `ConditionalCheckFailedException` as normal contention, not an infrastructure error
- Mint a fresh ownership token per acquisition; guard every renew, release, and fence with it
- Size the lease from observed worst-case work between renewals, not optimistic averages
- Renew on progress (each checkpoint), never on a wall clock
- Back the lock with a write fence, so correctness survives even a raced lock check
- Verify ownership on release; never delete a claim unconditionally

---

!!! check "You should now understand"
    - Why an in-process lock is invisible to other API replicas or workers
    - What "atomic" means: all-or-nothing, with no halfway state your application can race through
    - Why acquire is a conditional write and release is a token-guarded conditional delete
    - Why a lease must expire and also be renewed by progress
    - Why a write fence catches a worker that lost ownership but woke up later

??? question "Try this"
    **Worker A holds a claim with token `aaa`. It freezes, the lease expires, and Worker B acquires the same thread with token `bbb`. Worker A wakes up and tries to release the lock. What must happen?**

    ??? success "Answer"
        A's release must fail because the stored token is now `bbb`, not `aaa`. Release is `delete only if claim_token == my token`. Without that atomic ownership check, A would delete B's live claim and reopen the double-run race.

*Next: [Step 7 · Queue and Worker Execution](08-queue-and-worker-execution.md) — the full run that survives crashes, deploys, and reconnects, with the lock as one part of the worker ritual.*
