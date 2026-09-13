# Agentic Engineering Patterns

**Production agent systems fail in a way ordinary web apps do not: they return `200 OK` and still give the wrong answer.**

The process runs, the logs look normal, the stream closes cleanly — and the user gets a confident report about the wrong country, or a plan that executed twice, or an answer built on a number the model made up. This series is about that class of bug, and the handful of engineering patterns that prevent it.

It is written for an engineer who has built normal Python web APIs and is now looking at an agent system for the first time. You do **not** need to know LangGraph going in. You do need to be willing to ask one question of every code snippet: *what was saved before this ran, and what is saved after?* That question catches most silent failures.

## The one idea

Everything here turns on a single distinction:

- **Transport success** — the request finished, nothing threw.
- **Semantic success** — the right context, state, lock, plan, and persistence rules actually held.

A silent failure is any moment those two drift apart. Keep the [glossary](00-glossary.md) open beside you; every term is defined there the first time it appears.

## How to read this

The series builds up. Read it in order the first time.

### Foundations — the shapes of the bug
1. [Silent Failures](01-silent-failures-overview.md) — the failure shape, and why uptime dashboards miss it.
2. [Context Injection](02-context-injection.md) — attaching the wrong context at the wrong boundary.
3. [LangGraph State](03-langgraph-state.md) — how "I didn't write that key" silently keeps stale data.

### Mechanisms — the moving parts
4. [SSE Cancellation](04-sse-cancellation.md) — when a client disconnect kills work you needed to finish.
5. [Distributed Locks](05-distributed-locks.md) — one run at a time, across replicas, even under crashes.
6. [Durable Async Runs](07-durable-async-agent-runs.md) — a run that survives a worker crash, a deploy, and a reconnect.

### Building an agent — putting it together
7. [Human-in-the-Loop Approval](08-human-in-the-loop-plan-approval.md) — pause a run for a human and resume it safely.
8. [Designing the Orchestrator Prompt](09-designing-the-orchestrator-prompt.md) — where prompt wording ends and code enforcement begins.

### Case study
9. [Nine Silent Failures](06-nine-silent-failures-langgraph-research-agent.md) — nine real bugs caught in review before shipping, each one an instance of the patterns above.

## Reference
- [Glossary](00-glossary.md) — every term, defined once.
- [Source Notes](source-notes/README.md) — the deeper, full-fidelity design notes the articles distill.

---

*This is a teaching series. If a passage loses you, that is a bug in the writing — ask, and it gets fixed.*
