# SSE Cancellation

Server-Sent Events are a practical way to stream agent progress, but cancellation semantics are easy to get wrong. When a client disconnects, the stream may stop while the expensive background work continues.

## What SSE Is

SSE is a long-lived HTTP response where the server sends text frames over time.

The browser can close the connection as soon as it receives the final event. In Starlette or FastAPI, that disconnect cancels the response generator at its next `await`.

```text
server generator
  yield "event: token"
  yield "event: end"
  await save_to_database()  <- may never run after client closes
```

The HTTP stream succeeded from the user's perspective. The server-side persistence step may still be skipped.

## Hidden Failure

From the user's perspective, the request is gone. From the server's perspective, detached tasks may still be consuming tokens, holding locks, or writing partial state.

The opposite can also happen: work that you expected to run after the final event is cancelled before it starts.

`asyncio.CancelledError` inherits from `BaseException` in modern Python, not ordinary `Exception`. A broad `except Exception` block will not catch it.

## Risks

- Orphaned tasks continue after disconnect
- Cleanup handlers do not propagate cancellation to child coroutines
- Shared resources remain locked longer than intended
- Post-stream writes are skipped after the client receives `end`
- Half-finished writes leave records that no reader can interpret

## Safe Terminal-Event Pattern

Start required persistence before yielding the terminal event. Keep a strong reference to the task so it is not garbage collected.

```python
ACTIVE_BACKGROUND_TASKS: set[asyncio.Task] = set()

async def stream():
    async for event in run_graph():
        if event.type == "end":
            task = asyncio.create_task(save_turn(event.turn))
            ACTIVE_BACKGROUND_TASKS.add(task)
            task.add_done_callback(ACTIVE_BACKGROUND_TASKS.discard)

        yield encode_sse(event)
```

This does not make the save infallible. It changes the failure mode from "client disconnect cancels the save before it starts" to "background task owns its own completion and logging."

## Test Shape

```text
1. Open the stream.
2. Read until the first terminal event.
3. Close the client connection immediately.
4. Assert the database save still completes.
5. Assert lock release still happens or is safely skipped by token check.
```

## Guardrails

- Treat disconnect as a first-class cancellation path
- Propagate cancellation through every spawned task boundary
- Test with forced disconnects, not only happy-path streams
- Do not place required writes after a terminal `yield`
