# Before Resume: The Proposal Turn, End to End

> Reconstructed from source notes. Genericized: product → "the platform"; brand → `BRAND_A`;
> the memory service → "the memory service (STM/LTM)"; tables → `app-{env}-*`. Framework names
> (LangGraph, deepagents, DynamoDB) are real.

This is the *before-approval* half of a deep agent: everything from the moment a research
question arrives to the instant the human clicks Approve. Its companion,
[`life-of-a-deep-run.md`](./life-of-a-deep-run.md), takes over from the approve click and runs
the plan asynchronously.

The governing principle of this whole phase, stated up front:

> **Persistence stores the source; the model input is a derived, disposable projection.** Nothing
> that can be rebuilt is written; nothing that is written is ever parsed back out of a prompt.

And one deliberate absence: during this entire phase, **nothing is written to chat history.** The
user's question is *not* saved when it arrives (the way a quick-mode question is) — a plan the
user never approves would otherwise orphan a row in the conversation of record. The question is
persisted at end of run instead, from state.

---

## Part 0 — The big picture

```
1. POST /chat/stream (mode=deep)
   Browser ──► API (ECS)
   │EventSource│   prepare_memory():  memory service STM+LTM
   │           │                      entity-state get_item → DynamoDB: entity-state
   │  SSE:     │   build_proposal_graph_input()
   │  plan     │◄─ graph.astream(...)  ── every super-step → saver.put()
   │  events + │        interrupt()
   │  interrupt│
   └───────────┘
                       2. the thread PARKS here (nothing is running)
                          DynamoDB: deep-checkpoints
                            full graph state + the pending interrupt

3. POST /deep/{thread}/plan/refine   (loop 0..n times)
     read_pending_gate → acquire claim → Command(resume) → SSE: updated plan, interrupt, release

4. POST /deep/{thread}/approve  →  plan LOCKS  →  life-of-a-deep-run.md takes it from here
```

**Read it top to bottom:** the proposal request reads three stores to build the model's context;
the graph checkpoints itself into DynamoDB on every super-step; `interrupt()` parks the whole
thread inside the checkpoint table; and every refine or approve — minutes or days later, on any
API replica — reconstructs everything it needs from that one table. **Resume works because
nothing lives in process memory.**

---

## Part 1 — The cast of characters

### The stores this phase touches

| Store | Physical name | Role before resume | Access |
|---|---|---|---|
| **Checkpoint table** | `app-{env}-deep-checkpoints` | The whole ballgame: every super-step's state snapshot, the pending interrupt, and the resume-claim lease | Written by LangGraph's saver; read by resume |
| **Entity-state table** | `app-{env}-entity-state` | The previous turn's resolved `{country, brand, period}` groups — quick or deep, same row | One `get_item` per proposal |
| **The memory service** | (STM/LTM) | STM turns + session summary + LTM insights/preferences | Read by `prepare_memory()`, fail-safe |
| **Chat history** | `app-{env}-chat-history` | Fallback memory source when the memory service is off | Read-only here; **deliberately not written** |
| **S3 offload bucket** | `$DEEP_CHECKPOINT_S3_BUCKET` | Checkpoints past ~350 KB offload transparently; the table keeps a pointer | Written by the saver when needed |

### The IDs — where each one is born and what it's for

| ID | Born where | Lifetime | Purpose |
|---|---|---|---|
| `thread_id` | `chat_id` chosen at proposal time | Whole conversation | The LangGraph thread — the deep thread **is the chat**, not the session. Refine and approve arrive as separate requests and must land on the same thread. |
| `deep_run_id` | `uuid4().hex` per proposal | One proposal→report cycle | Scopes the observability trace to ONE turn; resumes never rewrite it, the next proposal mints a new one |
| `question_ts` | Server clock at question arrival | One turn | Orders entity-state writes at end of run by *arrival* time (completion order inverts under slow runs) |
| `plan_id` | Derived (`plan-{thread_id}`) per question cycle | One question's cycle | Stable across refinements by design — which is why it alone cannot authorize an approve |
| `interrupt_id` | LangGraph, per `interrupt()` pause | One pause | Binds an approval to the exact revision the user was looking at; a stale tab's approve is rejected |
| Claim token | `uuid4().hex` per `acquire_resume_claim()` | One resume attempt | Token-guarded release: a stream that outlives its lease cannot delete the next holder's claim |
| `owner_id` | Captured from the proposal request into the interrupt payload | The run | Execution always runs under the plan owner's identity, never the resume caller's |

