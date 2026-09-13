# Deep Dive · Code Components and Organization

A production deep agent should not live in one `agent.py` file. The code layout is part of the safety model: each module group should own one boundary in the state machine.

The organizing question is:

> If this fails silently, which file should a reviewer open first?

## Package Layout

The `agent/deep/` package is easiest to understand as seven groups:

| Group | Modules | Owns |
|---|---|---|
| Graph and control flow | `graph.py`, `gate.py`, `plan.py`, `contract.py` | Graph assembly, plan approval, todos-to-plan projection, and shared names. |
| Middleware | `proposal_context.py`, `manifest.py`, `sufficiency.py`, `retrieval_budget.py`, `exit_path.py` | Context injection, held-data manifest, completeness checks, loop bounds, and report writing. |
| Execution and analysis | `analyst.py`, `interpreter.py` | Bounded code analysis and sandboxed interpreter calls. |
| Async worker plumbing | `run_queue.py`, `run_state.py`, `worker_lock.py`, `resume_claim.py`, `resume_decision.py`, `checkpointer.py`, `token_escrow.py` | Durable approval execution: queue, status, locks, double-resume prevention, pending resume writes, shared checkpoints, and worker auth. |
| Event feed | `curator.py`, `event_log.py`, `events_table.py` | Stable user-facing events, idempotent writes, and table plumbing for the SSE tail. |
| Provenance, citations, and charts | `provenance.py`, `citations.py`, `chart_tool.py`, `chart_state.py`, `chart_anchors.py`, `chart_adapter.py` | Retrieval identity, citation joins, source cards, chart specs, and report-cycle anchoring. |
| Prompts | `prompts/orchestrator_prompts.py`, `report_prompts.py`, `sufficiency_prompts.py`, `analyst_prompts.py` | Planning, follow-up routing, report writing, sufficiency grading, and code-analysis behavior. |

The point is not the exact filenames. The point is that each module group has one reason to change.

## Graph And Control Flow

`graph.py` assembles the graph, tools, and middleware. It should wire the system together, not absorb every policy.

`gate.py` owns the plan-approval seam:

- `submit_plan`
- the one `interrupt()`
- plan status transitions
- `cycle`
- `entity_groups`
- approve/refine/decline behavior

`plan.py` owns the read-only projection from model-authored todos to a typed plan. The agent writes todos; code derives the plan shape. That keeps resume deterministic because the same durable todos project to the same plan.

`contract.py` is the shared string surface. Tool names, node names, and event names used by both graph and curator should not be duplicated as loose literals.

## Middleware

Middleware owns the model-call and exit boundaries:

| Middleware | Role |
|---|---|
| `UserContextMiddleware` | Injects persona/profile/default context into model calls. |
| `DataManifestMiddleware` | Renders held data so the orchestrator can reuse prior retrievals. |
| `AllowedToolsMiddleware` | Removes tools the model must not see. |
| `PlaybookReadGuard` | Fences `read_file` to approved playbook paths. |
| `SufficiencyGateMiddleware` | Checks completeness before report writing and may allow one gap-fill round. |
| `RetrievalBudgetMiddleware` | Meters retrieval rounds, not individual calls. |
| `DeepExitPathMiddleware` | Generates the final report and assembly outputs only when evidence is reportable. |

The ordering matters. `after_agent` hooks run in reverse registration order, so sufficiency must run before the exit path writes the report. Retrieval budget uses model-call hooks so it can meter loop depth without disturbing report ordering.

## Async Worker Plumbing

The worker modules own the difference between "approved" and "executed":

```text
approve endpoint
  -> resume_decision.py writes pending resume value
  -> run_state.py writes queued status
  -> run_queue.py enqueues {thread_id, run_id}

worker
  -> run_queue.py receives message
  -> worker_lock.py claims thread
  -> checkpointer.py reloads graph state
  -> graph resumes with input=None
```

`resume_claim.py` prevents double-click or stale-tab resume races. `worker_lock.py` prevents two workers from writing the same thread at once. `token_escrow.py` exists because the worker, not the browser request, eventually needs data-source credentials.

These modules should stay boring. Their job is durable execution, not model judgment.

## Event Feed

The frontend should never consume raw framework events. The event feed modules form a projection:

| Module | Role |
|---|---|
| `curator.py` | Converts LangGraph/DeepAgents events into stable UI frames. |
| `event_log.py` | Writes frames with idempotency keys so replay does not duplicate them. |
| `events_table.py` | Owns DynamoDB table access for the SSE tail. |

This keeps narration separate from truth. Checkpoints are the source of truth; event rows are the user's progress feed.

## Provenance, Citations, And Charts

This group owns attribution. It answers "which retrieval produced this fact?"

| Module | Role |
|---|---|
| `provenance.py` | Builds deterministic `provenance_id`s, filters current-cycle results, detects pending elicitation, and creates cycle stamps. |
| `citations.py` | Maps report markers like `[3]` back to the correct source result. |
| `chart_tool.py` | Lets the report writer request chart generation. |
| `chart_state.py` | Atomically stores chart catalog/spec state. |
| `chart_anchors.py` | Verifies inline `{{chart:ID}}` markers against the same-cycle chart catalog. |
| `chart_adapter.py` | Adapts deep report data to the chart contract. |

Do not key citations on display order. Retrieval ordinals, source ordinals, and card display ordinals are different concepts.

## Prompts

Prompts are code assets, not copy blobs. Split them by job:

| Prompt file | Role |
|---|---|
| `orchestrator_prompts.py` | Plan, submit, refine, execute, and follow-up routing. |
| `report_prompts.py` | Final report writing over approved evidence. |
| `sufficiency_prompts.py` | Completeness grading before report writing. |
| `analyst_prompts.py` | Bounded code-analysis loop behavior. |

The prompt shapes judgment. Code enforces boundaries. If a prompt line is trying to prevent an unauthorized tool call, move that rule into middleware.

## Where Bugs Should Live

Use the package map during code review:

| If the bug is about... | Look first in... |
|---|---|
| A plan was proposed, approved, refined, or locked incorrectly | `gate.py`, `plan.py` |
| A stale default or held-data inventory changed model behavior | `proposal_context.py`, `manifest.py` |
| A report was written too early, too late, or over no evidence | `sufficiency.py`, `exit_path.py`, `provenance.py` |
| Retrieval loops went too deep or punished parallelism | `retrieval_budget.py` |
| A run executed twice or did not resume after approval | `resume_claim.py`, `resume_decision.py`, `run_queue.py`, `worker_lock.py` |
| Progress duplicated, disappeared, or rendered oddly | `curator.py`, `event_log.py` |
| Citations, cards, or charts attached to the wrong evidence | `provenance.py`, `citations.py`, `chart_anchors.py`, `chart_state.py` |

This is how code organization becomes an engineering pattern: it makes the next silent failure easier to localize before it reaches production.

---

!!! check "You should now understand"
    - Why `agent/deep/` should be grouped by state-machine ownership
    - Which modules own planning, middleware, async execution, event feed, provenance, charts, and prompts
    - Why code organization helps reviewers localize silent failures

??? question "Try this"
    **A duplicate report appears after SQS redelivery. Which module group do you inspect first?**

    ??? success "Answer"
        Start with provenance and event-feed ownership. `provenance.py` should produce the same cycle stamp for the same evidence set, so `reported_cycle` can block duplicate report generation. `event_log.py` should dedupe replayed progress frames by idempotency key, not by sequence number.
