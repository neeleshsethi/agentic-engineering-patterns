# Human-in-the-Loop Plan Approval

Some agent actions are expensive, slow, or irreversible. A research plan that hits external APIs, runs database queries, or spends real money should be shown to a human *before* it executes, not after. This article builds that approval gate from the ground up.

The running example is a pharmaceutical commercial-analytics agent: it plans a set of data-retrieval steps, a human approves or refines the plan, and only then does it query anything. The identifiers are generic (`BRAND_A`, `SOURCE_A`, `query_source`), but the shape applies to any agent with a costly execute phase.

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
Command(resume={"type": "approve"})
# or
Command(resume={"type": "refine", "feedback": "add a competitive analysis step"})
```

LangGraph finds the paused graph in the checkpoint, re-runs the node, and delivers the `Command` as the return value of `interrupt()`. That is the entire mechanism: two requests, one checkpoint, one `interrupt()` call. Everything else in this article is built on top.

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
    "plan_id": plan.plan_id,             # which plan revision
    "thread_id": thread_id,              # which thread
    "owner_id": owner_id,                # who proposed it
    "interrupt_id": str(uuid.uuid4()),   # which exact interrupt
})
```

If the human cannot make an informed decision from the payload, they approve blindly. And if the code cannot identify *which* plan the human approved, a stale browser tab can approve a revision that no longer exists.

On the approve endpoint, read the payload back from the checkpointer and validate it *before* resuming:

```python
gate = read_pending_interrupt(thread_id)   # LangGraph exposes the pending writes
if gate["plan_id"]  != request.plan_id:   raise HTTPException(409)  # stale tab
if gate["owner_id"] != request.user_id:   raise HTTPException(403)  # wrong user
# only now:
graph.astream(Command(resume={"type": "approve"}), config)
```

If validation fails, the graph stays paused. No state change, no double execution.

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

```python
def submit_plan(title, scope, runtime) -> Command:
    plan = project_plan(runtime.state.get("todos"))   # from the agent's own todos
    decision = interrupt({"plan": plan.model_dump()})  # ONE interrupt, no loop

    if decision["type"] == "approve":
        locked = plan.model_copy(update={"status": "locked"})
        return Command(update={
            "plan": locked.model_dump(),
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

```text
agent: writes plan steps
agent: submit_plan -> interrupt -> human: "add BRAND_A competitive analysis"
       ToolMessage: "user requested changes... update the todo list, then call submit_plan again"
agent: edits its steps            <- the AGENT interprets the feedback
agent: submit_plan -> a NEW interrupt -> human: approve
       ToolMessage: "Plan approved and locked. Execute now."
agent: executes
```

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

Two browser tabs, or a double-clicked approve, can both try to resume the same thread. LangGraph does not support concurrent runs on one thread — they would execute twice and leave the final state to whichever finished last. Claim the thread with a conditional write before resuming.

```python
table.put_item(
    Item={"PK": f"CLAIM#{thread_id}", "SK": "RESUME",
          "claim_token": token, "expires_at": now + LEASE},
    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
    ExpressionAttributeValues={":now": now},
)
```

Two racing resumes hit this write; exactly one wins, the loser gets a 409. The lease lets a crashed resume stream recover, and a token-guarded release stops a slow stream from deleting a newer holder's claim. This is the same pattern, at a smaller scale, as the worker lock in [Distributed Locks](./05-distributed-locks.md).

## An Implementation Path

Each step is runnable and testable before the next:

```text
1. Plan generation only. submit_plan takes a Plan and returns it. No interrupt. Validate content.
2. Add interrupt(). Resume from a test with Command(resume={"type": "approve"}).
3. Add the approve endpoint. Read the pending interrupt, validate, resume. Two real requests.
4. Add the refine path. Return feedback as a ToolMessage; let the agent edit and resubmit.
   Do NOT loop inside the tool or call an LLM from it.
5. Add validation. Put plan_id and interrupt_id in the payload; test the stale-tab case.
6. Add the concurrency claim. Two simultaneous approves -> exactly one 200, one 409.
```

The hardest step is 5: idempotency and stale-state bugs both surface there. Run the approve endpoint twice on the same payload and confirm the second call is a 409 or a safe no-op.

Once a plan is locked, the run itself has to survive worker crashes and reconnects — that is [Durable Async Agent Runs](./07-durable-async-agent-runs.md).

## Guardrails

- Treat everything before `interrupt()` as code that re-runs on every resume
- Put side effects after the interrupt, or make them idempotent
- Design the interrupt payload to carry both the human's decision inputs and the code's validation inputs
- Never loop inside the gate tool; refine is the agent's own loop
- Derive the plan from the agent's working state with a pure function; never let the model write typed plan fields directly
- Keep every status transition and concurrency check in code, not in the prompt
- Validate `plan_id` and `interrupt_id` before resuming, to reject stale-tab approvals