### The state channels (the deep graph's persisted shape)

All `LastValue` unless noted:

| Channel | Written by | When |
|---|---|---|
| `messages` | graph input, agent node, gate's ToolMessages; `write_todos` (TodoListMiddleware) | planning + every refine (append-reducer, accumulates across the whole cycle) |
| `todos` | `write_todos` — the sole writer | every submit; resets to `[]` at a new question cycle |
| `plan` | `submit_plan` via entity update | every submit (`proposed` / `+mod_count` / `locked`) |
| `entity_groups` | `submit_plan` | every submit; resets at a new cycle |
| `raw_question` | `build_proposal_graph_input` | proposal only |
| `user_context` | `build_proposal_graph_input` | proposal only — always written, even empty |
| `question_ts` | `build_proposal_graph_input` | proposal only |
| `deep_run_id` | `build_proposal_graph_input` | proposal only |

> The "proposal only" rows are the resume contract in miniature: refine and approve are
> `Command(resume=…)` calls that write *nothing* into these channels, so `LastValue` semantics
> carry the proposal's values forward unchanged through every resume.

---

## Part 2 — The proposal turn, end to end

### Step 0: The request arrives — and one thing pointedly does NOT happen

A deep turn enters through the same `/chat/stream` endpoint as every quick turn, with
`mode=deep`. Quick mode saves the user's message to chat history immediately. **Deep mode skips
that write** — the gate may never be approved, and a saved question with no answer would orphan a
row in the conversation of record. The question will be persisted at end of run instead, read
back out of the checkpoint's `raw_question` channel. This is the first instance of the phase's
governing principle.

### Step 1: `prepare_memory()` — three stores become one view

The orchestrator plans *blind to the caller's profile*: the graph input carries only the raw
question, so "give me my sales" has nothing to resolve "my" against. Deep reuses quick mode's
machinery verbatim:

```python
prepare_memory(user_id, chat_id, query):
    # 1. STM: recent turns for (actor=user_id, session=chat_id) + session summary, prepended as
    #    a system message. Fallback when the memory service is off/down: raw DynamoDB chat history.
    # 2. entity-state get_item(chat_id): ALWAYS consulted, even when the memory view above is
    #    empty — the sole carryover source must survive a degraded memory backend.
    # 3. LTM search (actor=user_id, cross-session): semantic insights + parsed "User preferences:"
```

Every memory-service call is **fail-safe** — reads return `None`, the app degrades, the turn
proceeds. An outage costs context quality, never availability.

### Step 2: `build_proposal_graph_input()` — one atomic input, one owner

The proposal's graph input is built in exactly one place (`proposal_context.py`), and any future
entry point that starts a proposal turn MUST build its input the same way:

```python
{
    "messages": [{"role": "user", "content": question}],
    "raw_question": question,                                  # verbatim, eviction-proof
    "user_context": build_proposal_user_context(user_info, memory),  # may be empty
    "question_ts": int(time.time() * 1000),                    # ARRIVAL time (for ordering)
    "deep_run_id": uuid.uuid4().hex,                           # one trace per turn
}
```

Two non-obvious rules, both `LastValue` footguns caught in review:

- **`user_context` is always written, even when empty.** An omitted write inherits the previous
  turn's value — a follow-up with nothing to inject would silently plan against the last
  question's context block.
- **`question_ts` is arrival time, not completion time.** A 45-minute run that completes after a
  newer question in the same chat must *lose* the entity-state write to that newer question.
  Completion order inverts under slow runs; arrival order never does.

The rendered `[User context]` block itself:

```
[User context]
(entities named explicitly in the question always win over anything below, and once a research
 plan is approved and locked, the locked plan's entities win over these defaults)
User defaults: Country: AUSTRALIA, Brand: BRAND_A. Use these when the question says "my" or omits
 country/brand.
Previously discussed in this conversation: Country: AUSTRALIA, Brand: BRAND_A, Period: 2024.
 Resolve follow-up references ("same brand", "what about …") against these.
Conversation memory: <session summary / LTM insights>
[/User context]
```

The precedence line at the top is the whole conflict-resolution policy in one sentence: **explicit
question > locked plan > carryover/defaults.** A hygiene detail that matters: the block's content
is user-influenced (LTM can echo prior conversation text), so a smuggled `[/User context]` inside
it is rewritten before composition — the end marker is ours alone; nothing in memory may forge
the boundary between system context and the user's words.

