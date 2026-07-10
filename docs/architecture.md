# Architecture

This repository is organized around one production concern: semantic correctness under non-failing conditions.

## Layers

- `articles/` explains the patterns and failure modes
- `examples/` demonstrates minimal reproductions and fixes
- `docs/` records architecture, rationale, and checklists
- `diagrams/` stores visual references used by the written material

## Core System Themes

- Graph orchestration with LangGraph
- Stateful execution with durable persistence
- Streaming interfaces over SSE
- Concurrency control with DynamoDB-backed coordination
