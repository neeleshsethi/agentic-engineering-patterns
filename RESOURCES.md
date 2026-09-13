# Production Deep Agent Engineering Resources

## Knowledge

- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)
  Primary framework reference for checkpoints, interrupts, state, streaming, and graph execution. Use for public API behavior.
- [AWS SQS FIFO queue documentation](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues.html)
  Primary source for FIFO ordering, message groups, visibility timeout, and retry behavior.
- [AWS DynamoDB conditional writes documentation](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
  Primary source for conditional writes used by locks, claims, and ownership fencing.
- [MDN Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
  Browser-facing reference for EventSource and `Last-Event-ID` reconnection behavior.
- `data/extracted/1392026-llamaparse/key-findings.md`
  Local OCR distillation from the screenshot batch. Use for production-specific architecture facts.
- `data/extracted/1392026-llamaparse/combined.md`
  Full local OCR extraction. Use when a lesson needs exact details beyond the distillation.

## Wisdom (Communities)

- Project source notes under `articles/source-notes/`
  Local design history and scar tissue. Use for advanced explanations after the beginner lesson has landed.

## Gaps

- Add official LangGraph links for the exact `put_writes` / pending resume mechanics if these lessons are later published with external citations.
