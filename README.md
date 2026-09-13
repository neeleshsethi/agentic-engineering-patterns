# Agentic Engineering Patterns

Production engineering patterns for building reliable deep agents.

📖 **Read online:** https://neeleshsethi.github.io/agentic-engineering-patterns/

This repository documents real-world lessons learned while building production agentic systems with LangGraph, Python, DynamoDB, and Server-Sent Events (SSE). It is written for engineers and interns who can build ordinary APIs, but are new to the extra failure modes that appear when an agent can pause, resume, remember, stream, and call tools.

Unlike introductory tutorials, these articles focus on problems that do not crash your application. They quietly produce incorrect results while everything still looks healthy from the outside.

The running theme is simple: a deep agent is not just a prompt around a model. It is a distributed system with memory, checkpoints, human approval gates, locks, streams, and persistence boundaries.

## Article Series

The series follows the order you would actually **build** a production deep agent. First understand what the system is and the principles that keep it correct. Then add one production mechanism at a time. The final case study only comes after the reader has the concepts needed to diagnose it.

1. [Problem, Design Decisions, and Implementation Plan](articles/01-problem-design-implementation-plan.md) — the production problem, the design principles, and the build order.
2. [End-to-End Architecture and Orchestrator Flow](articles/02-end-to-end-architecture-and-orchestrator-flow.md) — the full approve-to-worker lifecycle.
3. [State and Checkpoints](articles/02-state-and-checkpoints.md) — memory that survives one request.
4. [Context Injection](articles/03-context-injection.md) — the right context at the right model boundary.
5. [Planning and Human Approval](articles/04-planning-and-human-approval.md) — show the plan before expensive work begins.
6. [Identifiers](articles/05-identifiers.md) — which names stay stable across replay and which must change.
7. [Streaming and Background Work](articles/06-streaming-and-background-work.md) — progress streams without letting browser disconnects cancel required work.
8. [Distributed Locks](articles/07-distributed-locks.md) — one worker owns a run; acquire and release stay atomic.
9. [Queue and Worker Execution](articles/08-queue-and-worker-execution.md) — approved work leaves the API process and becomes replay-safe.
10. [The Orchestrator Prompt](articles/09-orchestrator-prompt.md) — prompt shapes judgment; code enforces boundaries.
11. [Nine Silent Failures](articles/10-nine-silent-failures.md) — the capstone: nine real bugs caught before release.

## Repository Structure

- `articles/` - Long-form technical blogs
- `examples/` - Minimal runnable code samples
- `docs/` - Architecture and design notes
- `diagrams/` - System diagrams

## Tech Stack

- LangGraph
- Python 3.11+
- DynamoDB
- Starlette
- Server-Sent Events
- asyncio

## Goal

Help engineers build production-ready agentic systems by documenting the kinds of failures that traditional tutorials rarely discuss.

## Learning Path For Python Interns

If you are new to deep agents, **read the [Article Series](#article-series) in order.** The goal is not to memorize LangGraph APIs. The goal is to build the production mental model: what state existed before this step, what state must exist after it, and what can go silently wrong at the boundary.

The `examples/` folders contain small before/after snippets for the same concepts. Read them when a production code sample in the long article feels too compressed. For each example, ask two questions: what state existed before this function ran, and what state must be true after it finishes?
