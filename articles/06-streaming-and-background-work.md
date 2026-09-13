# Step 5 · Streaming and Background Work

The agent can now hold state, inject context, pause for approval, and name the pieces of a run. The next step is user experience: the browser should see progress while the system works.

That does not mean the browser owns the work. The first rule of streaming production agents is this: **required work must outlive the HTTP connection showing its progress.** Here is what breaks when it cannot.

## The problem: the user closes the tab, and the save never happens

An agent finishes a run. Your code streams a final `end` event to the browser, then saves the turn to the database. It works every time you test it — because you sit and watch the stream finish.

In production, the browser is not so patient. The moment it receives the `end` event, it closes the connection. That disconnect cancels your server code *before the save runs*. The user got their answer, the request returned `200 OK`, and the turn was never written to history. Nothing threw. Nothing logged. This is a [silent failure](00-glossary.md#transport-success-vs-semantic-success).

> **[SSE (Server-Sent Events)](00-glossary.md#sse-server-sent-events)** — a long-lived HTTP response where the server sends small text frames over time, instead of one final body. It is how an agent streams progress to a browser.

## What actually happens on disconnect

[SSE](00-glossary.md#sse-server-sent-events) is a generator on the server: it `yield`s frames, and between yields it `await`s. When the browser closes the connection, the server framework (Starlette, FastAPI) cancels that generator at its **next `await`**.

```mermaid
sequenceDiagram
    participant B as Browser
    participant S as Server generator
    participant D as Database
    S->>B: yield "event: token"
    S->>B: yield "event: end"
    B--xS: closes connection on 'end'
    Note over S,D: next await is now cancelled
    S-xD: await save_to_database()  ✗ never runs
```

So any `await` placed *after* the terminal frame is a race against the client hanging up — and the client usually wins.

```python
# BEFORE — the save is cancelled before it starts
yield end_event
await save_to_database(...)   # the browser already left; this await never resolves
```

## The exception you will not catch

There is a second trap. When the framework cancels the generator, Python raises `asyncio.CancelledError`. In modern Python that inherits from `BaseException`, **not** `Exception`. So the usual safety net misses it entirely:

```python
try:
    await save_to_database(...)
except Exception:      # does NOT catch CancelledError
    logger.error("save failed")
```

The save is cancelled, the `except` never fires, and you have no log line telling you it happened.

## The fix: own the work before the terminal frame

The rule is simple: **required work must be started before the terminal frame is yielded.**

```text
BROKEN                              FIXED
──────                              ─────

  server          browser             server          browser
    │                │                  │                │
    │ yield "end" ──▶│                  │ create_task(save) ← background task
    │                │ closes conn      │ yield "end" ──▶│
    │ await save()   │                  │                │ closes conn
    │    ↑ CANCELLED │                  │ (task runs on) │
    │ no log, turn   │                  │ save() ✓       │
    │ missing        │                  │                │
```

Start the required work as a detached background task, and hold a strong reference so it is not garbage-collected mid-flight.

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

This does not make the save infallible. It changes the failure mode from *"a client disconnect cancels the save before it starts"* to *"a background task owns its own completion and its own logging."* The task now runs to completion regardless of whether the browser is still listening.

## Test it with a forced disconnect

The happy-path test never catches this bug — you have to hang up early.

```text
1. Open the stream.
2. Read until the first terminal event.
3. Close the client connection immediately.
4. Assert the database save still completes.
5. Assert lock release still happens, or is safely skipped by its token check.
```

## Guardrails

- Treat a client disconnect as a first-class cancellation path, not an edge case
- Never place a required write after a terminal `yield`; start it before, as an owned task
- Remember `CancelledError` is a `BaseException` — a bare `except Exception` will not see it
- Propagate cancellation through every task boundary you spawn
- Test with forced early disconnects, not only happy-path streams

---

!!! check "You should now understand"
    - Why SSE is a progress channel, not the source of truth for the run
    - Why an `await` after a terminal `yield` can be cancelled by browser disconnect
    - Why `CancelledError` escapes `except Exception`
    - Why required persistence should be owned before the terminal frame is yielded

??? question "Try this"
    **Your stream yields an `end` event and then awaits `save_turn()`. The frontend closes the EventSource immediately after receiving `end`. What is the silent failure?**

    ??? success "Answer"
        The server generator is cancelled at its next `await`, so `save_turn()` may never start. The user saw the answer and the stream ended cleanly, but history is missing. Start the required save as an owned background task before yielding the terminal frame, and test with a forced disconnect.

*Next: [Step 6 · Distributed Locks](07-distributed-locks.md) — streaming made progress visible; locking makes sure only one worker owns the run.*
