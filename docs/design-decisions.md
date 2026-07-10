# Design Decisions

## Why Articles And Examples Live Together

The written analysis is more useful when every pattern can be reduced to a minimal, runnable example. Keeping both in one repository makes the claims easier to verify.

## Why Focus On Silent Failures

Crash-only thinking is insufficient for agents. Production teams are more often harmed by plausible wrong answers than by obvious stack traces.

## Why LangGraph

LangGraph introduces structure around orchestration and state transitions, which makes it a useful foundation for explaining where correctness can drift.
