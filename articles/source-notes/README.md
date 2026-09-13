# Source Notes: A Deep Agent, End to End

These three notes are a faithful reconstruction of the internal design docs behind a production
**deep agent** — a human-in-the-loop research orchestrator that plans, gets a human to approve the
plan, then executes it durably on a worker fleet. They are the *source material* the published
[Silent Failures in Agentic AI](../01-silent-failures-overview.md) series draws from, at full
depth.

Names are genericized (product → "the platform", brand → `BRAND_A`, sources → `SOURCE_A..D`,
tables → `app-{env}-deep-*`, retrieval tool → `query_source`). Framework names (LangGraph,
deepagents, DynamoDB, SQS) are real. Raw OCR of the originals is kept in the repo's local `data/extracted/` directory (gitignored — not published).

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

The published [`articles/01–06`](../01-silent-failures-overview.md) are the teaching version —
genericized to "a research agent", no product context. These notes are where each of those
failures actually came from:

| Published article | Grounded by these notes |
|---|---|
| [02 – Context injection](../02-context-injection.md) | `before-resume` Step 3 (transient injection, the `[/User context]` boundary) · `orchestrator-prompt-annotated` Step 4 EXECUTE / Bug 1 (the entity-override the injection causes) |
| [03 – LangGraph state](../03-langgraph-state.md) | `before-resume` Part 1 (state channels, `LastValue` footguns) · `orchestrator-prompt-annotated` Part 4 (the read-only Plan projection) |
| [04 – SSE cancellation](../04-sse-cancellation.md) | `life-of-a-deep-run` Step 4 (SSE tail, `Last-Event-ID` replay=live) · termination-on-status (E-cases) |
| [05 – Distributed locks](../05-distributed-locks.md) | `life-of-a-deep-run` Part 3 (the keycard lock) & Part 4 (zombie fencing) · `before-resume` Part 4 (the resume claim — same pattern, smaller lease). *The published article now includes the tenure-token and write-fence sections drawn from these.* |
| [06 – Nine silent failures](../06-nine-silent-failures-langgraph-research-agent.md) | The edge-case catalogs in all three notes; Bug 1 in `orchestrator-prompt-annotated` is the canonical entity-override failure |
| [07 – Durable async agent runs](../07-durable-async-agent-runs.md) | The whole of `life-of-a-deep-run`, genericized: two-table split, one-worker-per-thread FIFO, heartbeat-on-progress, `seq`/`idem_key`, replay==tail, crash recovery, dead-letter paging |
| [08 – Human-in-the-loop plan approval](../08-human-in-the-loop-plan-approval.md) | `orchestrator-prompt-annotated` Part 3 (build-it-yourself) + Part 2/4 (gate internals, read-only Plan projection) · `before-resume` Parts 4–5 (refine/approve, the resume claim) |
| [09 – Designing the orchestrator prompt](../09-designing-the-orchestrator-prompt.md) | `orchestrator-prompt-annotated` Parts 1, 5, 6 (annotated prompt, allow-list middleware, ToolMessage continuation, grounding) · Bug 1 as the load-bearing prompt-only invariant |

The published series now covers all three source notes: **05 + 07** carry `life-of-a-deep-run`;
**08 + 09** carry `orchestrator-prompt-annotated`; and `before-resume` is folded across **08**
(refine/approve/claim) and **03** (state channels). The source notes remain the full-fidelity
reference behind the genericized articles.
