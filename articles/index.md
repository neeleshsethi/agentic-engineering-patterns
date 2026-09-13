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

The series is a build path, not a bug catalog. Read it as if you are implementing the system in this order.

### First, understand the project
1. [Problem, Design Decisions, and Implementation Plan](01-problem-design-implementation-plan.md) — the production problem, the design principles, and the build order.
2. [End-to-End Architecture and Orchestrator Flow](02-end-to-end-architecture-and-orchestrator-flow.md) — the full approve-to-worker lifecycle before the implementation details.

### Then build it step by step
3. [State and Checkpoints](02-state-and-checkpoints.md) — give the agent memory that survives one request.
4. [Context Injection](03-context-injection.md) — put the right user and business context at the right model boundary.
5. [Planning and Human Approval](04-planning-and-human-approval.md) — make the agent show its plan before expensive work begins.
6. [Identifiers](05-identifiers.md) — name the run, plan, pause, attempt, stream event, lock tenure, and fact provenance correctly.
7. [Streaming and Background Work](06-streaming-and-background-work.md) — let the user watch progress without letting the browser own correctness.
8. [Distributed Locks](07-distributed-locks.md) — make exactly one worker own a run, even under double-clicks, crashes, and retries.
9. [Queue and Worker Execution](08-queue-and-worker-execution.md) — move approved work out of the API process and make replay safe.
10. [The Orchestrator Prompt](09-orchestrator-prompt.md) — decide what the prompt shapes and what code must enforce.
11. [Clarifications](09a-clarifications.md) — handle ask-back turns correctly without confusing source elicitation with plan approval resume.
12. [Code Components and Organization](09b-code-components-and-organization.md) — keep `agent/deep/` split by ownership so bugs have an obvious home.
13. [Plan Lifecycle](09c-plan-lifecycle.md) — decide when to replan, extend the locked plan, or skip planning.
14. [Reactive Mode Prompt](09d-reactive-mode-prompt.md) — understand the one-pass router and its clarification rules.
15. [Interactive Lessons](lessons/index.html) — short deck-style walkthroughs for the full deep-agent run, state channels, provenance, and clarifications.

### Finally, review the scars
16. [Nine Silent Failures](10-nine-silent-failures.md) — the final case study. Each failure should now feel recognizable, not mysterious.

## Reference
- [Glossary](00-glossary.md) — every term, defined once.
- [Source Notes](source-notes/README.md) — the deeper, full-fidelity design notes the articles distill.

---

*This is a teaching series. If a passage loses you, that is a bug in the writing — ask, and it gets fixed.*
