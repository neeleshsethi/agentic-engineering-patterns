# LangGraph State

LangGraph makes long-running orchestration tractable, but correctness depends on disciplined state design. A graph can compile and run while silently losing information between nodes.

## Failure Modes

- Multiple nodes writing to the same key with incompatible assumptions
- Mutable objects shared across branches
- Missing reducers for channels that should merge rather than replace

## What To Watch

The problem is rarely that state is absent. More often, state exists but has the wrong shape, lifetime, or merge semantics.

## Guardrails

- Use clearly typed state models
- Separate durable state from ephemeral execution metadata
- Review channel merge behavior as part of code review
- Add tests that assert state evolution across multiple node transitions
