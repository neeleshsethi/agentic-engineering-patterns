# Example Tests

These are the test shapes an intern should write before changing production agent code.

The examples in this repository are mostly markdown snippets, so this file describes the assertions rather than importing a runnable package.

## Context Injection

```python
def test_context_skips_summarization_message():
    messages = [
        HumanMessage(
            content="summary",
            additional_kwargs={"lc_source": "summarization"},
        ),
        HumanMessage(content="real user question"),
    ]

    result = inject_context(messages, "default country: Australia")

    assert "default country" not in result[0].content
    assert "default country" in result[1].content
```

This test proves that role does not equal authorship.

## State Factory

```python
def test_new_turn_writes_empty_context_explicitly():
    result = build_proposal_graph_input("sales in France", "")

    assert result["raw_question"] == "sales in France"
    assert result["user_context"] == ""
```

This test protects against stale `LastValue` carry-over.

## DynamoDB Claim

```python
def test_release_requires_same_claim_token(fake_table):
    acquire_claim(fake_table, "thread-1", token="old")
    expire_claim(fake_table, "thread-1")
    acquire_claim(fake_table, "thread-1", token="new")

    with pytest.raises(ConditionalCheckFailedException):
        release_claim(fake_table, "thread-1", token="old")
```

This test proves that an expired worker cannot delete a newer worker's lock.

## SSE Persistence

```python
async def test_save_starts_before_terminal_event(client, store):
    stream = client.stream("/chat/stream")

    await stream.read_until_event("end")
    await stream.close()

    await wait_for_background_tasks()

    assert store.saved_turn_count == 1
```

This test proves that required persistence is not placed after a terminal stream yield.
