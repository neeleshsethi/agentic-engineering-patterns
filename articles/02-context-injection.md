# Context Injection

Context injection is one of the easiest ways to create a system that appears stable while producing the wrong answer. The agent still runs. The prompt still looks complete. But the wrong context is attached at the wrong boundary.

## What Context Injection Means

Context injection is the act of adding extra text or data to the model input that the user did not type in that exact request.

Examples include user defaults, account permissions, previous entities, retrieved documents, tool results, and approved plan details.

The model sees one final message list. Your code may know which parts came from memory, tools, or the user. The model only sees the composed prompt unless you label the boundaries clearly.

```text
Database memory
  -> "User default country is Australia"

User message
  -> "Give me my sales"

Injected model input
  -> [User context]
     User default country is Australia
     [/User context]

     Give me my sales
```

This is useful because "my sales" needs context. It is dangerous because stale or over-salient context can override a later user correction.

## Common Failure Modes

- Injecting user-specific data into a shared system prompt
- Forgetting to attach tool results to the state before the next node executes
- Reusing stale context across retries or resumed executions
- Attaching context to a synthetic framework message instead of the real user message
- Failing to escape delimiters inside user-influenced memory text

## Production Impact

These bugs often show up as inconsistent answers rather than hard failures. The same request may work in one path and fail in another because the prompt assembly is not deterministic across code paths.

## Boundary Diagram

```text
HTTP request
  -> build graph input
     -> checkpoint reads prior state
        -> middleware injects request-time context
           -> model call
              -> model response written back to state
```

The safest place for request-only context is usually just before the model call. That keeps the checkpoint clean. The tradeoff is that the injected text is invisible when you inspect saved graph state.

## Example: Skip Synthetic Summary Messages

Some frameworks add synthetic messages during summarization. They may still be `HumanMessage` objects, but they are not user-authored input.

```python
for i in range(len(messages) - 1, -1, -1):
    msg = messages[i]

    if not isinstance(msg, HumanMessage):
        continue

    if msg.additional_kwargs.get("lc_source") == "summarization":
        continue

    messages[i] = HumanMessage(
        content=compose_with_context(msg.content, context),
        additional_kwargs=msg.additional_kwargs,
    )
    break
```

`lc_source == "summarization"` marks a framework-created message. Skipping it prevents your code from labeling a summary as if it were the user's latest words.

## Example: Escape Your Own Delimiter

If your format uses `[/User context]` as a closing marker, user-influenced memory must not be able to contain that exact marker.

```python
def compose_with_context(content: str, context: str) -> str:
    safe_context = context.replace("[/User context]", "[/user context]")
    return f"[User context]\n{safe_context}\n[/User context]\n\n{content}"
```

The replacement does not make prompt injection impossible. It closes one obvious escape hatch where text inside the trusted block forges the block ending.

## Practical Guardrails

- Centralize prompt construction
- Make context sources explicit in typed state
- Snapshot final prompt inputs in debug environments
- Test prompt assembly independently from model calls
- Test with summarized histories, multimodal messages, and empty context
