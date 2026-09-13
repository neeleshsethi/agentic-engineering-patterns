# Deep Dive · Plan Lifecycle

The deep plan lifecycle is not "plan on every user message." A plan is a durable, approved execution contract. Once it is locked, follow-up turns either open a new plan cycle, extend the current cycle, or avoid planning altogether.

## The Full Plan Workflow

The full workflow is:

```text
write_todos -> submit_plan -> interrupt() -> approve/refine -> execute
```

That workflow is expensive and state-changing. It creates a plan, exposes it to a human, waits for approval, locks the plan, and only then lets retrieval run.

## When We Replan

Replanning means the system opens a new cycle and mints a fresh `plan_id`.

Use a new plan when:

- The user asks the first question on a thread.
- The user asks a follow-up that is genuinely a new topic.
- The old plan is complete and the next question needs a different research shape.

```text
Turn 1: "Compare BRAND_A sales in Germany and France."
  -> plan-thread-123-1

Turn 2: "Now analyze launch uptake in Japan."
  -> plan-thread-123-2
```

The second turn is not a small addition to the first approved question. It needs a fresh cycle, fresh todos, fresh approval, and a new `plan_id`.

## When We Extend The Locked Plan

An extension keeps the current locked plan and retrieves under the same `plan_id`. It is for small additions to the current approved question.

```text
User: "Also pull New Zealand."
```

The system should not send the user back through approval if the extension is still part of the same approved research intent. It retrieves directly under the current locked plan, so carried evidence stays attached to the same cycle.

Use extension when:

- The user adds a market, time slice, metric, or source to the same question.
- The current plan is locked and still relevant.
- The result should be combined with the current evidence set.

## When We Do Not Plan

Some turns should not call `write_todos` or `submit_plan` at all.

| Turn type | Why no plan? | What happens |
|---|---|---|
| Entity or metric gap | A plan would be underspecified. | Ask a terminal clarification before planning. |
| Source elicitation reply | The plan is already locked. | Re-query only the asking step. |
| Evidence reply | The answer is already in held evidence. | Answer from `source_results`. |
| Presentation change | Retrieval is not needed. | Run `rebuild_artifacts` once. |

The critical case is source elicitation. If the user answers "retail" after a source asked "retail or non-retail?", the system must not replan. It should invoke the graph normally on the same `thread_id`, keep the locked `plan_id`, and re-query only the open step.

## Turn Routing Table

| Turn type | Plan workflow? | What happens |
|---|---:|---|
| First question on a thread | Yes | Create todos, submit a plan, pause at the gate, then execute after approval. |
| New-topic follow-up | Yes | Advance `cycle`, mint a fresh `plan_id`, and approve a new plan. |
| Entity or metric gap | No | Ask a terminal clarification before any plan exists. |
| Mid-retrieval source elicitation reply | No | Keep the locked plan; re-query only the step that asked back. |
| Extension of current ask | No | Retrieve directly under the current locked plan. |
| Evidence reply or presentation change | No | Answer from held evidence or run `rebuild_artifacts` once. |

## The Invariant

Once a plan is locked, nothing short of a genuinely new question should reopen approval.

Replanning an elicitation reply would detach already-fetched evidence from the current cycle because current-cycle evidence is filtered by `plan_id`. Keeping the locked plan is what allows partial results to carry forward safely.

---

!!! check "You should now understand"
    - When a deep turn should create a new plan cycle
    - When a turn should extend the current locked plan
    - When a turn should skip planning entirely
    - Why source elicitation replies must not reopen approval
