# Context Injection

Context injection is one of the easiest ways to create a system that appears stable while producing the wrong answer. The agent still runs. The prompt still looks complete. But the wrong context is attached at the wrong boundary.

## Common Failure Modes

- Injecting user-specific data into a shared system prompt
- Forgetting to attach tool results to the state before the next node executes
- Reusing stale context across retries or resumed executions

## Production Impact

These bugs often show up as inconsistent answers rather than hard failures. The same request may work in one path and fail in another because the prompt assembly is not deterministic across code paths.

## Practical Guardrails

- Centralize prompt construction
- Make context sources explicit in typed state
- Snapshot final prompt inputs in debug environments
- Test prompt assembly independently from model calls
