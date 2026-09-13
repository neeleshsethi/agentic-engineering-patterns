# Step 3 · Planning and Human Approval

The agent now has durable state and the right context at the model boundary. The next job is to stop it from doing expensive work too early.

This chapter builds the planning gate: the model drafts a plan, the human approves or refines it, and only then does execution begin. That one design choice changes the topology of the system. The run can no longer live inside one HTTP request.

> Terms below — [`interrupt()`](00-glossary.md#interrupt), [checkpoint](00-glossary.md#checkpoint), [gate](00-glossary.md#gate), [lease](00-glossary.md#lease), [replica](00-glossary.md#replica) — are defined once in the [glossary](00-glossary.md).

Some agent actions are expensive, slow, or irreversible. A research plan that hits external APIs, runs database queries, or spends real money should be shown to a human *before* it executes, not after. This article builds that approval gate from the ground up.

The running example is a pharmaceutical commercial-analytics agent: it plans a set of data-retrieval steps, a human approves or refines the plan, and only then does it query anything. The identifiers are generic (`BRAND_A`, `SOURCE_A`, `query_source`), but the shape applies to any agent with a costly execute phase.

## The Big Picture First

Before diving into code, here is the shape of the whole system this chapter builds:

```text
 WITHOUT approval gate          WITH approval gate
 ───────────────────            ──────────────────

 question                       question
    │                              │
    ▼                              ▼
 Agent plans              Agent calls submit_plan tool
 (internally,                      │
  never shown)            Tool calls interrupt()
    │                     Graph PAUSES — plan shown to human
    ▼                              │
 Agent executes           Human: APPROVE or REFINE?
 (you can't stop it)               │
    │                    ┌─────────┴──────────┐
    ▼                    │ APPROVE            │ REFINE
 Answer                  │                   │
                         ▼                   ▼
                  Tool tells agent:   Tool tells agent:
                  "Execute now."      "Update todos, ask
                         │             me again."
                         ▼                   │
                  Agent executes       Agent edits plan
                         │             calls tool again ↑
                         ▼
                       Answer
```

The key idea: the `submit_plan` **tool** is the bridge between the agent and the human. The agent calls the tool. The tool pauses everything and shows the human the plan. When the human decides, the tool gets the decision back and **tells the agent what to do next** via a ToolMessage. The agent never talks to the human directly.

## Start With The Naive Version

Before any approval machinery, the baseline agent is one straight line:

```text
user question -> LLM plans (internal, never shown) -> agent executes -> answer
```

Everything happens in one HTTP request, one graph invocation, one stream. The problem is not correctness — the LLM might plan correctly. The problem is trust and cost. If the model misread the question, you want to catch it before three source calls run, not after. So you want to show the plan first, and wait.

## The One Primitive: `interrupt()`

LangGraph gives you exactly one primitive for pausing a graph and waiting for a human: `interrupt()`. It serializes the current graph state to the checkpointer and raises a special exception the runner catches. The HTTP response returns with whatever has streamed so far. The graph is frozen in durable storage, waiting.

```python
from langgraph.types import interrupt, Command

def submit_plan(plan: Plan, thread_id: str) -> str:
    # Pause here. The graph suspends, the HTTP request finishes.
    human_decision = interrupt({"kind": "plan_approval", "plan": plan.model_dump()})
    # Everything above re-runs on every resume (see below).
    # Everything below runs only after the human responds.
    return human_decision
```

The frontend renders the plan, the user clicks approve or asks for a change, and the frontend sends a *second* HTTP request carrying the decision:

```python
{"type": "approve"}
# or
{"type": "refine", "feedback": "add a competitive analysis step"}
```

LangGraph finds the paused graph in the [checkpoint](00-glossary.md#checkpoint), re-runs the node, and delivers the decision as the return value of `interrupt()`. In tests or an interim synchronous path, you may see this as `Command(resume=...)`. In the production approve path, the API writes the resume decision into the checkpoint first, queues work, and a worker later resumes with no graph input. Everything else in this article is built on top of that same pause/resume primitive.

```mermaid
sequenceDiagram
    participant U as Browser
    participant A as API
    participant C as Checkpoint (DynamoDB)
    U->>A: POST (propose)
    A->>C: run graph, saver.put() each step
    A->>C: interrupt() freezes state
    A-->>U: stream plan, close (no "end")
    Note over U,C: minutes or days pass; no process is running
    U->>A: POST /approve/async  (second request)
    A->>C: read pending interrupt, validate
    A->>C: saver.put_writes() stages resume decision
    A-->>U: 202 queued
    Note over C: Worker later reloads and resumes with input=None
```

The dashed pause is the whole point: between the two requests, **nothing is running**. The plan lives in the checkpoint, and any [replica](00-glossary.md#replica) can pick it up.

## The Idempotency Rule You Cannot Ignore

When a graph resumes, LangGraph re-runs the current node **from the beginning** — not from the `interrupt()` line. Everything before `interrupt()` runs again, on every resume.

```python
def submit_plan(plan, thread_id):
    send_slack_notification(thread_id, plan)   # runs on EVERY resume
    return interrupt({...})
```

Refine the plan twice and that notification fires three times: once on the initial proposal, once per resume. This is the single most common bug in resumable graphs. The fix is one of two things: move side effects to *after* `interrupt()`, or make them idempotent (check whether the work was already done before doing it).

The deeper consequence: all code before `interrupt()` must be a pure, deterministic function of durable state. If it computes the plan, that computation has to produce the identical plan every time it re-runs — which drives the design in the rest of this article.

## Designing The Interrupt Payload

The payload is the contract between backend and frontend. Changing it later is a frontend migration, so design it deliberately. It needs to carry two different kinds of thing:

```python
interrupt({
    "kind": "plan_approval",
    # 1. What the human needs to decide:
    "plan": plan.model_dump(),           # the human-readable steps, scope, entities
    # 2. What the code needs to validate the decision on resume:
    "plan_id": plan.plan_id,             # which logical plan
    "thread_id": thread_id,              # which thread
    "owner_id": owner_id,                # who proposed it
    "interrupt_id": str(uuid.uuid4()),   # which exact interrupt
})
```

If the human cannot make an informed decision from the payload, they approve blindly. And if the code cannot identify *which exact pause* the human approved, a stale browser tab can approve a revision that no longer exists.

`plan_id` and `interrupt_id` do different jobs. `plan_id` names the logical plan and may stay stable across refinements. `interrupt_id` is minted for each pause, so it is the stronger stale-tab guard.

On the approve endpoint, read the payload back from the checkpointer and validate it *before* resuming:

```python
gate = read_pending_interrupt(thread_id)   # LangGraph exposes the pending writes
if gate["plan_id"]      != request.plan_id:      raise HTTPException(409)
if gate["interrupt_id"] != request.interrupt_id: raise HTTPException(409)
if gate["owner_id"]     != request.user_id:      raise HTTPException(403)
# only now, in the async production path:
persist_resume_decision(thread_id, {"type": "approve"})
put_run_state(thread_id, run_id, status="queued")
enqueue_deep_run(thread_id, run_id)
return Response(status_code=202)
```

If validation fails, the graph stays paused. No state change, no double execution. If validation succeeds, execution still does not run in the approval request; the API has only made the approval durable and queued the worker.

## The Refine Loop: The Design That Does Not Work

With approve/reject the human can only say yes or no. Refine is more useful: the human describes a change, the agent revises the plan and re-presents it. The tempting implementation is a loop inside the tool:

```python
# DON'T do this
def submit_plan(plan: Plan) -> str:
    while True:
        decision = interrupt({"plan": plan.model_dump()})
        if decision["type"] == "approve":
            plan.status = "locked"
            return "approved"
        plan = call_llm_to_update_plan(plan, decision["feedback"])   # the problem
        continue
```

The problem is the marked line. Applying "add a competitive analysis step" means interpreting prose and rewriting research steps — that is LLM work. But you are inside a tool: plain Python, mid-way through the agent's own graph. To regenerate the plan you would make a *separate, out-of-band* LLM call — a second model that does not share the agent's system prompt, tools, or conversation. Now two different models author the plan. Formats drift, context is lost, and the plan the human finally approves was never seen whole by the agent that will execute it.

## Refine Is The Agent Loop

The structural fix: the tool never applies feedback. It hands the feedback back to the agent as its tool result, and the "loop" becomes the agent's own loop. Each `submit_plan` call interrupts exactly once and returns.

Here is who does what at each step of a refine round:

```text
┌─────────┐         ┌──────────┐         ┌──────────┐         ┌──────────┐
│ Browser │         │   API    │         │  Agent   │         │  Tool    │
│ (Human) │         │          │         │  (LLM)   │         │submit_plan│
└────┬────┘         └────┬─────┘         └────┬─────┘         └────┬─────┘
     │                   │                    │                    │
     │  "Add a competitor │                    │                    │
     │   analysis step"  │                    │                    │
     │──────────────────▶│                    │                    │
     │                   │  resume graph      │                    │
     │                   │  with feedback ───▶│                    │
     │                   │                    │  calls submit_plan │
     │                   │                    │───────────────────▶│
     │                   │                    │                    │
     │                   │                    │  ToolMessage:      │
     │                   │                    │  "user wants       │
     │                   │                    │   changes — update │
     │                   │                    │   todos, then call │
     │                   │                    │   submit_plan"     │
     │                   │                    │◀───────────────────│
     │                   │                    │                    │
     │                   │                    │  edits its own     │
     │                   │                    │  todo steps  ◀─── │ (Agent work)
     │                   │                    │                    │
     │                   │                    │  calls submit_plan │
     │                   │                    │  again ───────────▶│
     │                   │                    │                    │
     │                   │  interrupt() ◀─────────────────────────│
     │  new plan + gate  │                    │                    │
     │◀──────────────────│                    │                    │
     │                   │                    │                    │
```

**The key insight:** the `submit_plan` tool is a messenger, not an editor. It passes the human's feedback to the agent as a tool result and pauses again. The agent is the one that reads the feedback and rewrites the plan steps. The tool never touches the plan content itself.

```python
def submit_plan(title, scope, runtime) -> Command:
    plan = project_plan(runtime.state.get("todos"))   # from the agent's own todos
    decision = interrupt({"plan": plan.model_dump()})  # ONE interrupt, no loop

    if decision["type"] == "approve":
        locked = plan.model_copy(update={"status": "locked"})
        return Command(update={
            "plan": locked.model_dump(),
            # Production approval was staged before the worker resumed.
            # This update runs when the worker drains that pending resume value.
            "messages": [ToolMessage("Plan approved and locked. Execute the steps now.")],
        })

    # refine: DON'T touch the plan — route the feedback back to the agent
    bumped = plan.model_copy(update={"modification_count": plan.modification_count + 1})
    return Command(update={
        "plan": bumped.model_dump(),
        "messages": [ToolMessage(
            f"The user requested changes: {decision['feedback']}\n"
            "Update the todo list, then call submit_plan again. Do not retrieve anything yet.")],
    })
```

Here is the complete picture of both outcomes — approve and refine — showing exactly what the tool returns to the agent each time:

```text
                    AGENT calls submit_plan tool
                              │
                              ▼
                    Tool builds plan from todos
                    Tool calls interrupt()
                    ┌─────────────────────────────┐
                    │  GRAPH PAUSES               │
                    │  HTTP request ends          │
                    │  Plan shown in browser      │
                    └─────────────────────────────┘
                              │
              ┌───────────────┴────────────────────┐
              │ Human clicks APPROVE                │ Human sends REFINE feedback
              ▼                                     ▼
    Worker resumes graph                  API resumes graph
    interrupt() returns:                  interrupt() returns:
    {"type": "approve"}                   {"type": "refine",
                                           "feedback": "add X step"}
              │                                     │
              ▼                                     ▼
    Tool returns ToolMessage:             Tool returns ToolMessage:
    ┌────────────────────────┐            ┌────────────────────────────────┐
    │ "Plan approved and     │            │ "User requested changes: add X │
    │  locked. Execute the   │            │  step. Update the todo list,   │
    │  research steps now."  │            │  then call submit_plan again." │
    └────────────────────────┘            └────────────────────────────────┘
              │                                     │
              ▼                                     ▼
    AGENT reads ToolMessage               AGENT reads ToolMessage
    → runs query_cortex,                  → edits its own todo steps
      query_iqvia_gmi, etc.               → calls submit_plan again
    → writes report                       → back to interrupt() ↑
```

**The tool is the messenger in both directions.** Going in: it carries the plan to the human via `interrupt()`. Coming back: it tells the agent what the human decided. The agent never talks to the human directly — it only ever reads a ToolMessage.

Each refine round is one full trip: a new tool call, a new `interrupt()`, a new HTTP request. The tool stays dumb; the agent stays the sole author of the plan. The human's feedback reaches the agent as conversational input, never as a direct edit the code performs.

## The Plan Is A Read-Only Projection

The refine design has a prerequisite that also solves the idempotency problem: the agent never writes a `Plan` object directly. It writes plain todo items — natural-language steps with routing tags in their text — and the typed `Plan` is *derived* from those todos by a pure function every time it is needed.

```python
def submit_plan(title, scope, runtime) -> Command:
    todos = runtime.state.get("todos") or []
    plan = project_plan(todos, thread_id=thread_id, prior=prior)   # pure: same in, same out
    ...
```

Two payoffs:

- **The agent cannot corrupt the plan.** It can only write inconsistent todos; the projection is a sanitizing layer between raw model output and the typed structure the frontend receives. A malformed routing tag fails soft — the raw text stays visible in the step title, and the routing field is left unset for the executor to skip.
- **Resume is safe by construction.** Because `project_plan` is pure and the plan id is derived deterministically from the thread, re-running `submit_plan` on every resume rebuilds the bit-for-bit identical plan. A random id or a timestamp here would fork a different plan on each resume and break the `plan_id` validation above.

Refine, then, is just: the agent edits its todos, calls `submit_plan` again, and the projection produces the revised plan automatically. There is no plan object being mutated in place.

## The Gate Does Not Run On Every Turn

The full plan workflow is expensive and state-changing:

```text
write_todos -> submit_plan -> interrupt() -> approve/refine -> execute
```

That workflow should run only when the conversation needs a new approved plan. In the production agent, only two of the common turn shapes opened the gate:

| Turn type | Plan workflow? | What happens |
|---|---:|---|
| First question on a thread | Yes | Draft todos, submit the plan, pause for approval. |
| Follow-up that is a genuinely new topic | Yes | Advance `cycle`, mint a new `plan_id`, and approve a fresh plan. |
| Entity or metric gap with no safe default | No | Ask one terminal clarification before any todos exist. |
| Mid-retrieval source elicitation reply | No | Keep the locked plan; re-query only the step that asked back. |
| Extension, such as "also pull New Zealand" | No | Retrieve directly under the current locked plan. |
| Evidence reply or presentation change | No | Answer from held evidence or rebuild artifacts once. |

This is why the plan's lifetime matters. A locked plan keeps its `plan_id`, step ids, and carried evidence attached to the same cycle. Re-opening the gate for a source-elicitation reply would mint a new `plan_id`, orphan already-fetched results from the current cycle, and ask the human to approve the same question twice.

The rule is simple enough to review: clarification about the user's intended entity or metric happens before planning; source ambiguity can ride the plan gate; once a plan is locked, only a genuinely new question reopens planning.

## The State Machine Lives In Code

The plan has a lifecycle, and the code — not the model — owns every transition:

```text
DRAFT --(interrupt: human sees plan)--> DRAFT (modification_count++)   [refine]
      --(approve)--> LOCKED --> EXECUTING --> COMPLETE
```

- **DRAFT** can be modified, cannot be executed.
- **LOCKED** cannot be modified, can be executed.
- **EXECUTING** is guarded against a second concurrent run.
- **COMPLETE** is terminal.

The model only ever writes plan *content*, and only in DRAFT. The `locked` transition happens in code on approve, via `model_copy`. The model cannot accidentally lock or unlock a plan, and it cannot execute an unlocked one — because the gate structurally blocks continuation until the human resumes. If you try to prompt the model into enforcing this ("only proceed if the plan is approved"), you have put a state decision in the one place that cannot guarantee it.

## Concurrency: One Resume At A Time

Imagine a user opens the plan approval screen in two browser tabs. Both show the same plan. Both have an "Approve" button. If they click approve in tab A and then again in tab B half a second later, two HTTP requests hit your server at almost the same moment — both trying to resume the same graph thread.

LangGraph does not support two runs on the same thread at once. If both requests succeed, the graph executes the research plan twice in parallel, each writing to the same checkpoint. Whichever finishes last wins and overwrites the other's work. The user gets one result, one run's state is silently lost, and nothing logs an error.

The fix is a **claim**: before resuming, write a small record to the database that says "I own this thread right now." The write is conditional — it only succeeds if no one else already holds the claim. The second request races to write the same record, loses the race, and gets a `409 Conflict` back. Exactly one resume proceeds.

```text
Tab A clicks approve:
  → try to write CLAIM#thread-123 to database
  → nobody holds it yet, write succeeds
  → Tab A resumes the graph ✓

Tab B clicks approve (50ms later):
  → try to write CLAIM#thread-123 to database
  → record already exists, write fails
  → Tab B gets 409 Conflict ✓
```

In code, the conditional write looks like this:

```python
table.put_item(
    Item={
        "PK": f"CLAIM#{thread_id}",   # one record per thread
        "claim_token": token,          # a unique ID so only the holder can release it
        "expires_at": now + 1800,      # auto-expire after 30 min in case of crash
    },
    # only succeed if the record does not exist yet, or if it expired
    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
    ExpressionAttributeValues={":now": now},
)
```

The `expires_at` is the crash-recovery handle: if the first request crashes mid-run, its claim expires after 30 minutes and a retry can acquire it again. The `claim_token` UUID prevents a slow stream that outlived its lease from deleting a newer holder's claim when it eventually finishes.

This is the same pattern at a smaller scale as the worker lock in [Distributed Locks](./07-distributed-locks.md).

## An Implementation Path

Each step is runnable and testable before the next:

```text
1. Plan generation only. submit_plan takes a Plan and returns it. No interrupt. Validate content.
2. Add interrupt(). Resume from a test with Command(resume={"type": "approve"}).
3. Add the approve endpoint. Read the pending interrupt, validate, resume. Two real requests.
4. Add the refine path. Return feedback as a ToolMessage; let the agent edit and resubmit.
   Do NOT loop inside the tool or call an LLM from it.
5. Add validation. Put plan_id and interrupt_id in the payload; test the stale-tab case.
6. Add the async approve path. Persist resume decision, write `queued`, enqueue FIFO, return `202`.
7. Add the concurrency claim or worker lock. Two simultaneous approves -> one queued run, one conflict or safe duplicate.
```

The hardest step is 5: idempotency and stale-state bugs both surface there. Run the approve endpoint twice on the same payload and confirm the second call is a 409 or a safe no-op.

Once a plan is locked, the run itself has to survive worker crashes and reconnects — that is [Durable Async Agent Runs](./08-queue-and-worker-execution.md).

## Guardrails

- Treat everything before `interrupt()` as code that re-runs on every resume
- Put side effects after the interrupt, or make them idempotent
- Design the interrupt payload to carry both the human's decision inputs and the code's validation inputs
- Never loop inside the gate tool; refine is the agent's own loop
- Derive the plan from the agent's working state with a pure function; never let the model write typed plan fields directly
- Keep every status transition and concurrency check in code, not in the prompt
- Validate `plan_id` and `interrupt_id` before staging the resume decision, to reject stale-tab approvals
- In the production path, stage approval in the checkpoint, queue work, and let the worker resume with no input

---

!!! check "You should now understand"
    - Why planning should happen before expensive execution
    - How `interrupt()` turns one user task into multiple HTTP requests
    - Why everything before `interrupt()` must be deterministic or idempotent
    - Why the interrupt payload must contain both human-readable plan data and machine-checkable validation data
    - Why refine belongs in the agent loop, not inside the tool function

??? question "Try this"
    **A teammate wants to call the LLM from inside `submit_plan()` when the human asks for a refinement. Why is that the wrong boundary?**

    ??? success "Answer"
        The plan would now be authored by a separate out-of-band model call that does not share the agent's full prompt, tools, state, or conversation context. The gate tool should pause exactly once and return the human's feedback as a tool result. The agent loop should read that feedback, update the plan, and call `submit_plan()` again.

*Next: [Step 4 · Identifiers](05-identifiers.md) — once a plan can pause, refine, and queue, you need to name the thread, plan, pause, attempt, stream events, and ownership token precisely.*