### Step 3: Injection is transient — the block is never checkpointed

`UserContextMiddleware` composes the block onto the latest genuine `HumanMessage` per model call,
via `ModelRequest.override(messages=…)` — an immutable copy the graph never writes back to state.
Three consequences:

1. **The checkpointed transcript stays clean** — no block accumulates across the dozens of model
   calls in a cycle, so checkpoints don't bloat O(N) with user-specific data.
2. **Injection targets the model REQUEST, never the system prompt** — the system prompt is prompt-
   cached as request-invariant; per-user content would break that cache for every user in the
   process.
3. **After summarization compacts a long thread**, the only `HumanMessage` left may be the
   synthetic summary — the middleware skips it (via a `summarization` source marker) rather than
   labeling summarizer prose as the user's words.

### Step 4: The checkpointer — why this phase survives anything

The graph is compiled with `DynamoDBSaver` (from `langgraph-checkpoint-aws`):

| Setting | Value | Why |
|---|---|---|
| Table | `app-{env}-deep-checkpoints` | Generic PK/SK saver schema; name discovered via env slug, published to SSM |
| `thread_id` | `chat_id` | Refine/approve are separate HTTP requests — they find the thread by the id the frontend already has |
| TTL | 30 days (`DEEP_CHECKPOINT_TTL_SECONDS`) | A gate parked longer than this is abandoned by definition |
| Compression | gzip | State carries the full transcript |
| S3 offload | > ~350 KB → `s3://$DEEP_CHECKPOINT_S3_BUCKET/deep-checkpoints/…` | DynamoDB's 400 KB item cap; transparent to the app |
| Fallback | `InMemorySaver` when `DEEP_CHECKPOINTER_BACKEND=memory` | Single-process only (local dev, unit tests); misconfiguration *raises* rather than degrades |

LangGraph calls `saver.put()` after every super-step — our code never writes a checkpoint. By the
time the gate fires, the entire planning conversation is already durable. **The interrupt doesn't
make the thread resumable; the checkpointing that preceded it did.**

### Step 5: The planning loop — `write_todos → submit_plan → interrupt()`

The orchestrator lays out research steps with `write_todos` (the deepagents/langchain middleware
owns that channel), then calls the gate tool `submit_plan`:

1. Reads the prior plan from state (absent on the first call; unreadable priors from older schema
   versions are logged and discarded, never fatal).
2. Projects the raw todos into the typed `Plan`: stable step ids (`step-1…`), title, scope,
   `plan_id` derived from the thread, `modification_count`.
3. Detects a new question cycle: a prior with `status == "locked"` means this submit starts a
   fresh question on the same thread — `plan_id` increments (`plan-{thread}-2`),
   `modification_count` resets, and `entity_groups` reset (carrying the previous question's groups
   forward would persist wrong-country entities; **empty beats wrong**).
4. Calls `interrupt()` with the payload the whole "before resume" phase hands to the "after":

```python
interrupt({
    "kind": "plan_approval",
    "thread_id": thread_id,            # the chat
    "owner_id": owner_id,              # RLS scope: execution runs as the plan OWNER, never the
                                        # resume caller; persistence attribution
    "plan": plan.model_dump(),         # the typed projection the frontend renders
    "prompt": "Approve this research plan, or reply with the changes you want.",
})
```

A clarifying question (ambiguous request — two similar sources at different grains) rides the SAME
single gate, prepended to the prompt: the user answers it as refine feedback or approves the
plan's stated defaults.

The SSE stream emits the plan and the interrupt frame, then closes. **HTTP is done.**

---

## Part 3 — Anatomy of a parked thread

While the user reads the plan — for seconds or for days — this is the complete physical state of
the system:

```
DynamoDB: deep-checkpoints, thread = chat_id
  checkpoint chain (one per super-step, gzipped, TTL 30d)
  latest checkpoint:
    messages        full planning transcript (Human + AI + Tool)
    todos           the proposed research steps
    plan            {status: "proposed", plan_id, steps[], mod_count}
    entity_groups   the plan's resolved {country, brand, period}
    raw_question    the user's words, verbatim
    user_context    the rendered context block (source, not projection)
    question_ts     arrival epoch ms
    deep_run_id     this turn's trace key
  + the PENDING INTERRUPT (payload above, addressed by interrupt_id)
  (no CLAIM row yet — claims exist only while a resume is streaming)

Everything else: NOTHING.
  no process holds the thread          any API replica can resume it
  no chat-history rows for this turn    no entity-state write yet
  no SQS message, no events             no lock, no run-state
```

