# Life of a Deep Run

> Reconstructed from source notes. Genericized: product → "the platform"; brand → `BRAND_A`;
> data sources → `SOURCE_A`/`SOURCE_B`/…; tables → `app-{env}-deep-*`; the retrieval tool →
> `query_source`. Framework names (LangGraph, deepagents, DynamoDB, SQS) are real.

This is the after-approval half of a deep agent: everything that happens once a human has
approved a research plan and the run executes asynchronously on a worker fleet. Its companion,
[`before-resume.md`](./before-resume.md), covers the proposal/plan phase that leads up to the
approve click.

---

## Part 0 — The big picture

```
                         approve
  Browser  ───────────────────────────────►  API (ECS)
  │EventSource│                                 │  approve:
  │           │                                 │   1. checkpoint approval (durable)
  │  SSE      │                                 │   2. put_run_state(queued)   → deep-events
  │  frames   │◄──── id: seq  data: event       │   3. send_message           → SQS
  │  tail     │                                 │
  └───────────┘                                 ▼
        ▲                          SQS: app-{env}-deep-run.fifo
        │  Query(seq > cursor)     MessageGroupId = thread_id
        │  + read run# status                   │  receive / redeliver
        │  every ~250 ms                         ▼
        │                          Worker (ECS, S3)
        └──────── reads only ──────  pickup ritual: stamp · lock · seed · wrap
                  deep-events                     │
                                                  │  LangGraph astream()
                                    ┌─────────────┴──────────────┐
                                    ▼                            ▼
                    emitter: PutItem seq++ (fenced)   LeaseExtendingSaver:
                    → deep-events                     saver.put() → snapshot + heartbeat
                    (events + run# + lock#)           → deep-checkpoints
                                                      (LangGraph state = source of truth)

  3 strikes → SQS moves msg → app-{env}-deep-run-dlq.fifo → CloudWatch depth alarm → SNS → ops
```

**Read it left to right:** the `approve` call persists intent (checkpoint + run-state) *before*
enqueuing; the queue serializes per thread; the worker produces two independent write streams
(snapshots to `deep-checkpoints`, feed events to `deep-events`); the API reads only
`deep-events`; the DLQ is the only path that pages a human.

---

## Part 1 — The cast of characters

### The four stores

| Store | Physical name (per env) | What lives there | Who writes |
|---|---|---|---|
| **Job queue + DLQ** | `app-{env}-deep-run.fifo` / `app-{env}-deep-run-dlq.fifo` (SQS) | One message per approved run; poison pills end up in the DLQ | API enqueues; worker receives/deletes; SQS moves to DLQ |
| **Checkpoint table** | `app-{env}-deep-checkpoints` (DynamoDB) | LangGraph state snapshots — the source of truth for resume | LangGraph's saver (automatically) |
| **Event table** | `app-{env}-deep-events` (DynamoDB) | The user-facing activity feed. `PK=thread_id`, `SK=seq`, TTL ~24h | The worker's event emitter (our app code) |
| **Run-state + lock** | Same `deep-events` table, different PK prefix (`run#`, `lock#`) — **not** a separate table | `{status, reason, receive_count}` and `{owner, expires_at}` | Worker (+ failure detectors) |

**Where the names come from:** the DynamoDB stack derives every table name as
`app-{env}-dynamodb-{config-name}` and publishes it to SSM at
`/app/{env}/dynamodb/{name}/name`, which is how app code discovers the physical name at runtime
instead of hardcoding it.

Two principles govern the checkpoint/event split:

1. **The checkpoint is the source of truth; the event log is a projection.** An event-table
   outage never kills a run — the feed goes quiet, the work continues.
2. **The event partition contains only events.** Run-state and lock live under a different PK
   (`run#abc`, `lock#abc`) so the reader's `Query(thread_id = tid AND seq > cursor)` never needs
   a skip rule.

### The full layout of `deep-events`

One table, **three item kinds** (DynamoDB is schemaless beyond the key, so they coexist for free):

| PK | SK | Kind |
|---|---|---|
| `"abc"` | `<seq>` | events — the partition the SSE tail queries |
| `"run#abc"` | `0` | run-state record `{status, reason, receive_count, run_id}` |
| `"lock#abc"` | `0` | lock `{owner, expires_at, ttl}` |

The key schema is fixed for every item (`partition_key: thread_id String`, `sort_key: seq
Number`), and DynamoDB requires every item to supply the sort key. That single fact explains
`SK=0` on the singleton rows: on event rows `seq` is meaningful data (the feed counter); on
run-state and lock rows it's a mandatory placeholder — those partitions hold exactly one item
each, so a constant fills the slot. The payoff: the full key `("run#abc", 0)` is one exact
address — reads are `GetItem`, never `Query`.

#### 1. Event row — many per thread; the only rows where `seq` is a real counter

| Attr | Type | Value / meaning |
|---|---|---|
| `thread_id` | S (PK) | `"abc"` — bare thread id; the partition the SSE tail queries, **nothing else may use it** |
| `seq` | N (SK) | worker-allocated order (seeded from max, +1 per curated event); **gaps allowed** |
| `idem_key` | S | `task_id + intra-node index` → read-side dedup of crash-replayed events |
| `event` | M | `{type, label, phase, step_id, …}` — the curated payload the browser renders |
| `s3_key`, `content_type` | S | payloads > 400 KB become an S3 pointer |
| `ts` | N | display timestamp (epoch ms) — **never** used for ordering (that's `seq`'s job) |
| `ttl` | N | now + 24h — janitorial expiry, no correctness logic may depend on it (deletion lags ~48h) |

