# Glossary

Every term the series uses, defined once. Each article links here the first time it uses one of these words. If a passage ever loses you, this is the page to keep open beside it.

The definitions are ordered roughly from "things a normal web engineer already knows" to "things specific to agent systems."

---

## Web and infrastructure terms

### Replica
One of several identical copies of your API server running at once, behind a load balancer. Two requests from the same user can land on two different replicas. Anything one replica keeps in its own memory is invisible to the others — which is why shared state has to live in a database, not in a Python variable.

### SSE (Server-Sent Events)
A long-lived HTTP response where the server keeps sending small text frames over time instead of one final body. It is how an agent streams progress ("planning…", "querying…") to a browser. The browser's built-in `EventSource` object consumes it. See [SSE Cancellation](06-streaming-and-background-work.md).

### Last-Event-ID
A header the browser's `EventSource` automatically re-sends when it reconnects after a dropped connection. It contains the `id:` of the last frame the browser received. The server uses it as a cursor to resume the stream exactly where it left off.

### FIFO queue
A "first-in, first-out" message queue. Messages are delivered in order. The variant used here also guarantees that only one message per **group** is being processed at a time — which is how "one worker per conversation" becomes a property of the queue instead of code you write.

### Visibility timeout
When a worker picks up a queue message, the queue hides it from other workers for a set time. If the worker finishes and deletes the message, it is gone. If the worker dies and the timeout lapses, the queue makes the message visible again and another worker gets it. This is the mechanism that redelivers work after a crash.

### DLQ (dead-letter queue)
A separate queue where a message lands after it has failed too many times ("poison pill"). Instead of retrying forever, the system parks it here and raises an alarm so a human can look. In this design it is the *only* thing that pages a person.

### TTL (time-to-live)
An expiry timestamp on a database row. A background sweeper deletes rows past their TTL, eventually. It is janitorial cleanup, not a precise timer — deletion can lag by hours, so correctness must never depend on it.

---

## DynamoDB terms

### Conditional write
A database write that only succeeds if a condition is true at the moment of writing. The check and the write happen together, atomically, with no gap for a second writer to sneak in. This is the single most important primitive in the series — it is how one winner is picked out of a race.

### `attribute_not_exists(PK)`
The condition "only write this if there is no item with this primary key yet." In plain English: *"only if this slot is empty."* For a lock it means "acquire only if nobody holds it." See [Distributed Locks](07-distributed-locks.md).

### ConditionExpression
The DynamoDB parameter that carries a conditional write's condition. When the condition is false, DynamoDB rejects the write with a `ConditionalCheckFailedException` — which your code treats as the normal "someone else got there first" signal, not an error.

### PK / SK
Partition key and sort key — the two parts of a DynamoDB item's address. `PK` groups related items together; `SK` orders them within the group. One table can hold several *kinds* of item by giving them different `PK` prefixes (for example `run#abc` vs `lock#abc`).

---

## Locking terms

### Lock
A rule enforced through data: "only one process may act on X at a time." Here a lock is not a language primitive; it is a row in a database that every replica can see.

### Claim
This series' word for a lock used to guard a resumable run. Before running, a request writes a small marker — the claim — that says "I am running this thread; hands off." If the marker already exists and is still live, another request got there first. Same idea as a lock, named for what it does.

### Lease
The claim's expiry. A claim is deliberately short-lived so that if its holder crashes, the thread is not wedged forever. A healthy holder keeps pushing the expiry forward while it works ("renewing the lease"). See [Distributed Locks](07-distributed-locks.md#the-lease-must-be-short-the-work-is-long).

### Token (ownership token / claim_token) { #token }
A fresh random string minted each time a claim is acquired. It identifies *this specific tenure*, not the worker. Every write to the claim is guarded by "only if the stored token still equals mine," so a process that lost the claim can neither renew nor delete it. Think of a hotel key card: reprogramming the door for a new guest silently invalidates the old card.

