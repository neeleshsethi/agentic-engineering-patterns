# Durable Async Agent Runs

A long agent run should survive a worker crash, a deploy, and a client that closes its laptop. That means the run cannot live in one process's memory. It has to live in durable storage, and every process — the API, the worker, a failure detector — has to be able to reconstruct it from there.

This article covers what happens *after* a plan is approved: the run is handed to a background worker, executes for tens of minutes, streams progress to the browser, and can be resumed by a different worker if the first one dies. It assumes the vocabulary from the [glossary](00-glossary.md) — [checkpoint](00-glossary.md#checkpoint), [replica](00-glossary.md#replica), [lease](00-glossary.md#lease), [idempotency](00-glossary.md#idempotency) — and builds directly on [Distributed Locks](05-distributed-locks.md).

## Two Streams, Two Tables, Two Audiences

The single most useful decision is to separate the run's *truth* from the run's *narration*.

- **Checkpoints** are the graph's state, written automatically by the framework's saver after every step. They are machine-readable and exist for resume. This is the source of truth.
- **Events** are curated, human-readable feed lines written by your own code. They exist for the user's progress view. This is a projection.

```text
worker executes graph
  ├── saver.put()  -> checkpoints table   (state, for resume)
  └── emitter      -> events table         (feed lines, for the UI)
```

Keep them in separate tables. An outage of the events table then makes the feed go quiet while the run keeps executing — narration is allowed to fail, truth is not. If you conflate them, a feed write failure can take down a real run.

## One Worker Per Thread, As A Transport Guarantee

Two runs on the same conversation must not execute concurrently, or they corrupt each other's state. You can enforce this with application code, or you can push it down into the queue.

A FIFO queue with a per-thread group id will not deliver a second message for a group while one is still in flight. "One worker per thread" becomes a property of the transport, not a lock you maintain.

```text
approve -> enqueue {thread_id, run_id}, group_id = thread_id
queue delivers at most one in-flight message per group
different threads (groups) still run fully in parallel
```

Serialize only where correctness needs it (per thread), and keep parallelism everywhere else (across threads).

## Persist Intent Before You Enqueue

Order matters at the approval boundary. Write the durable state first, enqueue last.

```python
# 1. persist the approval into the checkpoint (durable)
graph.update_state(config, {"plan": approved_plan})

# 2. write a run-state record so the run exists even before a worker touches it
put_run_state(thread_id, status="queued", run_id=run_id)

# 3. enqueue a small pointer, not the state
send_message({"thread_id": thread_id, "run_id": run_id}, group_id=thread_id)
```

The reasoning: a lost message can be re-enqueued, but a lost approval cannot be reconstructed. The queue message is deliberately tiny — a pointer plus intent. The real state is already in the checkpoint. A double-clicked approve produces a byte-identical message, which content-based deduplication drops.

## Pickup Is A Ritual, Not A Function Call

When a worker receives a message, a fixed sequence runs *before* the graph does:

```text
1. stamp   copy the delivery/receive count onto the run-state record
2. lock    conditional-put a fresh lease token for this thread
3. seed    read max(sequence) for this thread's events
4. wrap    compile the graph with a lease-extending saver
5. renew   push the lease expiry forward
6. run     stream the graph
```

Step 1 matters because the queue shows the receive count only to the current holder of the message. Copying it into durable state is what lets anyone — the API, a detector, a human — reason about how many attempts a run has taken after the fact.

## Heartbeat On Progress, Not On A Clock

The worker must keep proving it is alive so the queue does not redeliver the message to a second worker. The wrong way is a wall-clock timer; it keeps a stuck-but-alive process looking healthy. The right way ties the heartbeat to the same event that proves progress: the checkpoint write.

```python
class LeaseExtendingSaver(BaseCheckpointSaver):
    """Every checkpoint write also proves liveness, in the same breath."""

    def __init__(self, inner, on_put):
        self._inner = inner
        self._on_put = on_put   # extend queue visibility + renew lock

    def put(self, config, checkpoint, metadata, new_versions):
        result = self._inner.put(config, checkpoint, metadata, new_versions)
        try:
            self._on_put()
        except Exception:
            logger.warning("heartbeat failed; will retry next checkpoint")
        return result   # fire and forget; a missed beat only risks an early redelivery
```

A checkpoint is simultaneously the *liveness* signal (a dead worker stops checkpointing) and the *progress* signal (state just became durable). Tying the heartbeat to it lets the queue reclaim jobs from crashes and hangs alike.

The size rule that falls out: the visibility timeout must exceed the longest *single* unit of work between checkpoints (one model call or one tool call), never the whole run. Do not add mid-unit heartbeats — they would keep a zombie alive.

## Sequence Number Versus Idempotency Key

The feed needs a total order and a dedup identity. Use two different values, because they have opposite requirements under replay.

- **Sequence** is a plain in-process counter, seeded once per pickup from the current max and incremented per emitted feed line. It gives ordering and becomes the reader's cursor. Gaps are allowed; ordering is guaranteed. On replay it takes new values.
- **[Idempotency key](00-glossary.md#idempotency-key)** identifies the *logical* event regardless of which attempt wrote it. It must be identical when a node is replayed, so it is derived from execution position — never from the sequence number, never random.

Step through a crash-and-replay to see why the two values need opposite behavior:

<div class="scrubber" data-scrubber markdown="0">
  <div class="scrubber-stage">
    <div class="scrubber-step">
      <span class="scrubber-time">Worker A · emits</span>
      <div class="scrubber-caption">A runs the "tools" node from checkpoint 31 and emits two feed lines. seq counts up; idem_key is derived from execution position.</div>
      <pre class="scrubber-item">seq 9   idem "ckpt31#tools#0"
seq 10  idem "ckpt31#tools#1"   <span class="ok">← user sees both</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Worker A · dies</span>
      <div class="scrubber-caption">A crashes before the post-node checkpoint. The queue will redeliver the message to another worker.</div>
      <pre class="scrubber-item">seq 9   idem "ckpt31#tools#0"
seq 10  idem "ckpt31#tools#1"   <span class="warn">← A gone, node not committed</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Worker B · resumes</span>
      <div class="scrubber-caption">B reloads checkpoint 31 and re-runs the same node. seq continues from the table max (new numbers) — but idem_key repeats, because execution position is identical.</div>
      <pre class="scrubber-item">seq 11  idem "ckpt31#tools#0"   <span class="warn">← same key, new seq</span>
seq 12  idem "ckpt31#tools#1"   <span class="warn">← same key, new seq</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Reader · dedups</span>
      <div class="scrubber-caption">The reader has already shown idem "ckpt31#tools#0" and "#1". It drops the repeats. The user sees each line exactly once.</div>
      <pre class="scrubber-item">shown: {ckpt31#tools#0, ckpt31#tools#1}
seq 11, 12 → <span class="bad">duplicate idem_key, skipped</span>   <span class="ok">✓ exactly-once</span></pre>
    </div>
  </div>
</div>

Exactly one of the two must change on replay (the [sequence](00-glossary.md#sequence-number-seq)) and exactly one must not (the key). Many frameworks already compute a deterministic per-execution task id; if so, use it and let the framework own the hard part.

## Replay And Live Tail Are The Same Query

The reader is deliberately dumb. It knows nothing about queues, locks, or workers. It reads one table.

```python
cursor = int(request.headers.get("Last-Event-ID", 0))
while True:
    items = events.query(PK=tid, seq_gt=cursor, consistent=first_pass)
    for item in dedup_by_idem_key(items):
        yield sse(id=item["seq"], data=item["event"])
        cursor = item["seq"]

    run = get_run_state(tid)
    if run.status in ("failed", "completed"):
        yield terminal_frame(run.status, run.reason)
        return
    await asyncio.sleep(0.25)
```

Three properties make this robust:

- **Reconnect is free.** The browser's native `EventSource` resends the last id it saw as `Last-Event-ID`. That becomes the cursor. Replay and live tail are one query; there is no separate history endpoint and no per-connection server state.
- **Termination is on status, not a sentinel.** A crashed worker never writes a "done" event. If the only exit were a sentinel event, the user would watch a spinner forever. The run-state record can be flipped to `failed` by a detector that is not the worker.
- **Dedup makes at-least-once look exactly-once.** Every duplicated feed line from a redelivery collapses on the idempotency key at read time.

See [SSE Cancellation](./04-sse-cancellation.md) for what happens to server-side work when that client connection closes.

## Crash Recovery Costs One Unit Of Work

The failure model is small on purpose. When a worker dies mid-run, the silence stops the heartbeat, the queue's visibility timeout lapses, and the message is redelivered with an incremented receive count. The next worker runs the same pickup ritual and resumes from the last checkpoint.

```text
loss granularity = one node, never the whole run
cost of a crash   = one duplicated model/tool call, one receive consumed
```

Per-node pending writes mean a parallel sibling that already finished is not re-executed. The at-most-one incomplete node re-runs, and its replayed events dedup on the reader side.

For the ownership and zombie-fencing mechanics that keep two workers from writing the same thread, see [Distributed Locks](./05-distributed-locks.md).

## Poison Pills Page A Human, Nothing Else Does

A message that kills every worker must not retry forever. Cap the delivery count. On the final permitted attempt, the worker wraps execution and, on failure, flips the run-state record to `failed` with a user-visible reason. If it cannot (a hard crash), the queue moves the message to a dead-letter queue, and a depth alarm on that queue is the one and only path that pages an operator.

```text
3 strikes -> dead-letter queue -> depth alarm -> ops
```

Everything else in this design recovers on its own. The dead-letter queue is the deliberate exception: it is where automation stops and a human starts.

## Guardrails

- Keep truth (checkpoints) and narration (events) in separate tables
- Persist approval and run-state before enqueuing; enqueue only a pointer
- Heartbeat on progress writes, not on a wall clock
- Size visibility timeouts from the longest single unit of work, not the whole run
- Derive dedup identity from execution position, never from the ordering counter
- Terminate streams on durable status, never on a sentinel event a crash can skip
- Reserve paging for the dead-letter path; let every other failure self-recover