#### 2. Run-state row — exactly one per thread; the SSE tail's termination signal

| Attr | Type | Value / meaning |
|---|---|---|
| `thread_id` | S (PK) | `"run#abc"` — prefix keeps it out of the event query |
| `seq` | N (SK) | `0` — constant, singleton partition |
| `status` | S | `queued \| running \| retrying \| failed \| completed` — written by API (queued) then worker; the tail loop ends the stream on `failed`/`completed` |
| `reason` | S? | user-visible; only on `failed` (`"Run failed after 3 attempts"`) |
| `receive_count` | N | stamped from `ApproximateReceiveCount` at every pickup — durable strike history |
| `run_id` | S | `"run-42"` — which run this record describes |
| `updated_at` | N | epoch ms — debugging/staleness checks |

TTL: none, or **>** event TTL — the status must outlive the events it terminates.

#### 3. Lock row — at most one per thread; only touched with token-guarded writes

| Attr | Type | Value / meaning |
|---|---|---|
| `thread_id` | S (PK) | `"lock#abc"` — prefix isolates it from both other kinds |
| `seq` | N (SK) | `0` — constant, singleton partition |
| `owner` | S | `uuid4` minted at **this** acquisition — tenure identity, not worker identity; every renew/release conditions on it |
| `expires_at` | N | the **enforced** expiry, checked in the acquire condition (`… OR expires_at < now`) — app-compared, clock-skew tolerant |
| `ttl` | N | now + 24h — garbage collection of abandoned rows **only**, never consulted by lock logic |

> "So which rows does `seq` exist on?" The *attribute* exists on all rows (the schema demands
> it); the *counter* exists only on event rows. The two expiry-like fields on the lock differ:
> `expires_at` is load-bearing (evaluated inside the conditional put), `ttl` is a janitor.

### The IDs — where each one is born and what it's for

| ID | Born where | Lifetime | Purpose |
|---|---|---|---|
| `thread_id` | Frontend/session, before the run | Whole conversation | Partition key everywhere; SQS `MessageGroupId` |
| `run_id` | API, at approve time | One run | Distinguishes run N from N+1 on the same thread; part of the SQS body |
| `MessageGroupId` | = `thread_id`, on `send_message` | — | SQS serializes deliveries per group → one worker per thread |
| `ApproximateReceiveCount` | SQS, per receive | One receive | Drives 3-strikes behavior |
| Receipt handle | SQS, per receive | One receive | Capability to delete/extend *this* delivery |
| `checkpoint_id` | LangGraph, per super-step | Durable | Addresses one snapshot; ingredient of `idem_key` |
| `seq` | Worker's in-memory counter | Durable (one event) | Total order of the feed; SSE `id:`; becomes the reader cursor |
| `idem_key` | Worker's emitter, from execution position | Durable | Read-side dedup of replays; write-side own-retry detection |
| Lock owner token | Worker mints `uuid4()` at acquisition | One lock tenure | Proves this process holds the thread; guards renew/release |

---

## Part 2 — The happy path, end to end

### Step 0: Approve → checkpoint → enqueue (API side)

```python
# 1. Persist the approval INTO the checkpoint first (approval is durable before anything is
#    queued: a lost message can be re-enqueued, a lost approval cannot be reconstructed).
graph.update_state(config, {"plan": approved_plan})

# 2. Write the run-state record: the run now exists, even if no worker has touched it yet.
#    This is an item in the EVENT table at PK=f"run#{thread_id}", SK=0.
put_run_state(thread_id, status="queued", run_id=run_id)

# 3. Enqueue. Body is small and deterministic — the checkpoint holds the real state; the
#    message is just a pointer + intent.
sqs.send_message(
    QueueUrl=queue_url,
    MessageBody=json.dumps({"thread_id": thread_id, "run_id": run_id}),
    MessageGroupId=thread_id,   # serialize per thread
    # No MessageDeduplicationId: content-based dedup hashes the body.
)
```

**Why FIFO + `MessageGroupId = thread_id`:** SQS will not deliver a second message of the same
group while one is in flight. "One worker per thread" becomes a *transport* guarantee.
Different threads (groups) run in parallel — correctness needs serialization only per thread,
parallelism everywhere else.

**Why content-based dedup:** a double-clicked Approve, a frontend retry, or an SDK retry of
`send_message` produces a byte-identical body within seconds. SQS hashes the body (SHA-256) and
silently drops the duplicate.

### Step 1: Worker pickup — the full ritual

The worker long-polls. When a message arrives, six things happen in strict order **before the
graph runs**:

```python
msg = sqs.receive_message(
    QueueUrl=queue_url,
    MessageSystemAttributeNames=["ApproximateReceiveCount"],  # ask for it explicitly
    WaitTimeSeconds=20,
)["Messages"][0]

body    = json.loads(msg["Body"])            # {"thread_id": "abc", "run_id": "run-42"}
receipt = msg["ReceiptHandle"]               # capability for delete/extend
attempt = int(msg["Attributes"]["ApproximateReceiveCount"])
```

1. **Stamp** — copy `ApproximateReceiveCount` onto the run-state record (durable strike history).
2. **Lock** — conditional-put a fresh lease token (see Part 3).
3. **Seed** — `ConsistentRead` query for `max(seq)` on the event partition.
4. **Wrap** — compile the graph with `LeaseExtendingSaver` around the DynamoDB saver.
5. Renew lock expiry.
6. Run `LangGraph astream()`.

### The two write streams

**Stream 1 — checkpoints — is fully out of the box.** LangGraph calls `saver.put()` after each
node completes; we never write checkpoint code. Our only involvement is one wrapper class:

