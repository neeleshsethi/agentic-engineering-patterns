---
summary: "Move approved work out of the API process and make replay safe."
---

# Step 7 · Queue and worker execution

> **Design question:** [What happens when it crashes halfway?](ch2-designing-a-deep-agent.md#crash-halfway) · **Principles:** Make Operations Idempotent, Separate Before Serializing Shared State

!!! note "Before you read"
    This page assembles pieces taught earlier. Skim the linked section first if any is hazy.

    - **The staged resume decision.** Approval is written into the checkpoint before anything is queued, so a worker can resume with no input. Shown end to end in [Chapter 3](ch3-architecture-and-flow.md), built in [Step 3](step-3-planning-and-human-approval.md).
    - **SQS FIFO behavior.** Message groups, the visibility timeout, and redelivery after silence. Taught in the [DynamoDB and SQS bridge](bridge-dynamodb-and-sqs.md).
    - **The lease and the zombie.** A lock that expires unless renewed, and a frozen worker that wakes after losing it. Taught in [Step 6](step-6-distributed-locks.md).

A long agent run should survive a worker crash, a deploy, and a client that closes its laptop. That means the run cannot live in one process's memory. It has to live in durable storage, and every process (the API, the worker, a failure detector) has to be able to reconstruct it from there.

This article covers what happens *after* a plan is approved: the run is handed to a background worker, executes for tens of minutes, streams progress to the browser, and can be resumed by a different worker if the first one dies. It assumes the vocabulary from the [Glossary](glossary.md) ([checkpoint](glossary.md#checkpoint), [replica](glossary.md#replica), [lease](glossary.md#lease), [idempotency](glossary.md#idempotency)) and builds directly on [Step 6 · Distributed locks](step-6-distributed-locks.md).

## Two Streams, Two Tables, Two Audiences

Start by separating the run's *truth* from its *narration*.

- **Checkpoints** are the graph's state, written automatically by the framework's saver after every step. They are machine-readable and exist for resume. This is the source of truth.
- **Events** are curated, human-readable feed lines written by your own code. They exist for the user's progress view. This is a projection.

```text
worker executes graph
  ├── saver.put()  -> checkpoints table   (state, for resume)
  └── emitter      -> events table         (feed lines, for the UI)
```

Keep them in separate tables. An outage of the events table then makes the feed go quiet while the run keeps executing. Narration is allowed to fail; truth is not. If you conflate them, a feed write failure can take down a real run.

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
# 1. persist the approval decision into the checkpoint (durable)
persist_resume_decision(thread_id, {"type": "approve"})

# 2. write a run-state record so the run exists even before a worker touches it
put_run_state(thread_id, status="queued", run_id=run_id)

# 3. enqueue a small pointer, not the state
send_message({"thread_id": thread_id, "run_id": run_id}, group_id=thread_id)
```

A lost message can be re-enqueued, but a lost approval cannot be reconstructed. The queue message is deliberately tiny: a pointer plus intent. The real state, including the pending resume decision, is already in the checkpoint. A double-clicked approve produces a byte-identical message, which content-based deduplication drops.

The approval decision is not written as a normal state field. It is staged into LangGraph's pending-writes area, so the paused `interrupt()` can consume it later.

## What Is The Run-State Record?

Before the pickup ritual makes sense, you need to know what the run-state record is.

It is a single row in the database (one row per run attempt). Think of it as a **status board** for the run that every part of the system can read:

```text
┌─────────────────────────────────────────────────────────────┐
│  RUN-STATE RECORD                                           │
│                                                             │
│  thread_id   = "chat-abc"    ← which conversation          │
│  run_id      = "run-001"     ← which attempt               │
│  status      = "running"     ← current lifecycle state     │
│  receive_count = 2           ← how many times SQS retried  │
│  reason      = null          ← filled on failure           │
└─────────────────────────────────────────────────────────────┘
```

The status field moves through a simple lifecycle:

```text
Approval happens
      │
      ▼
  "queued"   ← API writes this when it enqueues the work
      │
      │  Worker picks up the SQS message
      ▼
  "running"  ← Worker writes this on first attempt
  "retrying" ← Worker writes this on attempt 2, 3...
      │
      │  Work finishes
      ▼
  "completed"  or  "failed"  ← Worker writes this last
```

Three parts of the system read this record:

- **The SSE tail** — checks status on every poll. When it sees `completed` or `failed`, it sends a final event to the browser and closes the stream. Without this, the browser would spin forever after a crash.
- **The worker** — reads `receive_count` to know if this is a retry and writes the new status on pickup.
- **A failure detector** — can flip status to `failed` even if the worker crashes before it can do so itself.

## Pickup Is A Ritual, Not A Function Call

When a worker receives a message, a fixed sequence runs *before* the graph does:

```text
1. stamp   copy the delivery/receive count onto the run-state record
2. lock    conditional-put a fresh lease token for this thread
3. seed    read max(sequence) for this thread's events
4. wrap    compile the graph with a lease-extending saver
5. renew   push the lease expiry forward
6. run     call agent.astream_events(None, config)
```

Step 1 matters because the queue shows the receive count only to the current holder of the message. Copying it into durable state lets anyone (the API, a detector, a human) reason about how many attempts a run has taken after the fact.

Step 6 matters because `None` is the correctness signal. The worker is not inventing a new input, and it is not receiving approval from SQS. The approval was already staged in the checkpoint before enqueue. The worker reloads the checkpoint, LangGraph drains the pending resume value, and execution continues.

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

The size rule that falls out: the visibility timeout must exceed the longest *single* unit of work between checkpoints (one model call or one tool call), never the whole run. Do not add mid-unit heartbeats; they would keep a zombie alive.

## Sequence Number Versus Idempotency Key

Every feed line carries two values with opposite behavior under replay. `seq` is a counter, seeded once per pickup from the table's current max, that orders the feed and serves as the reader's cursor. A replay gives it new values. The [idempotency key](glossary.md#idempotency-key) names the logical event and comes out identical on replay, so the reader can drop the repeat. [Step 7a · The event feed and idempotency](step-7a-event-feed-and-idempotency.md) is the full treatment: how the key is derived from LangGraph's `task_id`, why one stream's noisy frames must not shift another's keys, and how the writer and reader use both values.

## Replay And Live Tail Are The Same Query

The reader is deliberately dumb. It knows nothing about queues, locks, or workers. It queries one table for rows whose `seq` is above the browser's `Last-Event-ID`, so a reconnect and a live tail are the same query, and it skips any idempotency key it has already sent on this connection. [Step 7a](step-7a-event-feed-and-idempotency.md#the-reader-dumb-on-purpose) walks through the code.

Two properties matter for the worker:

- **Termination is on status, not a sentinel.** A crashed worker never writes a "done" event. If the only exit were a sentinel event, the user would watch a spinner forever. The run-state record can be flipped to `failed` by a detector that is not the worker.
- **Dedup makes at-least-once look exactly-once.** Every duplicated feed line from a redelivery collapses on the idempotency key at read time.

See [Step 5 · Streaming and background work](./step-5-streaming-and-background-work.md) for what happens to server-side work when that client connection closes.

## Crash Recovery Costs One Unit Of Work

The failure model is small on purpose. When a worker dies mid-run, the silence stops the heartbeat, the queue's visibility timeout lapses, and the message is redelivered with an incremented receive count. The next worker runs the same pickup ritual and resumes from the last checkpoint.

```text
loss granularity = one node, never the whole run
cost of a crash   = one duplicated model/tool call, one receive consumed
```

Per-node pending writes mean a parallel sibling that already finished is not re-executed. The at-most-one incomplete node re-runs, and its replayed events dedup on the reader side.

For the ownership and zombie-fencing mechanics that keep two workers from writing the same thread, see [Step 6 · Distributed locks](./step-6-distributed-locks.md).

## Poison Pills Page A Human, Nothing Else Does

A message that kills every worker must not retry forever. Cap the delivery count. On the final permitted attempt, the worker wraps execution and, on failure, flips the run-state record to `failed` with a user-visible reason. If it cannot (a hard crash), the queue moves the message to a dead-letter queue, and a depth alarm on that queue is the only path that pages an operator.

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

---

!!! check "You should now understand"
    - Why approved work should be persisted before it is enqueued
    - Why checkpoints are truth and event streams are narration
    - Why the worker pickup sequence is a ritual, not a casual function call
    - Why heartbeat should follow checkpoint progress instead of a wall clock
    - Why replay needs both `seq` for order and idempotency keys for deduplication

??? question "Try this"
    **A worker emits progress lines, crashes before checkpointing the node, and another worker replays that node. What should the user see?**

    ??? success "Answer"
        The user should see each logical progress line once. The replayed events get new `seq` values because they are new stream records, but the same idempotency keys because they represent the same logical writes. The reader deduplicates by idempotency key and keeps `seq` only for ordering and cursor movement.

*Next: [Step 7a · The event feed and idempotency](step-7a-event-feed-and-idempotency.md) — the run now survives a crash; make sure its progress frames still appear exactly once.*
