---
summary: "Nine real bugs that returned 200 OK. Each should now feel recognizable."
---

# Capstone 1 · Nine silent failures in one agent

> **Design question:** [How do I fix it for real?](ch2-designing-a-deep-agent.md#fixing-it-for-real) · **Principles:** Fix Root Causes, Attack the Premise, Exhaust the Design Space

Use this as the final review exercise: a deep agent can look healthy, return `200 OK`, and still be wrong.

> **Capstone.** This is where the series lands: the nine bugs below are the failure modes from the build path (state, context, planning, identifiers, streaming, locking, durability, and prompt boundaries) seen together in one real system. Each bug recalls its mechanism in a few lines and links back to the step that teaches it.

This article is written for the engineer who has built normal Python APIs and is now trying to understand production agents. If that is you, the hardest adjustment is this: with agent systems, "the request succeeded" is not the same as "the work was correct."

We learned this while building a commercial-strategy agent that helps commercial leaders answer where-to-play, what-to-play and how-to-win questions. The agent takes a question, proposes a multi-step research plan, pauses for human approval, then executes the approved plan and writes a report. On paper, the stack was ordinary enough: LangGraph for orchestration, middleware for context injection, DynamoDB for persistence, and Server-Sent Events for streaming progress to the browser.

Before one release, code review found nine bugs. None crashed the app. None produced scary logs. None would have paged anyone. Each one completed successfully while corrupting the meaning of the run.

That is the core lesson of this piece:

```text
Transport success means:
  the HTTP request finished

Semantic success means:
  the right context, state, lock, plan, and persistence rules all held
```

Deep agents fail when those two meanings drift apart.

## The system in plain English

Imagine a business user asks:

```text
Give me my sales.
```

That sentence is not enough by itself. The system needs to know what "my" means: which country, which product, which time period, and which previous conversation context should carry forward.

So the agent does four things:

1. It adds user-specific context before the model sees the question.
2. It asks the model to propose a research plan.
3. It pauses and waits for a human to approve or refine that plan.
4. It resumes later, runs the research, streams progress, and saves the final answer.

The hard part is that these steps do not happen inside one neat function call. A single user turn spans multiple HTTP requests, may resume on a different API replica, and depends on checkpointed state in DynamoDB.

None of the bugs below are "the model hallucinated" stories. They are ordinary engineering bugs at the boundaries around the model.

The bugs use the series' terms as they were taught: `thread_id`, state, checkpoint, `interrupt()`, `Command(resume=...)`, `plan_id` and `claim_token`. Each is defined once in the [Glossary](glossary.md).

## The architecture: one user turn, several HTTP requests

Most backend systems map one user action to one request. Deep mode does not. A single research turn spans at minimum three separate HTTP requests over potentially 30 or more minutes:

```text
POST /chat/stream?mode=deep        <- proposal: fresh question, starts the graph
  -> streams plan as SSE
  -> closes at "interrupt" frame (graph pauses, no "end" sent)

POST /deep/{thread_id}/plan/refine <- optional: user wants changes
  -> Command(resume={type: refine, feedback: "..."})
  -> streams updated plan, closes at "interrupt" again

POST /deep/{thread_id}/approve/async <- user locks the plan
  -> validate plan_id + interrupt_id
  -> stage resume decision in checkpoint
  -> write run-state queued
  -> enqueue FIFO message
  -> return 202 queued

GET /deep/{thread_id}/events       <- browser watches research
  -> tail curated event log until run-state completed/failed
```

These are independent HTTP requests, potentially from different browser tabs, different API replicas, and minutes apart. The graph does not know they are related. LangGraph's checkpointer does.

### How the checkpoint ties them together

Every LangGraph node that runs writes its output to the checkpoint. When `submit_plan` calls `interrupt()`, the graph pauses and LangGraph writes the interrupt payload, including the plan, the prompt, and gate metadata, to the checkpoint's pending writes. The HTTP request finishes, the SSE stream closes, but the graph state is frozen in DynamoDB, waiting.

```python
# gate.py - submit_plan tool
decision, feedback = _decision_of(
    interrupt({
        "kind": "plan_approval",
        "thread_id": thread_id,
        "owner_id": owner_id,
        "plan": plan.model_dump(),
        "prompt": prompt,
    })
)
# Everything above interrupt() re-runs on every resume.
# Everything below runs only after the human decision arrives.
```

The plan is therefore stored twice for practical purposes:

- As graph state in the `plan` channel, so later graph nodes can read it.
- As part of the pending interrupt payload, so the HTTP approval endpoint can validate the exact plan before resuming.

That is why `read_pending_gate(thread_id)` can see `gate["plan"]["plan_id"]` before the graph resumes. It is reading the pending interrupt metadata from the checkpoint, not recomputing the plan.

On the refine or approve request, `read_pending_gate(thread_id)` reads the interrupt payload directly from the checkpointer, with no graph compile required, to validate preconditions. A refine request may synchronously resume to produce another plan. In the production approve path, the endpoint stages the approval decision into the checkpoint, writes run-state as `queued`, enqueues a FIFO message, and returns `202 queued`.

```python
# deep.py router - approve endpoint
gate = read_pending_gate(thread_id)
if gate is None:
    raise HTTPException(404)
if gate["plan"]["plan_id"] != payload.plan_id:
    raise HTTPException(409, "plan_id_mismatch")

persist_resume_decision(thread_id, {"type": "approve"})
put_run_state(thread_id, run_id, status="queued")
enqueue_deep_run(thread_id, run_id)
return {"status": "queued", "run_id": run_id}
```

The `plan_id` and `interrupt_id` checks prevent a stale browser tab from approving an older pause after the user has refined it. `plan_id` may identify the logical plan across refinements; `interrupt_id` identifies the exact interrupt the user saw.

```text
Tab A sees plan_id p1
Tab B refines plan to p2
Tab A clicks approve with p1
API reads pending gate p2
API returns 409 plan_id_mismatch
```

Later, the worker receives the queue message and calls `agent.astream_events(None, config)`. The approval is not in the SQS message and not in an ordinary state channel; it is already staged in checkpoint pending writes.

### Why the checkpoint and the resume claim live in the same DynamoDB table

LangGraph does not support concurrent runs on a single thread. Two approve clicks racing, or two browser tabs both hitting approve, would execute the research plan twice and leave the final checkpoint to whichever process finished last. To prevent this, we write a claim item to DynamoDB before every resume:

```text
DynamoDB table: agent-checkpoints
|- Checkpoint items  (LangGraph's own PK/SK scheme per thread + step)
\- Claim items       PK = "CLAIM#{thread_id}", SK = "RESUME"
                     Fields: claim_token, expires_at, ttl
```

Both live in the same table because the claim needs the same durability and region as the checkpoint, and an in-process claim would be invisible to other API replicas. It is the acquire-with-expiry, token-guarded-release claim from [Step 6](step-6-distributed-locks.md). Bug 8 below is what happened before release checked the token.

### One Langfuse trace across all three requests

Each `graph.astream_events()` call opens a new LangGraph run, which by default opens a new Langfuse trace. Without intervention, a deep turn that goes proposal -> refine -> approve produces three sibling traces with no structural link. We seed the trace ID deterministically from the thread ID:

```python
trace_id = langfuse_client.create_trace_id(seed=f"deep:{thread_id}")
handler = CallbackHandler(trace_context={"trace_id": trace_id})
handler.last_trace_id = trace_id
```

Same `thread_id` means the same seed, which means the same trace ID on every request. All three phases land in one Langfuse trace, one Agent Graph, properly sequenced.

In Langfuse terms, a trace is the top-level timeline for one logical user task. A run or observation is one child operation inside that timeline, such as a LangGraph invocation, model call, tool call, or chain step.

`CallbackHandler` is the bridge that receives LangChain/LangGraph callback events and sends them to Langfuse. Passing `trace_context={"trace_id": trace_id}` tells it to attach this request's events to the existing logical trace.

```text
Langfuse trace: deep:t-123
  run: proposal request
    model call
    tool call submit_plan
  run: refine request
    model call
    tool call submit_plan
  run: approve request
    model calls
    research tools
    report writer
```

Without the seeded trace ID, those three HTTP requests look like unrelated traces. Debugging "why did this approved plan produce that report?" becomes a manual correlation problem.

## The design: state channels and middleware

### What a LangGraph channel is

Each state key is a channel with its own update rule. The default, `LastValue`, keeps the old value when a turn does not write the key, and [Step 1](step-1-state-and-checkpoints.md) explains why that is a footgun for channels that must reset on a new turn. `messages` uses an appending reducer instead.

Persisted channels in our agent included:

```text
messages          DeltaChannel    Full transcript: HumanMessage, AIMessage, ToolMessage
todos             LastValue       [{content, status}] managed by write_todos tool
plan              LastValue       Serialized Plan dict: plan_id, status, steps, modification_count
entity_groups     LastValue       [{country, brand, period}] written by submit_plan
raw_question      LastValue       User's verbatim question text
user_context      LastValue       Rendered context block (defaults + memory)
```

### Why we split state across two TypedDicts in two files

LangGraph compiles the graph state schema from `state_schema=` plus any `state_schema` fields declared by middleware. We used that to co-locate each channel declaration with its sole writer:

```python
# gate.py
class DeepState(DeepAgentState):
    plan: NotRequired[dict[str, Any]]
    entity_groups: NotRequired[list[dict[str, Any]]]

# proposal_context.py
class _UserContextState(AgentState):
    raw_question: NotRequired[str]
    user_context: NotRequired[str]

# graph.py
graph = create_deep_agent(
    ...,
    state_schema=DeepState,
    middleware=[UserContextMiddleware(), ...],
)
```

At compile time, LangGraph merges `DeepState` and `_UserContextState` into one checkpointed schema. At runtime they are indistinguishable, but in the source each channel declaration sits beside the code that writes it. Dropping `UserContextMiddleware` from the middleware list still compiles and runs. The channels just disappear from the checkpoint quietly, the same kind of structural gap Bug 5 exploited.

### Why middleware for context injection

We needed to prepend user-specific context, including defaults and prior conversation entities, to the user's question before it hit the model. Four approaches were on the table:

- System prompt injection: rejected because the orchestrator prompt is request-invariant and cached.
- Checkpointed message injection: rejected because the block would accumulate in history across turns.
- Tool call: rejected because it adds an extra model/tool round trip and can be skipped.
- Middleware: inject just before the LLM call, after the checkpoint is read.

```python
class UserContextMiddleware(AgentMiddleware):
    state_schema = _UserContextState

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._inject(request))

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._inject(request))

    def _inject(self, request: ModelRequest) -> ModelRequest:
        context = str((request.state or {}).get("user_context") or "")
        if not context:
            return request
        ...
        return request.override(messages=messages)
```

`request.override(messages=...)` returns a new `ModelRequest`. It does not mutate state. The injected copy vanishes after the model call, the checkpoint stays clean, and the middleware reads its own declared channels directly from `request.state`.

The tradeoff is that the injection is invisible in the checkpointed transcript and every assumption the injection code makes about message structure becomes a correctness risk. Bugs 2 and 3 are both examples of those assumptions failing.

## 1. Injected context kept overriding a decision the user already made

Middleware injected the user's default entities, country Australia, on every model call, including execution calls after a human had approved a plan for Germany. The model resolved Germany correctly at plan time, so this is neither Step 1's stale state nor Step 2's plan-time precedence bug. During execution the freshly injected default sat near the top of the prompt while the approval sat lower in history, and the report came back about Australia. Gating injection on plan status would have broken multi-turn sessions, so the fix was a precedence rule written into the context block and the execute step. [Step 8](step-8-orchestrator-prompt.md#the-case-that-has-to-be-both) steps through the bug and shows the shipped prompt text.

Lesson: when context should only apply at one phase of a multi-phase pipeline, encode that constraint in the context itself. A prompt rule beats a code gate when the code gate has to reason about shared state across turns.

## 2. A framework dependency swapped in a synthetic HumanMessage under our feet

The context middleware attached its block to the latest `HumanMessage` in the request. `deepagents` installs a summarization middleware unconditionally, and near the context limit it replaces old history with a synthetic `HumanMessage` tagged `lc_source: "summarization"`. The original user message may already be gone. Our reverse scan found the summary, attached the context to it, and so labeled summarizer prose as the user's own words. The fix skips messages with that tag. [Step 2](step-2-context-injection.md#two-ways-it-goes-wrong) shows the corrected scan.

Lesson: when your code finds "the latest item of type X in a list," enumerate every party that can insert an X. Framework middleware can insert objects that pass `isinstance(msg, HumanMessage)` without being genuine user input.

## 3. A search loop continued past its target and decorated the wrong message

The injection code scanned backward through the message list looking for a `HumanMessage` with string content. The original condition:

```python
if isinstance(msg, HumanMessage) and isinstance(msg.content, str):
    # inject here
```

LangChain messages can carry list-form content for multimodal inputs (images, documents). If the latest human message has list content, the condition fails and the loop **keeps going backward** until it finds an older string message.

```text
Message list (newest at the bottom):

  index 0  HumanMessage  "Give me Germany sales"     ← Turn 1 (string content)
  index 1  AIMessage     "Here are the results..."
  index 2  HumanMessage  [image, "Compare these"]    ← Turn 2 (LIST content, fails check)

Backward scan starts at index 2:
  index 2 → HumanMessage but content is list → FAIL → keep going
  index 1 → AIMessage → skip
  index 0 → HumanMessage with string → MATCH → inject here ✗ WRONG TURN
```

Turn 2's context ("user defaults: Australia") gets injected into Turn 1's question. Turn 2's message receives no context at all. The model plans for Australia when the user asked about Germany, and the context lands on a turn that already completed.

The fix was to separate "not the target" from "the target exists but cannot be processed":

```python
for i in range(len(messages) - 1, -1, -1):
    msg = messages[i]
    if not isinstance(msg, HumanMessage):
        continue
    if msg.additional_kwargs.get("lc_source") == "summarization":
        continue
    if not isinstance(msg.content, str):
        logger.warning(
            "user_context not injected: latest human message has %s content",
            type(msg.content).__name__,
        )
        return request
    # inject and return
```

Lesson: in a backward search, failing the capability check should not silently retarget the search to an older item. When you find the right item but cannot act on it, stop and log.

## 4. User-influenced text inside a trusted block could forge its own closing delimiter

The context block closes with `[/User context]`, and part of the block comes from session summaries we do not fully control. If that text ever contained the literal delimiter, it would close the block early and the rest would pose as the user's words. The fix lowercases the delimiter inside injected text before embedding it. [Step 2](step-2-context-injection.md#two-ways-it-goes-wrong) shows what the model sees with and without the escape.

Lesson: this is prompt injection. Any time you embed external text inside a structured format that uses delimiters, escape your delimiter before embedding.

## 5. A three-file invariant had no structural owner and no error on violation

Starting a new turn required writing three `LastValue` channels together: the message, the raw question, and the context block, even when the context is empty. The rule lived in a hand-built dict in one file, the channel declarations in another, and the readers in a third. A caller that wrote only `messages` would silently keep the previous turn's `raw_question` and `user_context`. The fix was a factory function, `build_proposal_graph_input`, placed beside the channel declarations. This is the France/Germany bug from [Step 1](step-1-state-and-checkpoints.md#turn-the-convention-into-an-api).

Lesson: there is a hierarchy of invariant enforcement: type signature, factory function, documented convention, and tribal knowledge. Critical invariants should sit as high in that hierarchy as practical.

## 6. The post-stream database write got cancelled when the client hung up

A deep run streamed its `end` event and then awaited the database save. The frontend correctly closes the SSE connection as soon as it sees `end`, and in Starlette a client disconnect raises `CancelledError` at the generator's next `await`. The save never ran. Because `CancelledError` is a `BaseException` in Python 3.11+, no `except Exception` block logged it. The fix starts the save as a background task before yielding `end`. [Step 5](step-5-streaming-and-background-work.md) shows the before and after.

Lesson: any `await` inside an SSE generator that runs after a terminal event is a race against client disconnect. Create the task before yielding the terminal event and hold a strong reference to it.

## 7. Writer and reader used different keys for the same record

Chat history is stored in DynamoDB under `chat_id`. The approval payload marked `chat_id` as optional.

The writer used a fallback:

```python
key = payload.chat_id or payload.session_id
```

but every reader used only `chat_id`:

```python
history = get_history(payload.chat_id or "")
```

When the frontend omitted `chat_id`, the write succeeded under `session_id`. The next reader looked up `""` and found nothing. Deep mode looked stateless for that traffic slice with no error anywhere.

Quick mode had already solved this correctly by skipping the write entirely when `chat_id` is absent:

```python
if not chat_id:
    logger.info("skipping persistence: no chat_id")
    return
```

Lesson: if a field is optional, some callers will omit it. Writer and reader must handle that absence identically. Otherwise you create rows that no reader will ever find.

## 8. The lock release did not prove it still owned the lock

The resume claim expires after 30 minutes, and the run deleted it on completion with an unconditional `delete_item`. A run that outlived its lease could therefore delete a second request's newer claim and reopen the double-run window. The fix stores a per-claim token on acquire, makes release conditional on it, and logs a skipped release at info level. That is the token-guarded delete in [Step 6](step-6-distributed-locks.md#release-must-prove-ownership).

Lesson: "release a lock" and "release your lock" are different operations. Always prove ownership on release.

## 9. Embedding stateful context in message records made an explicit reset invisible

After each completed turn, we saved session state, including resolved entities such as country, brand, and time period, as a field on the assistant message record in DynamoDB. The next turn scanned message history in reverse to find the most recent record with that field and injected it as context.

The bug was a three-step collapse of semantics.

First, the state field was coerced before storage:

```python
entity_groups = values.get("entity_groups") or None
```

An explicit reset to `[]` became `None`.

Second, the persistence layer skipped `None` fields:

```python
if md.entity_groups:
    item["entity_groups"] = md.entity_groups
```

The reset turn was written with no `entity_groups` attribute at all.

Third, the read path scanned backward for the most recent non-empty value:

```python
for msg in reversed(memory):
    groups = msg.get("entity_groups", [])
    if groups:
        return groups
```

So the scan skipped the reset turn and landed on an older populated turn, rehydrating stale state.

The fix was to move session state out of message records and into a purpose-built store with direct overwrite semantics:

```python
state_value = values.get("session_state")
if state_value is not None:
    store.put(session_id, state_value)

state_value = store.get(session_id)
```

`None` means the state channel was never set and the previous row remains untouched. `[]` means explicit reset and should overwrite the previous value.

Lesson: embedding session state inside event or message records creates two structural problems. It forces readers to scan history instead of doing direct lookup, and it makes empty-vs-absent semantics easy to destroy through convenience coercions like `x or None`.

## The pattern

All nine bugs shared one property: the code ran to completion and the operation succeeded in the narrow transport sense because nothing threw. What failed was the semantic contract.

| Bug | What "succeeded" | What happened |
| --- | --- | --- |
| Context overrides approved plan | `200 OK`, report generated | Report about wrong entities |
| Synthetic message targeted | `200 OK`, context injected | Context labeled summarizer prose as user words |
| Loop retargeted wrong message | `200 OK`, context injected | Context attached to a previous turn's message |
| Delimiter forged in block | `200 OK`, plan proposed | Smuggled text treated as user content |
| Convention-only channel write | `200 OK`, turn started | Previous context bled into a new turn |
| Save cancelled on disconnect | `200 OK`, end event sent | Database write never ran |
| Wrong key on write | `200 OK`, row written | Row written under a key no reader uses |
| Unconditional lock release | `200 OK`, claim deleted | Another request's claim was deleted |
| `[] or None` reset invisible | `200 OK`, turn answered | Follow-up planned against prior topic entities |

The review habits that caught these before they shipped were consistent:

- Test the content, not just the return code.
- Read library source, not just the docs.
- For every abstract pattern, such as injection, locking, or persistence, enumerate failure modes phase by phase and ask whether a transport-level success code means the semantic work completed.

Built on LangGraph, Python 3.11, DynamoDB, and Starlette SSE. The stack is incidental. Context injection targeting, lock ownership, async cancellation, and writer-reader key mismatch are universal failure classes.

## A note on the approval code snippet

One detail to watch in the approval endpoint pattern above: the code shows a `plan_id` check, but `plan_id` is stable across refinements by design: it names the logical plan, not the exact interrupt. To reject a stale browser tab that approved an older revision, also validate `interrupt_id`, which is minted fresh for each pause:

```python
gate = read_pending_gate(thread_id)
if gate is None:
    raise HTTPException(404)
if gate["plan"]["plan_id"] != payload.plan_id:
    raise HTTPException(409, "plan_id_mismatch")
if gate["interrupt_id"] != payload.interrupt_id:
    raise HTTPException(409, "interrupt_id_mismatch")
# only now stage the decision
persist_resume_decision(thread_id, {"type": "approve"})
put_run_state(thread_id, run_id, status="queued")
enqueue_deep_run(thread_id, run_id)
return {"status": "queued", "run_id": run_id}
```

`plan_id` catches a plan-content mismatch. `interrupt_id` catches the more common case: the user refined and re-interrupted, but an old tab still has the original interrupt open.

---

!!! check "You should now understand"
    - How the same `200 OK` shape appears across state, context, streaming, locking, and persistence bugs
    - Why the useful review question is semantic: "did the right invariant hold?", not only "did the request finish?"
    - Why stale state, stale browser tabs, stale lock holders, and stale memory records are the same family of production problem
    - Why each earlier chapter exists: every build step prevents one class of silent failure in this case study

??? question "Final review"
    **Pick any one bug in the table above. Name the chapter that should have taught you to catch it, the invariant that failed, and the smallest test or review question that would expose it.**

    ??? success "Example answer"
        For "unconditional lock release," the chapter is [Step 6 · Distributed locks](step-6-distributed-locks.md). The failed invariant is: a process may only release the claim it still owns. The review question is: "Does release prove ownership with the current claim token, or does it delete by thread id alone?" The test is a takeover scenario: A acquires, A's lease expires, B acquires, A attempts release, and B's claim must remain.

## Four more from production (post-release)

The nine above came from a single pre-release review. These four arrived as production PRs in the following weeks. They follow the same pattern: the system reported success, and something semantic was wrong.

### Quadratic writes from replace-semantics streaming

A model that emits reasoning tokens sends many `thinking.delta` events, each carrying the *full accumulated* reasoning text to that point (replace semantics). Persisting each delta naively means N deltas write O(N²) bytes into one DynamoDB partition key, which hit the per-partition write-capacity ceiling (~1,000 WCU/s) mid-run.

The fix: time-coalesce live frames in the curator, flushing at most once per 2 seconds per step. The SSE path to the browser stays byte-identical; only the durable-log write is coalesced.

The lesson: replace-semantics streaming plus per-delta persistence is quadratic. Identify the semantics (append vs. replace) before deciding whether to persist every frame or the last.

### Async client bound to a per-job event loop

Each worker job called `asyncio.run()`, which creates and closes a new event loop per job. A cached `httpx.AsyncClient` is bound to the event loop that created it; on the second job, the first client's loop is already closed, and the first Cortex call dies with `Event loop is closed`.

The fix: one process-lifetime `asyncio.Runner`, with pooled clients drained at shutdown. A weakref guard converts any reintroduced per-job `asyncio.run()` into a named error at startup.

The lesson: an async client must never cross event loops. In a worker process that runs many jobs, one process-lifetime runner is the only safe design.

### Burned timeout retried

`ReadTimeout` was classified as a retryable error, so a call that already spent its entire 300-second timeout budget was replayed for another 300 seconds. Two exhausted attempts burned 600 seconds on work that had no chance of succeeding faster.

The fix: only retry errors that fail *before the request was sent* (`ConnectError`, `ConnectTimeout`), plus rate-limit and gateway responses (`429`, `502`, `503`). Use jittered exponential backoff. A `ReadTimeout` means the server acknowledged the request and timed out during processing; retrying it from the start sends a duplicate with no guarantee the first copy was not processed.

The lesson: a burned timeout is spent budget, not a transient blip. Classify retryable errors at the transport layer, not at the exception class.

### Stale plan re-persisted on a follow-up turn

The `plan` channel is `LastValue`. The exit path persisted the plan onto every completed turn's DynamoDB record, including follow-up turns that never called `submit_plan`. This caused a stale prior-turn plan to be stamped onto a follow-up row and resurface later as the current plan.

The fix: stamp `plan.minted_run_id = deep_run_id` at plan creation and persist only when `plan.minted_run_id` matches the current turn's `deep_run_id`. Fail open when either is absent.

The lesson: a sticky `LastValue` channel written only at plan time, combined with end-of-run persistence on every turn, silently re-persists the old value. The turn's own run id is the only safe discriminator.

---

**Next:** [Bridge · Evaluating an agent](bridge-evaluation.md) covers the evaluation basics Capstone 2 assumes. Capstone 2 then moves up one layer, to tool results that aren't task results and evaluations that hide a tradeoff.
