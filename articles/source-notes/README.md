# Source Notes: A Deep Agent, End to End

These three notes walk through the design of one **deep agent** at full depth: a
human-in-the-loop research orchestrator that plans, gets a human to approve the plan, then
executes it durably on a worker fleet. The published
[Agentic Engineering Patterns](../01-problem-design-implementation-plan.md) chapters teach the same
system one mechanism at a time; these notes keep the detail the chapters leave out.

All product, data-source and infrastructure names are illustrative (the platform, `BRAND_A`,
`SOURCE_A..D`, tables `{env}-agent-*`, retrieval tools sometimes collapsed to `query_source`).
Framework names (LangGraph, deepagents, DynamoDB, SQS) are real. The full orchestrator uses a
seven-tool allow-list; `query_source` is a teaching simplification where noted.

## The three notes

| # | Note | What it covers |
|---|---|---|
| 1 | [before-resume.md](./before-resume.md) | **The proposal turn.** From question arrival to the approve click: `prepare_memory`, the disposable-projection principle, the `write_todos → submit_plan → interrupt()` planning loop, the parked thread, the refine loop and resume-claim, approve. |
| 2 | [orchestrator-prompt-annotated.md](./orchestrator-prompt-annotated.md) | **The orchestrator itself.** The annotated system prompt (PLAN→FOLLOW-UP phase machine), gate internals (`submit_plan`, `project_plan`), the "don't loop inside the tool" insight, Bug 1 (entity override), and a 6-step build-it-yourself guide. |
| 3 | [life-of-a-deep-run.md](./life-of-a-deep-run.md) | **Async execution.** From the approve click onward: SQS FIFO one-worker-per-thread, the checkpoint/event two-table split, `LeaseExtendingSaver`, `seq`/`idem_key`, the SSE tail, the lock, zombie fencing, a 16-entry edge-case catalog. |

## Reading order

Read them in **chronological order of a single run**: `before-resume` → `orchestrator-prompt-annotated`
→ `life-of-a-deep-run`. Note 1 sets up the state model and the interrupt; note 2 explains the
orchestrator that produces the plan note 1 parks; note 3 picks up after approval and runs it.

If you only want one idea from the whole set, it's this:

> **The LLM generates content; the code manages the state machine.** Every hard boundary — gate
> sequencing, tool availability, status transitions, concurrency — is code and spends zero prompt
> tokens. The prompt is reserved for *judgment* and for invariants the code physically cannot
> enforce.

## How these map to the published series

The published chapters are the teaching version. These notes go deeper on each mechanism:

| Published chapter | Covered in depth by |
|---|---|
| [Chapter 2 · End-to-end architecture and orchestrator flow](../02-end-to-end-architecture-and-orchestrator-flow.md) | All three notes: async approval, pending resume writes, SQS FIFO, worker resume with `None`, stream curation, seven-tool allow-list |
| [Step 1 · State and checkpoints](../02-state-and-checkpoints.md) | `before-resume` Part 1 (state channels, `LastValue` footguns, reducer vs last-value vs pending resume writes) |
| [Step 2 · Context injection](../03-context-injection.md) | `before-resume` Step 3 (transient injection, the `[/User context]` boundary) · `orchestrator-prompt-annotated` Step 4 EXECUTE / Bug 1 (the entity override the injection causes) |
| [Step 3 · Planning and human approval](../04-planning-and-human-approval.md) | `orchestrator-prompt-annotated` Part 3 (build-it-yourself) + Parts 2/4 (gate internals, read-only Plan projection) · `before-resume` (async approve path) |
| [Step 4 · Identifiers that hold a run together](../05-identifiers.md) | `before-resume` identity table: `interrupt_id` rejects stale approvals; `plan_id` alone is not enough |
| [Step 5 · Streaming and background work](../06-streaming-and-background-work.md) | `life-of-a-deep-run` Step 4 (SSE tail, `Last-Event-ID` replay = live) · termination-on-status (E-cases) |
| [Step 6 · Distributed locks](../07-distributed-locks.md) | `before-resume` (the API's resume claim, `CLAIM#{thread_id}` / `RESUME`) · `life-of-a-deep-run` Parts 3–4 (the worker's run lock, `lock#{thread_id}` / `0`, and zombie fencing) |
| [Step 7 · Queue and worker execution](../08-queue-and-worker-execution.md) | The whole of `life-of-a-deep-run`: two-table split, one-worker-per-thread FIFO, heartbeat-on-progress, `seq`/`idem_key`, replay == tail, crash recovery, dead-letter paging |
| [Step 8 · Orchestrator prompt](../09-orchestrator-prompt.md) | `orchestrator-prompt-annotated` Parts 1, 5, 6 (annotated prompt, allow-list middleware, ToolMessage continuation, grounding) |
| [Deep dive · Clarifications](../09a-clarifications.md) | `orchestrator-prompt-annotated` (clarification families, deterministic source elicitation, fresh invocation vs resume) |
| [Deep dive · Code components and organization](../09b-code-components-and-organization.md) | All three notes (module map, middleware order and loop budgets, provenance/citation ownership) |
| [Deep dive · Plan lifecycle](../09c-plan-lifecycle.md) | `orchestrator-prompt-annotated` Part 3 and `before-resume` (when the plan workflow runs, extension turns, elicitation replies) |
| [Deep dive · Reactive mode prompt](../09d-reactive-mode-prompt.md) | `orchestrator-prompt-annotated` (prompt layers, clarification guard, elicitation relay) |
| [Deep dive · The event feed and idempotency](../09e-event-feed-and-idempotency.md) | `life-of-a-deep-run` (`seq` vs `idem_key`, the three-way collision branch) |
| [Deep dive · Carryover and the data manifest](../09f-carryover-and-the-data-manifest.md) | `before-resume` (`prepare_memory`, the carryover row) |
| [Capstone · Nine silent failures in one agent](../10-nine-silent-failures.md) | The edge-case catalogs in all three notes; Bug 1 in `orchestrator-prompt-annotated` is the canonical entity-override failure |