### Fencing / write fence { #fencing }
A backstop for when even a lock check races. Every real data write is made conditional on a slot only the rightful owner can fill. A stale process ("zombie") collides on its very next write — so the corruption attempt is itself the detection. See [Distributed Locks](07-distributed-locks.md#the-write-fence-when-even-the-lock-check-races).

### Zombie
A worker that froze (a long pause, a network partition) without dying, lost its claim to a replacement, then woke up still believing it owns the thread. The write fence exists to catch it.

---

## LangGraph terms

LangGraph is the framework that runs the agent as a graph of steps. These terms are how it models a run.

### Node
One step in the graph — a function that reads the current state and returns an update to it. An agent's "call the model" and "run a tool" are nodes.

### State
The dictionary the graph reads and writes as it runs. It holds named **channels** like `messages`, `plan`, and `raw_question`. See [LangGraph State](02-state-and-checkpoints.md).

### Channel
One key in the state, with a rule for what happens when a node writes it. The two rules that matter: **LastValue** (a new write replaces the old value) and **reducer** (a new write combines with the old value, e.g. appending to a message list).

### LastValue (the footgun)
The default channel rule: the latest write wins. The trap: *not writing* a key does not clear it — it keeps whatever the last checkpoint held. So a new turn that forgets to overwrite a channel silently inherits the previous turn's value.

### Checkpoint
The saved copy of the graph's state at a point in time, plus LangGraph's bookkeeping. It lives in a database, so a later HTTP request — even on a different [replica](#replica) — can reload it and continue. It is the run's source of truth.

### Checkpointer / saver
The component that writes checkpoints. In this series it writes to DynamoDB. You rarely call it directly; LangGraph calls it automatically after each step.

### Super-step
One "tick" of the graph: the batch of nodes that run together before the next checkpoint is written. Checkpoints happen per super-step.

### `interrupt()`
The one primitive for pausing a graph to wait for a human. It saves state to the checkpoint and raises a special exception; the HTTP response returns with whatever streamed so far, and the graph freezes in the database. See [Human-in-the-Loop Approval](04-planning-and-human-approval.md).

### Resume / `Command(resume=…)`
Continuing a paused graph. A later HTTP request sends `Command(resume=value)`; LangGraph reloads the checkpoint and delivers `value` as the return of the original `interrupt()` call. **Crucial subtlety:** resume re-runs the current node *from the top*, so everything before `interrupt()` runs again on every resume.

### Pending resume write
The low-level checkpoint entry that carries a human decision back into a paused `interrupt()`. In the production approve path, the API stages `(RESUME, {"type": "approve"})` with `saver.put_writes()` before enqueueing work. This is not a normal state channel: it is neither `LastValue` nor a reducer, and it does not appear in `get_state().values`.

### Thread / thread_id
The durable identity of one conversation or run. Same `thread_id` means same graph thread and same checkpoint. Refine and approve requests, possibly minutes apart on different replicas, find each other by sharing a `thread_id`.

### Middleware
Code that wraps a model call to change the request just before it reaches the model, without touching saved state. Used here to inject per-user context into the prompt, and to hide tools the agent is not allowed to use.

### ToolMessage
The result of a tool call, fed back to the model as its next input. This series uses it as a *continuation signal*: after a human approves or refines, the gate returns a ToolMessage telling the model what to do next, because a tool result has higher salience than the system prompt.

---

## Agent-design terms

### Gate
The approval checkpoint in the workflow — implemented as the `submit_plan` tool. It projects the plan, calls `interrupt()` once, and returns an instruction. See [Human-in-the-Loop Approval](04-planning-and-human-approval.md).

### Plan / plan_id / interrupt_id
The **plan** is the set of research steps the human approves. **plan_id** is stable across refinements (it identifies the plan, not the revision). **interrupt_id** is minted fresh on every pause, so it identifies the exact revision the user was looking at — which is what catches a stale browser tab approving an out-of-date plan.

### Projection
A value derived from a source of truth, rebuilt on demand rather than stored and mutated. The typed `Plan` is a projection over the agent's raw todo list; the user-facing event feed is a projection over the run's real state. A projection can be thrown away and recomputed, which is what makes the system's persistence safe.

### Run-state
A single database row that tracks the lifecycle of one run attempt. Fields: `thread_id`, `run_id`, `status`, `receive_count`, and optionally a `reason` on failure. Status moves through: `queued` (written by the API on approval) → `running` / `retrying` (written by the worker on pickup) → `completed` / `failed` (written last). The SSE tail polls this record to decide when to close the browser stream — it must not rely only on a final stream sentinel, because a crashed worker may never write one.

### StreamEnvelope
The curated event shape the frontend receives. Raw LangGraph or DeepAgents events are framework internals; the API or worker projects them into `StreamEnvelope` records before the UI sees them.

### Idempotency
The property that doing something twice has the same effect as doing it once. Critical wherever work can be retried or replayed after a crash.

### Idempotency key
A value that identifies a *logical* event regardless of which attempt produced it. Because it is derived from execution position (not from a counter or a random id), a replayed event carries the same key and the reader can dedupe it. See [Durable Async Runs](08-queue-and-worker-execution.md#sequence-number-versus-idempotency-key).

### Sequence number (seq)
A plain counter that gives the event feed a total order and doubles as the reader's cursor. Unlike the idempotency key, it takes *new* values on replay — which is exactly why the two are kept separate.

### Heartbeat
A periodic "I am still alive" signal. Here it is tied to progress (each checkpoint write) rather than a wall-clock timer, so a process that is alive-but-stuck stops beating and gets reclaimed. See [Durable Async Runs](08-queue-and-worker-execution.md#heartbeat-on-progress-not-on-a-clock).

### Grounding
The rule that the final answer may only use facts from tool results, never the model's own memory. In a regulated domain a fabricated figure that looks real is a compliance problem, so grounding is enforced by the prompt (code cannot tell where a number came from). See [Designing the Orchestrator Prompt](09-orchestrator-prompt.md#grounding-the-purely-prompt-invariant).

### Entity resolution
Working out what a vague question actually refers to — turning "give me my sales" into a specific country, brand, and time period — before deciding anything else.

### run_id { #run_id }
One execution *attempt* of a thread's work. A [thread](#thread-thread_id) can be run more than once — an initial enqueue, a retry after a crash, a queue redelivery — and each attempt gets a fresh **run_id**, so logs and run-state records can tell the attempts apart. Contrast `thread_id`, which stays constant across all of them. See [Identifiers](05-identifiers.md).

### Ordinal id { #ordinal-id }
The stable position of a step within an approved plan — step 1, step 2, step 3. Unlike [seq](#sequence-number-seq), which orders the live event stream and takes new values on replay, a step's ordinal is fixed once the plan is [locked](04-planning-and-human-approval.md), so a report can refer to "the result of step 2" and mean the same thing on every replay. See [Identifiers](05-identifiers.md).

### Provenance id { #provenance-id }
A pointer from a fact back to the tool result that produced it — which `query_source` call a given figure in the report came from. It makes [grounding](#grounding) *auditable*: every number carries the id of its retrieval, so a reviewer, or code, can confirm no figure was invented. See [Identifiers](05-identifiers.md).

### owner_id { #owner_id }
Who proposed a plan and owns a thread. Checked on resume so one user cannot approve or resume another user's run. An authorization identity — not an ordering or dedup one.

### trace_id { #trace_id }
The observability correlation id that ties every log line, span, and model call for one request together. It exists for humans debugging after the fact; nothing in the run's *correctness* depends on it. Distinct from [run_id](#run_id), which identifies the attempt itself.

---

## The one distinction the whole series turns on

### Transport success vs semantic success
**Transport success** means the HTTP request finished and nothing threw — `200 OK`. **Semantic success** means the right context, state, lock, plan, and persistence rules all actually held. A silent failure is any case where the first is true and the second is not. Every article is about one way those two can drift apart.

---

*Missing a term? It belongs here — tell your teacher (the agent) and it will be added.*
