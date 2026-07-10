# Production Checklist

Use this checklist before shipping an agentic workflow:

- Verify prompt/context assembly is deterministic
- Define typed state channels and merge behavior
- Test retries, resumes, and partial failures
- Simulate client disconnects during SSE streaming
- Verify background task cancellation and cleanup
- Instrument persistence and lock contention paths
- Validate lock ownership before release
- Track semantic correctness, not just uptime and latency
