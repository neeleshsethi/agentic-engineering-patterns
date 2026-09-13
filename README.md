# Agentic Engineering Patterns

Production engineering patterns for building reliable deep agents.

📖 **Read online:** https://neeleshsethi.github.io/agentic-engineering-patterns/

This repository documents real-world lessons learned while building production agentic systems with LangGraph, Python, DynamoDB, and Server-Sent Events (SSE). It is written for engineers and interns who can build ordinary APIs, but are new to the extra failure modes that appear when an agent can pause, resume, remember, stream, and call tools.

Unlike introductory tutorials, these articles focus on problems that do not crash your application. They quietly produce incorrect results while everything still looks healthy from the outside.

The running theme is simple: a deep agent is not just a prompt around a model. It is a distributed system with memory, checkpoints, human approval gates, locks, streams, and persistence boundaries.

## Article Series

The series follows the order you would actually **build** a deep agent — design it, give it state, let its work run past a single request, then plan → queue → execute that work safely. The "silent failure" bugs are woven in where each mechanism is introduced, and the capstone collects nine of them in one real system.

### Part 0 — Design foundation

1. [What a Deep Agent Is](articles/01-silent-failures-overview.md) — a deep agent is not a prompt around a model; it is a distributed system with memory, checkpoints, gates, locks, and streams. The one distinction the series hangs on: **transport success ≠ semantic success**.
2. [Identifiers](articles/10-identifiers.md) — the half-dozen names a single run answers to (`thread_id`, `run_id`, `plan_id`, `interrupt_id`, `seq`, idempotency key, `claim_token`, provenance id) and the one axis that matters: which are stable across a replay and which are not.
3. [LangGraph State](articles/03-langgraph-state.md) — state channels and reducers, and how a graph can compile and run while silently losing information between nodes.
4. [Context Injection](articles/02-context-injection.md) — getting the right context to the right boundary; the wrong context attached at the wrong seam looks stable and answers wrong.

### Part 1 — Async tasks that run

5. [SSE & Background Tasks](articles/04-sse-cancellation.md) — a run must outlive the HTTP request; a client disconnect must not cancel work that is still needed.

### Part 2 — Plan → SQS → worker

6. [Human-in-the-Loop Approval](articles/08-human-in-the-loop-plan-approval.md) — the gate that pauses a run and shows the plan to a human before anything expensive or irreversible executes.
7. [The Orchestrator Prompt](articles/09-designing-the-orchestrator-prompt.md) — with the gate in place, design the prompt that produces the plan: the prompt shapes judgment, the code enforces boundaries.
8. [Distributed Locks](articles/05-distributed-locks.md) — guard worker concurrency with a lease and a tenure token, and understand what a safe takeover looks like.
9. [Durable Async Runs](articles/07-durable-async-agent-runs.md) — persist intent, enqueue to a FIFO queue, one worker per thread, the pickup ritual, and resuming a run on a different worker after a crash.

### Part 3 — Capstone

10. [Nine Silent Failures](articles/06-nine-silent-failures-langgraph-research-agent.md) — a case study: nine bugs caught in code review before shipping a LangGraph research agent, each returning `200 OK` while corrupting the meaning of the run.

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

If you are new to deep agents, **read the [Article Series](#article-series) in order (Part 0 → Part 3).** The goal is not to memorize LangGraph APIs. The goal is to learn what can silently go wrong when agent state outlives one HTTP request — and to build the machinery that keeps a run correct once it does.

The arc is deliberate: understand what a deep agent *is* and how it holds state (Part 0), let its work run past a single request (Part 1), then plan, approve, and execute that work safely on a worker (Part 2). The capstone (Part 3) shows nine of these failures in one real system, so read it last.

The `examples/` folders contain small before/after snippets for the same concepts. Read them when a production code sample in the long article feels too compressed. For each example, ask two questions: what state existed before this function ran, and what state must be true after it finishes?