```python
class LeaseExtendingSaver(BaseCheckpointSaver):
    """Every checkpoint write also proves liveness in the same breath. Pure delegation
    otherwise."""

    def __init__(self, inner, on_put):
        self._inner = inner
        self._on_put = on_put   # extend lease + renew lock

    def put(self, config, checkpoint, metadata, new_versions):
        result = self._inner.put(config, checkpoint, metadata, new_versions)
        try:
            self._on_put()
        except Exception:
            logger.warning("lease/lock heartbeat failed; will retry next checkpoint")
        return result   # fire-and-forget: see edge case E8

    # get_tuple / list / put_writes: delegate unchanged
```

**Why hook `put()` and not a timer:** the checkpoint is simultaneously the *liveness* signal (a
dead worker stops checkpointing) and the *progress* signal (state just became durable). A
wall-clock heartbeat would keep renewing the lease of a worker that's alive but stuck; a
checkpoint-tied one lets SQS reclaim the job from zombies and hangs alike. **Sizing rule that
falls out:** the visibility timeout (~30 min) must exceed the longest *single* node (one LLM
call or one tool execution), never the whole run.

> `ChangeMessageVisibility` semantics — a common misconception: it is **not additive** and has
> nothing to do with how long the last node took. It *overwrites* the remaining countdown with a
> fixed value counted from the moment of the call ("N seconds from NOW"). We never measure node
> durations.

**Stream 2 — events — is entirely our code.** The emitter watches the LangGraph stream and
*curates*: friendly labels only (never raw SQL or tool args), phase and plan-step status, tool
activity, identified data sources, report tokens. One node might produce one or many events —
the counts are unrelated to checkpoint counts because the audiences differ.

### Step 3: How `seq` and `idem_key` are derived — precisely

**`seq` is a plain in-process integer:**

- **Seeded once per receive** from the table max (the `ConsistentRead` query in pickup step 3).
  DynamoDB has no auto-increment — sort-key values are always writer-supplied.
- **Incremented by the emitter** each time it decides a stream update deserves a feed line. Not
  per node, not per checkpoint — per curated user-visible event.
- **Safe as a local variable** only because of the single-writer-per-thread invariant (FIFO
  group + lock). The conditional put is the fence for the rare violations.
- **Gaps allowed by contract; ordering guaranteed.** A dead worker's straggler or a re-seed can
  leave holes. The reader only ever asks `seq > cursor`, so holes cost nothing.

**`idem_key` identifies the *logical* event, independent of which attempt wrote it:**

```python
idem_key = f"{checkpoint_id_the_node_was_launched_from}#{node_name}#{intra_node_index}"
#            └─ stable across crash-replay ─┘             └─ 0,1,2… within this node run ─┘
```

The defining property, and how each ingredient earns its place:

- **Must be identical when a node is crash-replayed** → derived from execution position, never
  from `seq` (which changes on replay) and never random.
- **Must differ across legitimate re-runs** → our agent node runs many times on purpose (ReAct
  loop). `checkpoint_id` disambiguates: iteration 7 is launched from a different checkpoint than
  iteration 2 (`ckpt_0031` vs `ckpt_0007`). Replay of iteration 7 resumes from the *same*
  `ckpt_0031` → same key. `checkpoint_id` is the one identifier that naturally distinguishes
  "running again on purpose" from "running again because we crashed."
- **`intra_node_index`** is the emitter counting emissions within the current node execution:
  first feed line → `#0`, second → `#1`.

**Worked example — node emits two events, worker dies, node replays:**

```
Attempt 1 (worker A): (seq  9, "ckpt_0031#tools#0")  (seq 10, "ckpt_0031#tools#1")
                       A dies before the post-node checkpoint.
B resumes FROM ckpt_0031 → node re-runs:
Attempt 2 (worker B): (seq 11, "ckpt_0031#tools#0")  (seq 12, "ckpt_0031#tools#1")
                       same keys, new seqs.
Reader dedups on idem_key → the user sees each line exactly once.
```

Exactly one of the two must change on replay (`seq`); the other must not (`idem_key`).

#### Where `checkpoint_id` comes from (verified against the venv)

