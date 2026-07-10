# Agentic Engineering Patterns

Production engineering patterns for building reliable AI agents.

This repository documents real-world lessons learned while building production agentic systems with LangGraph, Python, DynamoDB, and Server-Sent Events (SSE).

Unlike introductory tutorials, these articles focus on problems that do not crash your application. They quietly produce incorrect results while everything still looks healthy from the outside.

## Article Series

### 1. Silent Failures in Agentic AI

How nine bugs returned `200 OK` while producing incorrect answers.

Topics include:

- Context injection
- LangGraph state channels
- Middleware
- Async cancellation
- DynamoDB persistence
- Distributed locking

Current articles:

- `articles/01-silent-failures-overview.md`
- `articles/02-context-injection.md`
- `articles/03-langgraph-state.md`
- `articles/04-sse-cancellation.md`
- `articles/05-distributed-locks.md`
- `articles/06-nine-silent-failures-langgraph-research-agent.md`

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
