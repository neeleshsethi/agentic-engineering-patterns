# State Factory Example

This example shows why new-turn graph input should be built by a factory.

`LastValue` channels keep the previous checkpoint value when the new invoke omits the key. That is useful for durable state and dangerous for per-turn context.

## Failure

```python
# Turn 1
graph_input = {
    "messages": [{"role": "user", "content": "sales in Germany"}],
    "raw_question": "sales in Germany",
    "user_context": "default country: Australia",
}

# Turn 2, written by a future endpoint
graph_input = {
    "messages": [{"role": "user", "content": "sales in France"}],
}
```

If `raw_question` and `user_context` are `LastValue` channels, turn 2 inherits turn 1's values.

## Fix

```python
def build_proposal_graph_input(
    question: str,
    user_context: str,
) -> dict:
    return {
        "messages": [{"role": "user", "content": question}],
        "raw_question": question,
        "user_context": user_context,
    }
```

Every code path that starts a new proposal calls this function. Empty context is still a value and should be written explicitly as `""`.

## Review Test

```python
first = build_proposal_graph_input("sales in Germany", "default: Australia")
second = build_proposal_graph_input("sales in France", "")

assert second["raw_question"] == "sales in France"
assert second["user_context"] == ""
```