Minted by LangGraph core — checkpoint creation in `langgraph.checkpoint.base` (ships in the
separate `langgraph-checkpoint` package). The id is `str(uuid6(clock_seq=-2))`; checkpoint ids
sort chronologically (the `Checkpoint` TypedDict docstring guarantees "unique and monotonically
increasing"). Our DynamoDB saver just persists it with every snapshot — we never generate or
manage it.

**Even better: LangGraph already computes our exact recipe — the `task_id`.** In
`langgraph/pregel/_algo.py`, every node execution is assigned a deterministic task id:

```python
task_id = task_id_func(
    checkpoint_id_bytes,   # the checkpoint the task was launched from
    checkpoint_ns,         # subgraph namespace (sub-agents isolated for free)
    str(step),             # super-step number
    name,                  # node name
    PULL, *triggers,       # or PUSH + index for Send()-spawned parallel tasks
)   # xxhash (ckpt v>1) or uuid5 — both deterministic
```

That is literally the recipe — "checkpoint the node was launched from + node + disambiguator" —
computed and maintained by the framework itself. It's what makes `put_writes` idempotent
internally: a crash-replay from the same checkpoint gets the same `task_id`, while a legitimate
next loop iteration (new checkpoint, new step) gets a different one. So the emitter shouldn't
hand-assemble the prefix at all:

```python
idem_key = f"{task_id}#{intra_node_index}"   # task_id from the debug stream event
```

Same guarantees, zero assembly logic to get wrong.

#### How to actually get the `task_id` (verified on the pinned LangGraph)

**From the emitter (outside the graph) — the debug stream. This is the primary path:**

```python
async for ev in graph.astream(None, config, stream_mode="debug"):
    if ev["type"] == "task":            # node execution STARTING
        task_id = ev["payload"]["id"]   # e.g. "3970197c-fd2e-2115-a675-7cb3c7088e44"
        node    = ev["payload"]["name"] # (payload keys: id, input, name, triggers)
    elif ev["type"] == "task_result":   # node execution FINISHED
        task_id = ev["payload"]["id"]   # same id (payload: id, name, result, error, interrupts)
    elif ev["type"] == "checkpoint":    # super-step committed
        ckpt_id = ev["payload"]["config"]["configurable"]["checkpoint_id"]
```

> `stream_mode` accepts a list — `stream_mode=["debug", "updates"]` — and each yielded item is
> tagged with its mode, if you need feed content *and* task ids.

**Inside a node/tool — from its own config.** The runtime embeds the task id in the
`checkpoint_ns` it hands every task:

```python
def my_node(state, config):
    ns = config["configurable"]["checkpoint_ns"]
    task_id = ns.rsplit(":", 1)[-1]   # "a:3970197c-fd2e-…" — LangGraph builds ns as
                                       # f"{node_name}:{task_id}" (_algo.py)
```

**Which to use?** The debug stream, as default and primary: the emitter already lives on the
stream, so the whole event-identity contract (`task_id` + `intra_node_index` + `seq`) is
assembled in one component, the run behaves identically whether anyone watches (projection
principle), and the intra-node counter gets clean lifecycle brackets (`task` start → count →
`task_result` end, per id, safe under parallel tasks). The in-node surface matters only for a
long-running tool that streams mid-execution progress via a custom stream writer — the debug
stream only brackets a node, it can't see inside.

> **One caveat:** `stream_mode="debug"` payload shapes and the `checkpoint_ns` encoding are
> internals, not a stable public contract across LangGraph versions. Pin the version, and add a
> small contract test (a line graph run in CI) that fails loudly on any upgrade that moves them.

### The fence — every event write carries a condition

```python
event_table.put_item(
    Item={"thread_id": tid, "seq": seq, "idem_key": key, "event": payload, "ttl": ttl},
    ConditionExpression="attribute_not_exists(thread_id)",
    ReturnValuesOnConditionCheckFailure="ALL_OLD",
)
```

**The subtlety that trips everyone:** DynamoDB first addresses the exact slot `(thread_id, seq)`
from the item's full primary key, then evaluates the condition against whatever single item
sits at that slot. Empty slot → condition trivially true → write. Occupied slot → the occupant
necessarily carries its own `thread_id` → condition fails. So testing one key attribute means
exactly "is this slot empty?" — it does not scan. Adding `attribute_not_exists(seq)` would be
redundant, not stronger. Condition + write are atomic server-side — no check-then-act gap.
(Python analogy: `INSERT` relying on a primary-key constraint, not `SELECT`-then-`INSERT`.)

### Step 4: The reader — SSE tail (API side)

The API container knows nothing of SQS, locks, or workers — it reads exactly one table:
`app-{env}-deep-events` (both the event partition and the `run#` status item).
`GET /deep/{thread_id}/stream`:

```python
cursor = int(request.headers.get("Last-Event-ID", 0))   # reconnect replays from here
first_pass = True
while True:
    items = event_table.query(
        KeyConditionExpression=Key("thread_id").eq(tid) & Key("seq").gt(cursor),
        ConsistentRead=first_pass,   # strong on catch-up (must not miss a fresh write);
    )                                # eventual on the steady tail (halves RCU)
    first_pass = False
    for item in dedup_by_idem_key(items):
        yield f'id: {item["seq"]}\ndata: {json.dumps(item["event"])}\n\n'
        cursor = item["seq"]

    run = get_run_state(tid)         # termination on STATUS, not a sentinel
    if run.status in ("failed", "completed"):
        yield terminal_frame(run.status, run.reason)
        return
    await asyncio.sleep(0.25)
```

Three design points:

- **Replay and live tail are the same query.** The browser's native `EventSource` resends the
  last `id:` it saw as `Last-Event-ID` on reconnect; it becomes the cursor. No separate history
  endpoint, no server state.
- **Termination on run status.** A crashed worker never writes a "done" event — if the only exit
  were a sentinel, the user would watch a spinner forever. The run-state record can be flipped by
  someone other than the worker (a detector), so it's the reliable terminator.
- **`dedup_by_idem_key`** makes at-least-once writes look exactly-once to the user.

### Step 5: Completion

Final report lands as events (large payloads → S3 object with a pointer in the event item;
DynamoDB caps items at 400 KB). Worker writes `status=completed`, releases the lock, deletes the
SQS message (delete-last ordering). The tail loop sees `completed`, emits the terminal frame,
closes.

---

## Part 3 — The lock, in complete detail

### Plain-words version — one continuous story

The lock answers one question: *"which process is allowed to write for thread `abc` right now?"*
It's a parking permit for a single spot — one item, at `PK="lock#abc"`, `SK=0`. The subtlety: it
identifies a specific **tenure**, not a specific worker. Every acquisition mints a brand-new
random string (`uuid4()`). Think of a hotel key card: check in, the desk programs a new card for
room 12; your name isn't on it. When the next guest checks in, the door is reprogrammed for
their card and your old card simply stops opening the door. Nobody has to find you and take it
away.

**t=0 — Worker A acquires.** A mints token `"aaa-111"` and does a conditional put whose
condition reads: *"write this only if no lock item exists at this slot, OR the one that exists
has already expired."* DynamoDB checks and writes in one atomic step, so exactly one winner:

```
PK="lock#abc"  SK=0  {owner: "aaa-111", expires_at: t+600}   ← A's tenure
```

**Why the lock must expire at all — the entire reason Renew exists.** Imagine no expiry: A
crashes at t=50 holding the lock forever. Every future worker finds a "live" lock and backs off
— the thread is wedged permanently until a human deletes a DynamoDB item. So the lease is
deliberately short (minutes) — but our runs take 45+ minutes, so a healthy holder must keep
pushing the expiry forward. That's Renew.

**t=0…40 — A renews on every checkpoint** (piggybacked on the `LeaseExtendingSaver.put()`
heartbeat, because a checkpoint proves both life and progress):

```python
update_item(
    Key={"thread_id": "lock#abc", "seq": 0},
    UpdateExpression="SET expires_at = :new",
    ConditionExpression="owner = :me",                       # the token guard
    ExpressionAttributeValues={":new": now+600, ":me": "aaa-111"},
)
```

**"Token-guarded"** means exactly that one condition line: every write to the lock says "only if
the lock still contains MY token." Not *"extend the lock for abc"* but *"extend it if it's still
my card in the door."*

**t=45 — A freezes** (GC pause, network partition — doesn't matter). No checkpoints → no renews.
`expires_at` quietly passes at t=55. **Nothing happens in the table at expiry** — no deletion,
no notification. Expiry only means the next acquirer's `expires_at < now` will now evaluate true.

**t=60 — Worker B takes over.** SQS redelivered (the visibility lease lapsed from the same
silence). B mints `"bbb-222"` and runs the same acquire put — the "already expired" half of the
condition is now true, so the put succeeds and overwrites the whole item:

```
PK="lock#abc"  SK=0  {owner: "bbb-222", expires_at: t+600}   ← B's tenure
```

That overwrite **is** the takeover. There is no separate "break A's lock" step, and A was not
consulted. Token `"aaa-111"` was never revoked anywhere — it just no longer matches the door.

**t=75 — the zombie wakes.** A unfreezes mid-run believing it still holds everything. The token
guard catches it at every possible move:

1. Its next checkpoint fires the renew → condition `owner = "aaa-111"` vs item `"bbb-222"` →
   `ConditionalCheckFailedException`. That failure is *information*: "your card no longer opens
   this door." A well-behaved worker aborts right here.
2. Suppose it ignores that and writes an event → **Part 4's fence.** A's counter says `seq 9`; B
   already wrote 9. The put on `(PK="abc", SK=9, "slot must be empty")` fails and returns the
   occupant → the three-way branch.
3. Suppose it instead finishes and tries to clean up → **release is also token-guarded.** Imagine
   release were an unconditional `delete_item`: the item it deletes is B's. The door is now
   unlocked while B is still mid-run; worker C acquires on the next redelivery, and B and C are
   both writing the same thread — the exact double-writer disaster the lock exists to prevent.
   The guard turns that into a harmless failed delete: you can only remove *your own* card.

### Formal reference — four operations

```python
# ACQUIRE (pickup step 2). Mint a fresh token, then a conditional put.
owner_token = str(uuid.uuid4())    # identity for THIS TENURE, not this worker
now = int(time.time())
event_table.put_item(
    Item={"thread_id": f"lock#{tid}", "seq": 0, "owner": owner_token,
          "expires_at": now + LEASE_SECONDS,   # app-checked expiry
          "ttl": now + 86400},                 # janitorial only (see E11)
    ConditionExpression="attribute_not_exists(thread_id) OR expires_at < :now",
    ExpressionAttributeValues={":now": now},
)
# Success → I hold the thread. Overwriting an expired lock IS the takeover; there is no separate
# "break the lock" step and the old holder's token dies with it.
# ConditionalCheckFailedException → a LIVE holder exists. Under FIFO this "should" never happen
# (one in-flight message per group), so treat it as a real anomaly: do NOT process; let the
# message return to the queue via visibility lapse.

# RENEW — piggybacked on every checkpoint, token-guarded so a zombie can't refresh a lost lock.
event_table.update_item(
    Key={"thread_id": f"lock#{tid}", "seq": 0},
    UpdateExpression="SET expires_at = :new",
    ConditionExpression="owner = :me",         # only MY lock
    ExpressionAttributeValues={":new": now + LEASE_SECONDS, ":me": owner_token},
)

# CHECK — done lazily, only on the failure path of an event write (Part 4). Healthy workers
# never poll the lock.

# RELEASE — token-guarded delete.
event_table.delete_item(
    Key={"thread_id": f"lock#{tid}", "seq": 0},
    ConditionExpression="owner = :me",
    ExpressionAttributeValues={":me": owner_token},
)
# Without the guard: a process that outlives its own lease would unconditionally delete the NEXT
# holder's lock on teardown — re-opening the exact double-writer window the lock exists to close.
```

**Why a lock at all when FIFO already serializes delivery:** FIFO stops *deliveries*, but after a
visibility lapse the message is no longer "in flight" — SQS happily delivers it to B while a
stalled A is still executing. The lock is the arbiter for that window, and the write fence
(Part 4) is the backstop when even the lock check races.

---

## Part 4 — Zombie fencing: the three-way branch

```
Worker A ── writes seq 9 … then FREEZES (no checkpoints, no extends, no lock renews)
SQS      ── lease lapses (30 min, no extension) → redeliver (ApproximateReceiveCount=2)
Worker B ── stamp run# receive_count=2
            lock# conditional put: "empty OR expires_at < now" → TAKEOVER, new owner token
            Query max seq → PutItem 9,10,11 …
Worker A ── UNFREEZES, still believes counter=8, lock=mine
            PutItem seq 9 → condition FAILS, ALL_OLD returned, occupant idem_key ≠ mine
            GetItem lock#abc → owner ≠ my token
            BRANCH 2: I am the zombie. Raise. Stop emitting entirely.
Worker B ── continues seq 12, 13 … log uncorrupted
```

On the collision, `ALL_OLD` returns the occupying item and A runs:

```python
except ClientError as e:   # ConditionalCheckFailed
    existing = deserialize(e.response["Item"])   # who's in my slot?

    # Branch 1 — the occupant IS the event I'm writing right now.
    if existing["idem_key"] == this_write.idem_key:
        return seq
        # my own earlier attempt landed (ACK lost, SDK retried) or an identical replay already
        # wrote it. Either way the LOGICAL event exists; the write is satisfied. Continue.

    # Foreign occupant → am I still allowed to write at all? Check the lock NOW (lazily — this
    # read is paid only on the rare collision path):
    lock = get_lock(tid)
    if lock is None or lock["owner"] != owner_token or lock["expires_at"] < now:
        # Branch 2 — I am the zombie. My lock was taken while I was frozen.
        raise ZombieWriter(tid)
        # stop emitting ENTIRELY. Never re-seed past the collision — that would weave my stale
        # events into the live log ABOVE the reader's cursor.

    # Branch 3 — foreign occupant but I legitimately hold the lock: a dead predecessor's
    # straggler write landed AFTER my seed read.
    seq = reseed_from_table()   # query max again, hop over the corpse's writes
    retry_bounded()             # transient by construction: a corpse has finitely many
                                # in-flight/SDK-queued writes to drain
```

**What "mine" means in branch 1:** not worker identity — `idem_key` carries none. It compares
the occupant's key to the key of the write in my hand. Because `idem_key` is deterministic (same
execution position → same key), an equal key means the logical event is already durable, and who
physically wrote it is irrelevant.

**Branch 2 needs no cooperation from the zombie:** it missed the lease lapse, the redelivery, the
lock takeover — every memo. But the very write that would have corrupted the log is the atomic
operation that reveals what it has become. **The storage layer is the final arbiter of who's
alive** (same idea as fencing tokens in distributed locks). This is also why a competing writer,
for us, is *never* legitimate — so we must fence them out rather than run an
increment-and-retry allocator that tolerates them.

**Branch 3 walkthrough (how "foreign key but my lock" arises at all):**

```
t=40.000  Dying worker Z fires PutItem(abc, 9); the request leaves its network stack.
          Z dies. The HTTP request is still in flight.
t=40.100  B seeds: ConsistentRead max — truthful AT THIS INSTANT.
t=40.150  Z's straggler ARRIVES at DynamoDB. Slot (abc, 9) empty → lands.
t=40.200  B writes seq 9 → collision. Foreign idem_key. Lock check: mine.
t=40.250  B re-seeds → 9. Writes at 10. Z is dead; twitches are finite. Done.
```

Z's orphaned event at seq 9 stays in the log — harmless: when B's resumed run re-executes that
node, its events carry the same `idem_key`, and the reader dedups.

---

## Part 5 — Edge case catalog

Each entry: what happens, how it's detected, how it's handled, which layer owns it.

- **E1. Double-clicked Approve / frontend retry / SDK retry of `send_message`.** Two independent
  layers: (a) the resume claim's conditional-put lease — two racing approves, exactly one wins,
  the loser gets `409 resume_in_progress`; (b) SQS content-based dedup — byte-identical bodies
  within a 5-minute window are silently dropped. Residual: a *legitimate* second run minutes
  later has a different `run_id` → different body hash → correctly enqueued, and FIFO makes it
  wait behind run 1.

- **E2. Two runs queued on one thread.** `MessageGroupId = thread_id` — SQS won't deliver message
  2 of a group while message 1 is in flight, even with idle workers. Transport guarantee, no app
  code.

- **E3. Worker crashes mid-node (clean death).** Detected by silence: no more checkpoints → no
  lease extensions → visibility timeout lapses → SQS redelivers (`ApproximateReceiveCount`++).
  Handled by the pickup ritual on the next worker: resume from the last checkpoint; the
  at-most-one incomplete node re-runs; per-task pending writes (`put_writes`) mean a parallel
  sibling that *did* finish is not re-executed. **Loss granularity = one node, never the run.**
  Cost: one duplicated LLM/tool call, one receive count.

- **E4. Worker stalls without dying (the zombie).** Detected by the fence: the zombie's next
  event write collides, branch 2 fires. The zombie raises and stops emitting; B already owns the
  thread. **The corruption attempt is the detection.**

- **E5. Dead predecessor's straggler write (branch 3).** Detected by collision with a foreign
  `idem_key` while the lock check says "mine." Re-seed above the new max, bounded retry.
  Transient by construction; the orphan event dedups on read.

- **E6. Poison pill — a message that kills every worker (3 strikes).** Detected by
  `maxReceiveCount=3` on the redrive policy, and app-side by stamping `receive_count` onto the
  run-state record. Two cooperating paths, either flips the run: (a) the worker seeing
  `receive_count == 3` knows it's the final permitted attempt and wraps execution — on failure it
  writes `status=failed, reason="Run failed after 3 attempts"`; (b) if it can't (hard crash), SQS
  moves the message to the DLQ and the DLQ-depth CloudWatch alarm (→ SNS) plus a DLQ
  consumer/detector performs the same flip. Within ~250 ms of the flip the user sees a reason,
  not an eternal spinner. **Why stamping matters:** SQS shows the receive count only to the
  process currently holding the message; durably copying it to run-state lets anyone (API,
  detector, human) reason about attempts afterward. Forensics: the DLQ retains the poison message
  14 days; console redrive sends it back after a fix.

- **E7. Redelivery of an already-completed run.** Worker finished everything but crashed between
  the terminal writes and `delete_message`; the message resurfaces. Delete-last ordering makes
  this the designed worst case: the next worker resumes from the final checkpoint; the graph is
  at `END`; nothing re-executes; the status write is idempotent (deterministic keys → overwrite,
  not duplicate). Total cost: one no-op receive.

- **E8. The lease-extension / lock-renew call itself fails (SQS throttle, network blip).**
  Fire-and-forget in the saver wrapper — log a warning, continue; the old countdown keeps
  running and the next checkpoint retries. Worst case: a premature redelivery — which E3 already
  handles. Never crash a healthy run.

- **E9. A single node runs longer than the visibility timeout.** Prevented by sizing: the 30-min
  floor must exceed the longest single node; the lease resets to a full window at every
  checkpoint, so run length is irrelevant. **Do not add mid-node heartbeats — they'd renew
  zombies.** Hard wall: SQS caps total invisibility at 12 hours per receive. A run needing more
  must checkpoint, delete the message, and enqueue a continuation.

- **E10. Event-table outage while a run is executing.** The projection principle: emitter writes
  fail → log, drop the feed lines, keep executing; checkpoints (different table) are untouched.
  The feed goes quiet; the run completes. An event-log outage never costs work.

- **E11. TTL vs a late reconnect.** Risk: an in-progress run's early events expire before a
  disconnected user reconnects and replays. Handled by sizing TTL ≫ max run + max reconnect gap
  (~24h), and treating TTL as janitorial only (DynamoDB TTL deletion lags up to ~48h and is
  best-effort, so no correctness logic may depend on it). Same rule for the lock's `ttl`: the
  enforced expiry is `expires_at`; `ttl` merely garbage-collects abandoned rows.

- **E12. Clock skew between workers (lock expiry disputes).** Margins, not precision:
  `expires_at` comparisons use writer-supplied epochs; lease durations (minutes) dwarf realistic
  skew (sub-second under NTP). And even under skew, the conditional write is the arbiter of fact.

- **E13. Deploy / ECS task recycling mid-run.** Same machinery as E3 — a recycled task is just a
  clean death. The dying worker owes zero cooperation: the checkpoint + the undeleted message
  reconstruct everything.

- **E14. Event payload exceeds DynamoDB's 400 KB item cap.** Offload — the final report, large
  token batches, chart blobs go to S3 under deterministic keys; the event item carries a pointer.
  (The checkpointer already does the same for state.)

- **E15. SSE client disconnects and reconnects.** The browser's native `EventSource` resends
  `Last-Event-ID`; the reader seeds its cursor from it; catch-up uses `ConsistentRead`, then the
  steady tail relaxes to eventual. The worker writes regardless of whether anyone is watching.

- **E16. Duplicate feed lines from any at-least-once path (E3/E4/E5/E7).** Read-side dedup on
  `idem_key` — the single mechanism that converts "at-least-once written" into "exactly-once
  seen." Write-side, branch 1 additionally short-circuits byte-identical own retries.

---

## Part 6 — FAQ (in the order people ask them)

**Q: Are the "events" the LangGraph checkpoints?** No. Two streams, two tables, two audiences.
Checkpoints: automatic, machine-readable, for resume. Events: our emitter's curated feed lines,
for the user. One node might produce 1 or many events.

**Q: Does DynamoDB auto-increment `seq`?** No — DynamoDB has no auto-increment. The worker seeds
once per receive (strongly-consistent `max` query) and increments a local variable. Safe because
there is one writer per thread; fenced for the moment there briefly isn't.

**Q: Does B start numbering from 0 after taking over?** No — it seeds from the table max and
continues (…8 → 9, 10), so the feed is one continuous log. Starting at 0 would collide with A's
item immediately, and the fence would reject it.

**Q: Is `attribute_not_exists(thread_id)` missing an `AND seq` check?** No. The condition is
evaluated against the single item at the exact `(PK, SK)` slot being written — it can only ever
mean "is this slot empty?" Checking one key attribute is enough; the second is redundant.

**Q: How does a worker know an `idem_key` is "its own" without worker identity in the key?** It
doesn't need authorship — it compares the occupant's key with the key of the write in its hand.
Deterministic keys: equality = the logical event exists, satisfied regardless of who performed
it. Worker identity lives in exactly one place: the lock's owner token.

**Q: Why isn't `seq` part of `idem_key`?** Because replay assigns new seqs — a key containing
`seq` would never match its replay, and dedup would be decorative. `idem_key` is built
exclusively from things identical on replay.

**Q: Why `checkpoint_id` in the key when we have `node_name`?** The agent node runs many times
legitimately (ReAct loop). `checkpoint_id` separates iteration 7 from iteration 2 (different
launch checkpoints) while equating iteration 7 with its own replay (same launch checkpoint).
It's the only ID with exactly that property.

**Q: Is lease extension additive, or based on how long the node took?** Neither —
`ChangeMessageVisibility` overwrites the remaining countdown with a fixed "N seconds from now."
No measurement, no adaptivity; the constant just has to exceed one node.

**Q: What is `ApproximateReceiveCount` and why "approximate"?** An SQS system attribute (request
it in `ReceiveMessage`) counting deliveries of this message, including the current one.
"Approximate" because SQS is distributed and delivery edge cases can skew it — which is why it
drives coarse decisions (3 strikes) and is stamped durably at pickup, never used as a precise
counter.

**Q: Who reads `Query(seq > cursor)`?** Only the API's SSE tail (and the same query serves
reconnect replay). The write path never range-queries; its single read is the seed
(`Limit=1`, descending).