That last box is the point of the whole design. **"Resume" is not reconnection to a waiting
process — there is no process. It is reconstruction from the checkpoint**, which is why the gate
survives deploys, and why a later async phase can move execution to a different fleet without
touching any of this.

---

## Part 4 — The refine loop: resuming without double-executing

`POST /api/v1/deep/{thread_id}/plan/refine` with `{feedback}`. Before anything resumes, two gates
guard the gate.

**Preconditions — read straight from the checkpointer (`read_pending_gate`):**

| Check | Failure |
|---|---|
| Thread has a checkpoint | `404 unknown_thread` |
| Thread is paused at the gate interrupt | `409 not_at_gate` |
| Submitted `plan_id` matches the gated plan | `409 plan_id_mismatch` |
| Submitted `interrupt_id` is the pending interrupt | `409 stale_interrupt` — a tab looking at a superseded revision cannot act on it |
| Caller asserts an identity | `409 missing_identity` |
| Caller identity == `owner_id` from the interrupt payload | `403 not_thread_owner` |

Note where the truth comes from: **not a sessions table, not app memory — the checkpoint.** The
interrupt payload captured the owner at proposal time precisely so these checks need nothing else.

**The resume claim — one resume at a time, across replicas.** LangGraph does not support
concurrent runs on one thread: two racing resumes would execute twice and leave final state to
whichever finished last. So the endpoint claims the thread in the checkpoint table:

```
PK = "CLAIM#{thread_id}"   SK = "RESUME"
{claim_token: uuid4, interrupt_id, expires_at: now+30min, ttl: now+60min}
ConditionExpression: attribute_not_exists(PK) OR expires_at < :now
```

DynamoDB evaluates condition + write atomically server-side: two replicas race, exactly one wins,
the loser returns `409 resume_in_progress`. The lease expires so a crashed stream doesn't wedge
the thread; release is token-guarded (only if the claim token still matches) so a stream that
outlived its own lease cannot delete the next holder's claim. Same lesson, at smaller scale, as
the worker lock in the other note.

**The resume itself:**

```python
graph.astream(Command(resume={"type": "refine", "feedback": feedback}), config)
```

LangGraph loads the latest checkpoint and **re-enters `submit_plan` from the top** — everything
before `interrupt()` re-runs on every resume. This is why the gate's pre-interrupt code must be
idempotent, and it is by construction: the plan is a pure function of the todos, and `plan_id`
derives from the thread, so a re-run rebuilds the identical plan. The `interrupt()` call then
returns the resume value instead of pausing.

`_decision_of` normalizes defensively: anything that is not an explicit `{"type": "approve"}` is
refinement — a malformed decision can never lock a plan. The feedback goes back to the model as
the gate tool's `ToolMessage` result ("The user requested changes to the plan: … Update the todo
list with `write_todos`. Do not start any retrieval yet.") — **conversational, never a direct
plan edit; the agent is the sole writer of the plan.** The model revises the todos, calls
`submit_plan`, `modification_count` increments, a fresh interrupt (new `interrupt_id`) parks the
thread, the claim is released, and the loop is back at Part 3 with one more revision absorbed.

> Refine feedback re-enters as a **tool result, not a `HumanMessage`** — which is what keeps
> `UserContextMiddleware`'s invariant ("the latest genuine human message IS the current question")
> true through any number of refine cycles.

---

## Part 5 — Approve: the last moment of "before"

`POST /api/v1/deep/{thread_id}/approve` with `{plan_id, interrupt_id}`. Same preconditions, same
claim, one extra binding: `plan_id` is stable across refinements by design (the frontend tracks
one plan through its revisions), so it **cannot** distinguish revision 3 from revision 1 —
`interrupt_id` is what proves the user approved the revision they were actually looking at.

The resume value is `{"type": "approve"}`. `submit_plan` re-runs (idempotently), the interrupt
returns the decision, and the gate locks:

```python
locked = plan.model_copy(update={"status": "locked"})
# + entity_groups written from the plan's resolved entities
# + ToolMessage: "Plan approved and locked. Execute the research steps now."
```

The locked plan and its entity groups are now in the checkpoint — **durable before anything
executes** (a lost execution can be re-driven from this state; a lost approval cannot be
reconstructed).

What happens next is the fork between today and the async future:

- **Synchronous (shipped early):** the same request keeps streaming and execution runs in the API
  process.
