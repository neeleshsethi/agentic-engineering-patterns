# Distributed Locks

Distributed locks look straightforward until timing, retries, and partial failures are introduced. At that point, a lock can appear to work while still allowing duplicate execution or dead ownership.

## Failure Modes

- Lease expiry shorter than real work duration
- Unlock operations that do not verify ownership
- Retry loops that reacquire locks without reconciling prior state

## DynamoDB-Specific Concerns

With DynamoDB-backed coordination, conditional writes help, but they are not enough by themselves. You still need clear ownership tokens, lease renewal strategy, and recovery behavior for crashed workers.

## Guardrails

- Use unique ownership identifiers
- Verify ownership on release
- Size leases from observed worst-case work, not optimistic averages
- Instrument lock acquisition, renewal, and expiry paths