---

## Appendix A — Parallel tool execution (support handoff)

**Who executes N `query_source(...)` calls side by side?** The framework.
`deepagents.create_deep_agent()` is built on `langchain.agents.create_agent()`, which uses
LangGraph's `ToolNode`. In the async path, `ToolNode` runs the tool coroutines with
`asyncio.gather(*coros)`. In the sync path it uses an executor map. **There is no custom thread
pool or manual thread spawning** in app code for parallel tool calls.

**How do parallel results merge without overwriting each other?** The `source_results` state
channel uses an `operator.add` reducer, so each tool's `Command(update={"source_results": [...]})`
*appends*. A separate `step_validation_failures` channel uses a dict-merge reducer.

**Does the deep agent use multithreading?** For the normal async worker/API path, no: the worker
calls `agent.astream_events(None, version="v2")` and parallel tool execution is
framework-managed async concurrency via `asyncio.gather` inside the tool node — concurrent I/O,
not app-authored threads. Threads still exist at infrastructure boundaries: FastAPI endpoints use
`asyncio.to_thread` for blocking DynamoDB/SQS/checkpointer operations so the event loop isn't
blocked; the sync tool path can use an executor map; ECS may run multiple API/worker tasks (with
per-thread safety protected by SQS FIFO grouping, run-state, and worker locks).