- **Async (the other note):** the endpoint flips to checkpoint-then-enqueue — write `run#{thread}
  queued`, `send({thread_id, run_id})` to `deep-run.fifo`, return `202`, and the worker picks it
  up.

Either way: `life-of-a-deep-run.md` takes it from here.

---

## Part 6 — Edge case catalog (before resume)

- **B1. Double-clicked Approve / two browser tabs resuming.** The resume claim's conditional put —
  one winner; the loser gets `409 resume_in_progress`. (In the async phase, SQS content-based
  dedup adds a second layer.)

- **B2. A stale tab approves a superseded plan revision.** `interrupt_id` binding. Each refine
  cycle parks on a NEW interrupt; the old tab's `interrupt_id` no longer matches the pending one
  → `409 stale_interrupt`.

- **B3. A different user resumes someone else's thread.** `owner_id` captured into the interrupt
  payload at proposal time; a mismatched caller gets `403`, an anonymous one `409
  missing_identity`. Execution always runs under the owner's identity.

- **B4. Follow-up question on a thread with a locked plan.** New-cycle detection: the next
  `submit_plan` sees `prior.status == "locked"` → new `plan_id` (`plan-{thread}-2`),
  `modification_count` resets, `entity_groups` reset (the old plan's entities must not leak onto
  the new one). The proposal request itself overwrites `raw_question` / `user_context` /
  `question_ts` / `deep_run_id`.

- **B5. Compaction evicts the original question from the transcript.** The `raw_question` channel.
  Summarization can replace the whole message history with a synthetic summary; end-of-run
  persistence never notices, because it reads `raw_question`, not the transcript.

- **B6. The memory service is down at proposal time.** Fail-safe reads all the way down. STM/LTM
  return nothing, the memory view degrades to raw DynamoDB history (or nothing) — but the
  entity-state `get_item` is still consulted. In that case the context block is empty and
  `user_context` is *written* as empty, not omitted (or the previous turn's block would leak in).

- **B7. The planning transcript outgrows a DynamoDB item.** The saver's S3 offload (> ~350 KB).
  Transparent to every reader — the gate preconditions, the resume, and end-of-run snapshot never
  know the difference.

- **B8. The user never comes back.** TTLs. The checkpoint chain (and its parked interrupt) expires
  after 30 days; a claim from a crashed refine stream expires in 30 minutes. Nothing was ever
  written to chat history, so an abandoned proposal leaves no trace in the conversation — by
  construction, not by cleanup.

- **B9. A crashed refine stream holds the claim.** The lease. `expires_at` is enforced inside the
  next acquirer's condition; the crashed holder's token-guarded release simply never happens and
  the claim is overwritten after 30 minutes. The thread is briefly un-claimable until the lease
  expires, which beats failing the already-delivered stream.

---

## Part 7 — FAQ

**Why isn't the user's question in chat history while the gate is pending?** Because the gate may
never be approved. Quick mode saves the question immediately (its answer follows within the
request). A pending deep plan orphans nothing — the question survives regardless, in the
`raw_question` channel.

**Why is `thread_id = chat_id` and not the session id?** Refine and approve arrive as separate
HTTP requests, possibly days later, possibly on a different replica. The one identifier the
frontend reliably has across all of them is the chat id.

**Why doesn't the context block live in the transcript?** It would be checkpointed and re-sent on
every model call, accumulate across turns, carry user-specific data through the compaction
boundary, and break the request-invariant prompt cache. Transient injection costs nothing and
leaves the checkpoint clean. Source channels persist; projections are rebuilt.

**Refine re-runs `submit_plan` from the top — doesn't that redo work?** It re-runs the projection,
which is a pure function (`todos → Plan`) — microseconds, no LLM calls, and by design it rebuilds
the identical plan. The re-run-before-interrupt is the price of LangGraph's resume model, and here
the price is ~zero.

**Where is the plan between requests?** In the `plan` state channel, inside the latest checkpoint,
in DynamoDB. The interrupt payload carries a *copy* for the frontend to render; the channel is the
source of truth.

**What stops two refines from racing?** The resume claim — a conditional put on
`CLAIM#{thread_id}` in the checkpoint table. Atomic server-side; the loser gets `409`.

**Why does the deep proposal reuse quick mode's memory functions?** Because the product
requirement is one conversation across modes. A deep follow-up to a quick turn must see the same
carryover row and the same memory view. One `prepare_memory()`, one entity-state table, two
graphs.
