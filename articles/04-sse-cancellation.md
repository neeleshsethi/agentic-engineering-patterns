# SSE Cancellation

Server-Sent Events are a practical way to stream agent progress, but cancellation semantics are easy to get wrong. When a client disconnects, the stream may stop while the expensive background work continues.

## Hidden Failure

From the user's perspective, the request is gone. From the server's perspective, detached tasks may still be consuming tokens, holding locks, or writing partial state.

## Risks

- Orphaned tasks continue after disconnect
- Cleanup handlers do not propagate cancellation to child coroutines
- Shared resources remain locked longer than intended

## Guardrails

- Treat disconnect as a first-class cancellation path
- Propagate cancellation through every spawned task boundary
- Test with forced disconnects, not only happy-path streams
