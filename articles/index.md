# Agentic Engineering Patterns

**Production agent systems fail in a way ordinary web apps do not: they return `200 OK` and still give the wrong answer.**

The process runs, the logs look normal, the stream closes cleanly — and the user gets a confident report about the wrong country, or a plan that executed twice, or an answer built on a number the model made up. This series is about that class of bug, and the handful of engineering patterns that prevent it.

It is written for an engineer who has built normal Python web APIs and is now looking at an agent system for the first time. You do **not** need to know LangGraph going in. You do need to be willing to ask one question of every code snippet: *what was saved before this ran, and what is saved after?* That question catches most silent failures.

## The one idea

Everything here turns on a single distinction:

- **Transport success** — the request finished, nothing threw.
- **Semantic success** — the right context, state, lock, plan, and persistence rules held.

A silent failure is any moment those two drift apart. Keep the [Glossary](glossary.md) open beside you; every term is defined there the first time it appears.

## How to read this

There is one path. Start at [Chapter 1](ch1-problem-and-plan.md) and follow the **Next** link at the bottom of each page. The chapters follow the order you would build the system in, so read them as if you are implementing it. The two **Bridge** pages are short primers placed right before the pages that need them.

The appendix is optional. The glossary is worth keeping open beside you. The interactive lessons replay the same material as step-through decks. The source notes go deeper than the chapters and are best left until you have finished the main path.

<!-- reading-order -->

---

*This is a teaching series. If a passage loses you, that is a bug in the writing — ask, and it gets fixed.*
