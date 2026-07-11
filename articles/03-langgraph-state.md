# LangGraph State

LangGraph makes long-running orchestration tractable, but correctness depends on disciplined state design. A graph can compile and run while silently losing information between nodes.

## Mental Model

LangGraph state is a dictionary with rules attached to each key. Those rules decide what happens when a node writes a new value.

The common default is `LastValue`: the latest write replaces the previous value. If a later graph invocation does not write that key, the old checkpointed value remains.

That last sentence is the trap. "I did not write this key" does not mean "clear this key." It means "keep whatever the checkpoint already had."

```text
Checkpoint before new turn
  raw_question = "sales in Germany"
  user_context = "default country: Australia"

New graph input
  messages = ["sales in France"]

Checkpoint after input merge
  messages = previous messages + ["sales in France"]
  raw_question = "sales in Germany"       <- stale
  user_context = "default country: Australia" <- stale
```

If `raw_question` and `user_context` must describe the new user turn, the caller must write them every time.

## State vs Checkpoint

State is the logical data your graph reads and writes. Checkpoint is the durable stored copy that lets LangGraph resume later.

```text
Python node returns {"plan": plan_dict}
  -> LangGraph applies channel update rules
     -> checkpointer stores durable snapshot
        -> later HTTP request resumes with same thread_id
           -> LangGraph reloads state from checkpoint
```

A checkpoint can hold more than the final visible state. During `interrupt()`, it also records pending writes and the interrupt payload needed for resume.

## Channel Types

`LastValue` is for one current value, such as the active plan or the current raw question.

Reducer channels are for values that should combine across writes. Message history usually uses a reducer so new messages append instead of replacing the full list.

```python
class DeepState(TypedDict):
    plan: NotRequired[dict[str, Any]]        # LastValue
    raw_question: NotRequired[str]           # LastValue
    user_context: NotRequired[str]           # LastValue
    messages: Annotated[list[AnyMessage], add_messages]
```

Use `LastValue` only when stale carry-over is acceptable or every new turn writes an explicit replacement.

## Failure Modes

- Multiple nodes writing to the same key with incompatible assumptions
- Mutable objects shared across branches
- Missing reducers for channels that should merge rather than replace
- Treating an omitted key as if it clears prior state
- Splitting channel declaration, initialization, and consumption across unrelated files

## What To Watch

The problem is rarely that state is absent. More often, state exists but has the wrong shape, lifetime, or merge semantics.

## Example: Factory For New Turn Input

Do not let every endpoint hand-build graph input. Put the required keys behind one factory.

```python
def build_proposal_graph_input(question: str, user_context: str) -> dict:
    return {
        "messages": [{"role": "user", "content": question}],
        "raw_question": question,
        "user_context": user_context,
    }
```

This turns a hidden convention into an API. A reviewer can now check one function instead of searching every route that starts the graph.

## Guardrails

- Use clearly typed state models
- Separate durable state from ephemeral execution metadata
- Review channel merge behavior as part of code review
- Add tests that assert state evolution across multiple node transitions
- Add tests where the second turn intentionally passes empty context
