# SSE Streaming Example

This example shows why required writes should not happen after a terminal SSE event is yielded.

## Failure

```python
async def stream():
    async for event in graph_events():
        yield encode_sse(event)

        if event.type == "end":
            await save_turn_to_database(event.turn)
```

The browser may close the SSE connection immediately after receiving `end`. Starlette can then cancel the generator at the next `await`, before the save runs.

## Fix

```python
ACTIVE_TASKS: set[asyncio.Task] = set()

async def stream():
    async for event in graph_events():
        if event.type == "end":
            task = asyncio.create_task(save_turn_to_database(event.turn))
            ACTIVE_TASKS.add(task)
            task.add_done_callback(ACTIVE_TASKS.discard)

        yield encode_sse(event)
```

The background task must handle its own errors and logging. Keeping it in `ACTIVE_TASKS` ensures the task remains strongly referenced.

## Test

```text
1. Start the stream.
2. Read until the `end` event.
3. Close the client immediately.
4. Wait for the background task.
5. Assert the turn was saved.
```
