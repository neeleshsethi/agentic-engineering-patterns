# Context Injection Example

This example shows why "find the latest `HumanMessage`" is not enough.

Frameworks can add synthetic `HumanMessage` objects during summarization. Those messages are human-shaped, but not user-authored.

## Failure

```python
messages = [
    HumanMessage(
        content="Here is a summary of the conversation to date...",
        additional_kwargs={"lc_source": "summarization"},
    ),
    AIMessage(content="What should I do next?"),
]

for i in range(len(messages) - 1, -1, -1):
    if isinstance(messages[i], HumanMessage):
        messages[i].content = compose_with_context(messages[i].content, context)
        break
```

The code injects context into the summary. The model may now treat summary prose as if it were the user's latest words.

## Fix

```python
for i in range(len(messages) - 1, -1, -1):
    msg = messages[i]

    if not isinstance(msg, HumanMessage):
        continue

    if msg.additional_kwargs.get("lc_source") == "summarization":
        continue

    if not isinstance(msg.content, str):
        logger.warning("cannot inject context into non-string human message")
        break

    messages[i] = HumanMessage(
        content=compose_with_context(msg.content, context),
        additional_kwargs=msg.additional_kwargs,
    )
    break
```

The key rule is to stop treating type as authorship. `HumanMessage` means message role. It does not prove the browser user wrote it.
