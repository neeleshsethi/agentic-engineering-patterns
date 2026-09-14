# Deep Dive · The Event Feed and Idempotency

The event feed is the live activity log the user watches while a deep run executes: *Querying subnational… · SUBNATIONAL 412 rows · Querying CRP… · Report ready.* It is a projection of the run, streamed to the browser over Server-Sent Events. This deep dive is the companion to [Step 7 · Queue and worker execution](08-queue-and-worker-execution.md): that chapter gets the run *onto* a worker; this one is about making the frames it emits appear **exactly once**, even though two independent mechanisms conspire to write them twice.

The safe mental model is:

> The feed is written at-least-once and read exactly-once. Ordering is a write-time counter; identity is a replay-stable key. Correctness only ever leans on the identity.

## Two Sources Of Duplicates

Nothing here is hypothetical — the feed must survive both of these, and they compound:

- **At-least-once delivery.** The run message rides an SQS FIFO queue. If a worker stalls past the visibility timeout or dies before deleting the message, SQS redelivers it to another worker. The whole run — and its feed writes — happen again.
- **Crash-replay inside a run.** When a worker resumes from the last checkpoint, any node that had started but not yet committed **re-executes**, re-emitting the feed lines it already emitted before the crash.

A naive feed — "append a row per event" — double-counts under both. The design answer is to give every row two values with deliberately opposite behavior under replay.

## Two Values, Opposite Jobs

The `deep-events` table is the one place a sequence number is a real, writer-allocated counter (DynamoDB has no auto-increment). Every row carries both a `seq` and an `idem_key`:

| Value | Question it answers | Behavior on replay | Derivation |
|---|---|---|---|
| `seq` | *In what order?* | **New every write** | In-process counter, seeded once per pickup from the partition's current max, incremented per emitted line |
| `idem_key` | *Have I shown this already?* | **Identical** | `"{task_id}#{n}"` — from execution position, never from `seq`, never random |

`seq` is also the reader's cursor and the browser's `Last-Event-ID`. Gaps are fine; monotonicity is not. `idem_key` is the dedup identity. The entire scheme rests on the fact that exactly one of the two changes on replay and exactly one does not.

## How `idem_key` Is Replay-Stable

`idem_key = "{task_id}#{n}"`:

- **`task_id`** identifies one node executing in one superstep. A parallel fan-out — three retrievals in a single superstep — has three tasks and three `task_id`s. The curator does not hash anything: it parses `task_id` out of each streamed event's checkpoint namespace, which LangGraph shapes as `"{node}:{task_id}"`. Reading it per-event means there is no "which task is currently open" bookkeeping, so parallel tasks can never misattribute an emission.
- **`n`** is a per-`task_id` counter of the events that task has emitted — `#0`, `#1`, `#2` — reset fresh each time the keyer is constructed.

The subtlety that makes it work is *which* checkpoint the id keys on. `task_id` is derived from the checkpoint the node **started from** (its input), not the one it writes:

- **Crash-replay** — the same run resumes and re-runs the uncommitted node **from the same input checkpoint it started from before the crash** → **same `task_id`** → same `idem_key`. This is what makes replay idempotent.
- **Legitimate re-iteration** — the loop genuinely runs the same node again, but now from a **newer input checkpoint** → **different `task_id`** → correctly counted as new events.

So the answer to "doesn't the checkpoint change when the node re-runs?" is: on a replay the *input* checkpoint is the same (that is the whole point); only a real loop iteration advances it. `task_id` is precisely the signal that separates the two.

## The Writer: A Conditional-Put Fence

Every append is a fenced write at the exact `(thread_id, seq)` slot:

```python
event_table.put_item(
    Item={"thread_id": tid, "seq": seq, "idem_key": key, "event": payload, "ttl": ttl},
    ConditionExpression="attribute_not_exists(thread_id)",   # "is this slot empty?"
    ReturnValuesOnConditionCheckFailure="ALL_OLD",
)
```

The condition tests one key attribute but means "is this slot empty?" — DynamoDB first addresses the exact item by its full primary key, then evaluates the condition against whatever single item sits there. An empty slot passes trivially; an occupied slot necessarily carries its own `thread_id`, so the condition fails. `seq` is seeded once per pickup and incremented **in memory** — safe as a local only because of the single-writer-per-thread invariant (one FIFO message group per thread, plus the [worker lock](07-distributed-locks.md)). When the fence does fail, a three-way branch decides what the collision means:

| Collision | Meaning | Action |
|---|---|---|
| occupant's `idem_key` == mine | my own earlier attempt already landed (lost ACK + SDK retry, or a replay hitting the same slot) | treat as success, advance, continue |
| foreign occupant, my lock lost/expired | I am a zombie; a new worker owns the thread | raise `ZombieWriterError`, stop emitting — never weave stale events above the reader's cursor |
| foreign occupant, my lock still held | a dead predecessor's straggler write landed after my seed read | re-seed above the new max, bounded retry |