**Operational pattern for three parallel source calls:**

```
# Approved plan
step-1 [source: SOURCE_B] [group: 1 parallel]
step-2 [source: SOURCE_A] [group: 1 parallel]
step-3 [source: SOURCE_D] [group: 1 parallel]

# Model response after approval → one AI message with three tool_calls
query_source(source="SOURCE_B", step="step-1")
query_source(source="SOURCE_A", step="step-2")
query_source(source="SOURCE_D", step="step-3")

# Framework execution
LangGraph ToolNode parses all tool_calls from that one AI message.
Async path builds one coroutine per call → awaits asyncio.gather(*coros).

# App tool behavior (per call)
- validates plan/source/step
- creates a backend span "query_source: <agent_name>"
- stores one SourceResult with a provenance_id
- source_results reducer appends all results into one checkpointed state
```

In tracing this appears as one orchestrator/model span followed by multiple same-level
tool/backend spans with overlapping timestamps. If the model emits call 1, waits, then emits
calls 2 and 3 without needing call 1's output, that's a planning/model batching issue, not a
missing thread in app code.

---

## Appendix B — State, persistence, and stores (quick reference)

**Checkpoints.** `DynamoDBSaver` from `langgraph-checkpoint-aws`. Table
`app-{env}-deep-checkpoints`; `DEEP_CHECKPOINTER_BACKEND=dynamodb`; thread id = `chat_id`;
default TTL 30 days (`DEEP_CHECKPOINT_TTL_SECONDS`); large payloads offload to
`DEEP_CHECKPOINT_S3_BUCKET` under prefix `deep-checkpoints`. Misconfiguration *raises* for an
unknown backend; warns if the offload bucket is missing.