Critically, on a clean redelivery the new worker re-seeds `seq` **above** the current max, so its replayed events get *fresh* seqs — they do not collide with the old rows at all. The fence catches only the genuine same-slot races above. Dedup of the ordinary replay case is the reader's job.

## The Reader: Dumb On Purpose

The reader is the API-side SSE generator for `GET /deep/{thread_id}/stream`. It knows nothing about queues, locks, or workers; it polls one table and forwards frames.

```python
cursor = int(request.headers.get("Last-Event-ID", 0))
seen: set[str] = set()               # per-connection, in API RAM — not in the table
while True:
    items = event_table.query(PK=tid, seq_gt=cursor, consistent=first_pass)
    for item in items:
        cursor = item["seq"]                 # advance even on a skipped row
        if item["idem_key"] in seen:
            continue
        seen.add(item["idem_key"])
        yield sse(id=item["seq"], data=item["event"])

    run = get_run_state(tid)
    if run.status in ("failed", "completed"):
        yield terminal_frame(run.status, run.reason)
        return
    await asyncio.sleep(0.25)
```

Two cooperating mechanisms remove duplicates, and it is worth being precise about which does what:

- **The cursor** (persisted in the browser as `Last-Event-ID`) skips already-seen rows **across reconnects**. A fresh `EventSource` resumes from the last `seq` the browser saw, so replay and live tail are one query — there is no separate history endpoint.
- **The `seen` set** (idempotency keys held per connection, in the API process's RAM) skips crash-replay duplicates that land **ahead of the cursor within one live connection**. Those re-emitted lines get *new* `seq` numbers, so the cursor alone cannot catch them; only the repeated `idem_key` can. Because the cursor advances even on a skipped row, a mid-stream reconnect pages *past* the duplicates rather than re-examining them.

The durable table deliberately keeps the duplicate rows. Dedup is a presentation concern done live at read time and thrown away when the connection closes — the database never has to remember "what have I already shown this user."

Step through a crash-and-replay to see the writer climb while the reader collapses:

<div class="scrubber" data-scrubber markdown="0">
  <div class="scrubber-stage">
    <div class="scrubber-step">
      <span class="scrubber-time">Attempt 1 · writes</span>
      <div class="scrubber-caption">Node A then node B emit four feed lines. seq climbs; each line's idem_key is its execution position.</div>
      <pre class="scrubber-item">seq 1  taskA#0  Querying subnational...
seq 2  taskA#1  SUBNATIONAL 412 rows
seq 3  taskB#0  Querying CRP...
seq 4  taskB#1  CRP 88 rows          <span class="warn">← crash before commit</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Attempt 2 · replays</span>
      <div class="scrubber-caption">A new worker resumes and re-runs node B from the same input checkpoint. seq is re-seeded above the max, so the replayed lines get NEW seqs — but the same idem_keys.</div>
      <pre class="scrubber-item">seq 5  taskB#0  Querying CRP...       <span class="warn">← duplicate content, new seq</span>
seq 6  taskB#1  CRP 88 rows          <span class="warn">← duplicate content, new seq</span>
seq 7  taskC#0  Report ready</pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Reader · collapses</span>
      <div class="scrubber-caption">seen already holds taskB#0 and taskB#1. seq 5 and 6 are skipped; the cursor still advances past them. The user sees each line once.</div>
      <pre class="scrubber-item">seen: {taskA#0, taskA#1, taskB#0, taskB#1}
seq 5 taskB#0 → <span class="bad">skip</span>   seq 6 taskB#1 → <span class="bad">skip</span>
seq 7 taskC#0 → <span class="ok">show ✓ exactly-once</span></pre>
    </div>
  </div>
</div>

## One Stream Must Not Poison Another's Keys

There is a sharp edge in `n`, the per-task counter, that is easy to get wrong. If a single node emits **two interleaved kinds of frame with different reproducibility guarantees**, they must count under **separate** namespaces.

In this system one node calls a downstream reasoning model (Cortex) and streams two kinds of frame at once:

- **Live "thinking" deltas** — the panel filling token by token. Their *count* is non-reproducible: a replay re-calls the model and gets a different number of tokens, plus wall-clock coalescing variance.
- **Deterministic frames** — a terminal thinking summary, a step status, a plan-tick checkmark. These *must* reproduce, because the reader dedups them by `idem_key`.

Put both on one counter and the noisy stream shifts the quiet one's indices:

| Shared counter | Attempt 1 (5 deltas) | Attempt 2 replay (3 deltas) |
|---|---|---|
| `#0`–`#2` | live delta | live delta |
| `#3` | live delta | **status** ← |
| `#4` | live delta | **plan tick** ← |
| `#5` | **status** ← | |
| `#6` | **plan tick** ← | |

The status frame is `taskB#5` on attempt 1 but `taskB#3` on the replay → a different `idem_key` → the reader cannot tell it is a repeat → the checkmark renders twice. The noisy stream poisoned the quiet stream's keys.

The fix is to quarantine the non-reproducible frames under their own suffix (`"{task_id}~cs"`), so the deterministic frames keep counting from `#0` regardless of how many live deltas flew by:

| Split counters | Attempt 1 (5 deltas) | Attempt 2 replay (3 deltas) |
|---|---|---|
| `taskB~cs#…` | live deltas `~cs#0…#4` | live deltas `~cs#0…#2` |
| `taskB#0` | status ✓ | status ✓ |
| `taskB#1` | plan tick ✓ | plan tick ✓ |

Now `status = taskB#0` on both runs, regardless of delta count → the key reproduces → dedup works. The live deltas themselves may re-show after a replay, but they carry replace-semantics (each frame holds the full accumulated text so far), so the panel simply re-fills — harmless.

The rule generalizes past this one source: **a counter-derived idempotency key may only count events whose count is itself replay-stable.** Anything non-reproducible gets its own bucket.

## What Actually Rides The Wire

The reader emits `StreamEnvelope` frames. Each SSE message is an `id:` line (the DynamoDB `seq`, which the browser echoes back as `Last-Event-ID`) plus a `data:` line (the JSON envelope), blank-line terminated. Two things are worth internalizing:

- **`idem_key` does not ride the wire.** It is the event log's internal dedup name; the reader strips it after deduping. It is never something the browser has to understand.
- **Live thinking deltas are `artifact` frames with `operation: "replace"`**, each carrying the full accumulated text so far. That is exactly why re-showing one after a replay is harmless.

There are eight wire types — `status`, `content`, `artifact`, `warning`, `error`, `end`, plus the deep-mode-additive `plan` and `interrupt`. A worked pair of attempts:

```text
# Attempt 1 — one Cortex call (taskB) streams 3 live deltas, then 2 deterministic frames
id: 43
data: {"type":"artifact","data":{"artifact":"cortex_thinking","operation":"replace",
       "value":["...final: retail, non-retail, and CRP channels."]}}   ← taskB~cs#2 (live, terminal)
id: 44
data: {"type":"status","data":{"message":"SUBNATIONAL returned 412 rows","state":"completed"}}  ← taskB#0
id: 45
data: {"type":"plan","data":{"plan_id":"plan-abc-1","steps":[{"id":"step-1","status":"done"}]}}  ← taskB#1

# Attempt 2 — crash, replay; the node re-runs and Cortex streams only 2 deltas this time
id: 58 ...artifact... ← taskB~cs#0 (live, NEW seq)
id: 59 ...artifact... ← taskB~cs#1 (live, NEW seq)
id: 60 {"type":"status",...}  ← taskB#0  (SAME idem_key as seq 44)
id: 61 {"type":"plan",...}    ← taskB#1  (SAME idem_key as seq 45)
```

The reader's `seen` already holds `taskB#0` and `taskB#1` from attempt 1, so seq 60 and 61 are skipped — the status is not re-shown and the checkmark is not re-rendered — even though the live stream's length changed from 3 deltas to 2. That is the entire `~cs` payoff in one trace.

## Three Idempotency Layers, Three Keys

Event-feed idempotency is one of three layers, each guarding a different artifact against replay. Do not conflate them:

| Layer | Key / mechanism | Guards against |
|---|---|---|
| Event feed (this chapter) | `idem_key` + reader dedup | crash-replay re-emitting the same activity line |
| Report emission | `reported_cycle == cycle_stamp` | redelivery re-writing or double-reporting a report |
| Checkpoint / state | deterministic `plan_id` / `provenance_id` | a replayed branch forking new state instead of rebuilding |

All three rest on the same principle, which is the one worth carrying out of this series: **identity is deterministic** (`idem_key`, `cycle_stamp`, `plan_id`, `provenance_id` are pure functions of their inputs), **physical placement is not** (`seq`, retry timing), and correctness only ever relies on the deterministic half. See [Identifiers](05-identifiers.md) for how the same rule shapes every id in the run.

## Guardrails

- **Never dedup on the ordering value.** `seq` is new on every write by design. If you find code treating it as an identity, that is the bug.
- **Derive the identity from execution position, never from wall-clock or random.** The whole scheme fails the moment `idem_key` stops reproducing on replay.
- **Keep the durable log duplicate-tolerant.** Dedup lives at read time, per connection. The table's job is to be an append-only, fenced, ordered log — nothing more.
- **Isolate any stream whose event count is non-reproducible.** Give it its own counter namespace so it cannot shift a deterministic stream's keys.
- **Terminate on run status, not on a sentinel event.** A crashed worker never writes "done"; a detector flips run-state to `failed` so the reader can end the stream.