**Deep events and run state.** The `deep-events` table has two row families: event-feed rows
(`PK=chat_id`, `SK=seq number`, replayable curated SSE frames, TTL ~24h) and the run-state
singleton (`PK=run#{chat_id}`, `SK=seq`, latest async status `queued|running|retrying|failed|
completed`, no TTL). The event feed is a projection, not the execution source of truth — if an
append fails, the worker tries to write a warning frame and continues; the checkpoint owns graph
progress.

**SQS queue.** `app-{env}-deep-run.fifo`; `MessageGroupId = thread_id` serializes one worker per
chat thread; body is `{"thread_id", "run_id"}`. Queue URL resolution order:
`DEEP_RUN_QUEUE_URL`, then SSM `/app/{env}/sqs/deep-run/url`, then `GetQueueUrl`. In the API
task, `DEEP_RUN_QUEUE_URL` is load-bearing because the API role is send-only.

---

## Appendix C — API & SSE support contract

| Endpoint | Method | Purpose / support note |
|---|---|---|
| `/api/v1/chat/stream` (`mode="deep"`) | POST | Starts the proposal phase. Expected to close at `interrupt` with **no** terminal end frame while waiting for approval. |
| `/api/v1/deep/{thread_id}/plan/refine` | POST | Resume the gate with feedback. Streams an updated plan and another `interrupt`. |
| `/api/v1/deep/{thread_id}/decline` | POST | Decline the plan. No retrieval. Used as a misroute signal when an auto-router sent a bad deep plan. |
| `/api/v1/deep/{thread_id}/approve/async` | POST | Lock plan, write `queued` run-state, enqueue worker job. Returns `202`; execution output is **not** in this response. |
| `/api/v1/deep/{thread_id}/stream?cursor=` | GET | Replay/tail durable execution events. Uses `Last-Event-ID` or `?cursor=`. Terminal output comes from the event log plus run-state. |
| `/api/v1/deep/{thread_id}/run` | GET | Read async run status. Use on reload or stuck-run triage. |
