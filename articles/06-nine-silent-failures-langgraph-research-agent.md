# 9 Silent Failures We Caught Before Shipping Our LangGraph Research Agent

How code review caught bugs that produced `200 OK` and wrong answers.

We spent the past several months building an agentic research system on top of LangGraph. The agent takes a business question, proposes a multi-step research plan, pauses for human approval, then executes the plan and writes a report. The architecture looks straightforward on paper: LangGraph for orchestration, middleware for context injection, DynamoDB for persistence, and SSE for streaming.

Code review before a recent release surfaced nine bugs. None crashed the system. None surfaced in logs. None would have tripped an alert. They all shared the same failure mode: the code ran to completion, returned `200 OK`, and produced a wrong result, either silently wrong data, silently missing data, or subtly wrong LLM outputs that no automated test would catch.

Here is each one, but first the architecture that makes the bugs interesting: a single user interaction spans multiple HTTP requests, and DynamoDB is doing double duty as both the checkpoint store and the distributed lock.

## The architecture: one user turn, three HTTP requests

Most backend systems map one user action to one request. Deep mode does not. A single research turn spans at minimum three separate HTTP requests over potentially 30 or more minutes:

```text
POST /chat/stream?mode=deep        <- proposal: fresh question, starts the graph
  -> streams plan as SSE
  -> closes at "interrupt" frame (graph pauses, no "end" sent)

POST /deep/{thread_id}/plan/refine <- optional: user wants changes
  -> Command(resume={type: refine, feedback: "..."})
  -> streams updated plan, closes at "interrupt" again

POST /deep/{thread_id}/approve     <- user locks the plan
  -> Command(resume={type: approve})
  -> graph resumes execution, streams to "end"
```

These are independent HTTP requests, potentially from different browser tabs, different API replicas, and minutes apart. The graph does not know they are related. LangGraph's checkpointer does.

### How the checkpoint ties them together

Every LangGraph node that runs writes its output to the checkpoint. When `submit_plan` calls `interrupt()`, the graph pauses and LangGraph writes the interrupt payload, including the plan, the prompt, and gate metadata, to the checkpoint's pending writes. The HTTP request finishes, the SSE stream closes, but the graph state is frozen in DynamoDB, waiting.

```python
# gate.py - submit_plan tool
decision, feedback = _decision_of(
    interrupt({
        "kind": "plan_approval",
        "thread_id": thread_id,
        "owner_ntid": owner_ntid,
        "plan": plan.model_dump(),
        "prompt": prompt,
    })
)
# Everything above interrupt() re-runs on every resume.
# Everything below runs only after the human decision arrives.
```

On the refine or approve request, `read_pending_gate(thread_id)` reads the interrupt payload directly from the checkpointer, with no graph compile required, to validate preconditions. Then the endpoint calls `graph.astream_events(Command(resume=decision), config)` on the same `thread_id`. LangGraph reads the checkpoint, finds the pending interrupt, delivers the `Command` as the resume value, and the graph continues from exactly where it stopped.

```python
# deep.py router - approve endpoint
gate = read_pending_gate(thread_id)
if gate is None:
    raise HTTPException(404)
if gate["plan"]["plan_id"] != payload.plan_id:
    raise HTTPException(409, "plan_id_mismatch")

async for chunk in stream_deep_typed(
    graph_input=Command(resume={"type": "approve"}),
    thread_id=thread_id,
    ...,
):
    yield chunk
```

### Why the checkpoint and the resume claim live in the same DynamoDB table

LangGraph does not support concurrent runs on a single thread. Two approve clicks racing, or two browser tabs both hitting approve, would execute the research plan twice and leave the final checkpoint to whichever process finished last. To prevent this, we write a claim item to DynamoDB before every resume:

```text
DynamoDB table: deep-checkpoints
|- Checkpoint items  (LangGraph's own PK/SK scheme per thread + step)
\- Claim items       PK = "CLAIM#{thread_id}", SK = "RESUME"
                     Fields: claim_token, expires_at, ttl
```

Both live in the same table because the claim needs the same durability and region as the checkpoint. If the checkpoint is in DynamoDB, an in-process claim would be invisible to other API replicas. The conditional write is atomic at the DynamoDB level:

```python
_dynamodb_client().put_item(
    TableName=deep_checkpoints_table_name(),
    Item={
        "PK": f"CLAIM#{thread_id}",
        "SK": "RESUME",
        "claim_token": token,
        "expires_at": now + 1800,
        "ttl": now + 3600,
    },
    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
    ExpressionAttributeValues={":now": str(now)},
)
```

Release is token-guarded: the caller holds a UUID token from acquire, and the delete only succeeds if the stored token still matches. A stream that outlives its 30-minute lease can be re-claimed by a new request. When the original stream eventually finishes and tries to release, it gets `ConditionalCheckFailedException`, logs it at info, and leaves the new holder's lease intact.

### One Langfuse trace across all three requests

Each `graph.astream_events()` call opens a new LangGraph run, which by default opens a new Langfuse trace. Without intervention, a deep turn that goes proposal -> refine -> approve produces three sibling traces with no structural link. We seed the trace ID deterministically from the thread ID:

```python
trace_id = langfuse_client.create_trace_id(seed=f"deep:{thread_id}")
handler = CallbackHandler(trace_context={"trace_id": trace_id})
handler.last_trace_id = trace_id
```

Same `thread_id` means the same seed, which means the same trace ID on every request. All three phases land in one Langfuse trace, one Agent Graph, properly sequenced.

## The design: state channels and middleware

### What a LangGraph channel is

LangGraph state is a typed dict where each key has explicitly declared update semantics. The default is `LastValue`: each write replaces the previous value, and a key not written on a given invoke inherits whatever the checkpoint held. There are also reducer channels. `messages` uses one that appends new messages rather than replacing the list, reducing checkpoint growth from quadratic to linear.

The channel model matters because "I did not write this key" has a defined, non-obvious meaning: the previous value survives. In a multi-phase pipeline, resume paths only write the channels relevant to their phase. The rest carry over from checkpoint. That is correct behavior for most channels and a footgun for channels that need to be explicitly reset on a new turn.

Persisted channels in our agent included:

```text
messages          DeltaChannel    Full transcript: HumanMessage, AIMessage, ToolMessage
todos             LastValue       [{content, status}] managed by write_todos tool
plan              LastValue       Serialized Plan dict: plan_id, status, steps, modification_count
entity_groups     LastValue       [{country, brand, period}] written by submit_plan
raw_question      LastValue       User's verbatim question text
user_context      LastValue       Rendered context block (defaults + memory)
```

### Why we split state across two TypedDicts in two files

LangGraph compiles the graph state schema from `state_schema=` plus any `state_schema` fields declared by middleware. We used that to co-locate each channel declaration with its sole writer:

```python
# gate.py
class DeepState(DeepAgentState):
    plan: NotRequired[dict[str, Any]]
    entity_groups: NotRequired[list[dict[str, Any]]]

# proposal_context.py
class _UserContextState(AgentState):
    raw_question: NotRequired[str]
    user_context: NotRequired[str]

# graph.py
graph = create_deep_agent(
    ...,
    state_schema=DeepState,
    middleware=[UserContextMiddleware(), ...],
)
```

At compile time, LangGraph merges `DeepState` and `_UserContextState` into one checkpointed schema. At runtime they are indistinguishable, but in the source each channel declaration sits beside the code that writes it. Dropping `UserContextMiddleware` from the middleware list still compiles and runs. The channels just disappear from the checkpoint quietly, which is exactly the sort of structural gap Bug 5 exploited.

### Why middleware for context injection

We needed to prepend user-specific context, including defaults and prior conversation entities, to the user's question before it hit the model. Four approaches were on the table:

- System prompt injection: rejected because the orchestrator prompt is request-invariant and cached.
- Checkpointed message injection: rejected because the block would accumulate in history across turns.
- Tool call: rejected because it adds an extra model/tool round trip and can be skipped.
- Middleware: inject just before the LLM call, after the checkpoint is read.

```python
class UserContextMiddleware(AgentMiddleware):
    state_schema = _UserContextState

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._inject(request))

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._inject(request))

    def _inject(self, request: ModelRequest) -> ModelRequest:
        context = str((request.state or {}).get("user_context") or "")
        if not context:
            return request
        ...
        return request.override(messages=messages)
```

`request.override(messages=...)` returns a new `ModelRequest`. It does not mutate state. The injected copy vanishes after the model call, the checkpoint stays clean, and the middleware reads its own declared channels directly from `request.state`.

The tradeoff is that the injection is invisible in the checkpointed transcript and every assumption the injection code makes about message structure becomes a correctness risk. Bugs 2 and 3 are both examples of those assumptions failing.

## 1. Injected context kept overriding a decision the user already made

Before every LLM call in the planning phase, we injected a context block onto the user's message with their default entities and prior conversation history so phrases like "give me my sales" resolve correctly.

The bug was that we injected on every model call, including execution calls after the user had already approved a plan with specific entities:

```text
[User context]
User defaults: AUSTRALIA / PAXLOVID.
Use these when the question says "my" or omits country/brand.
[/User context]

give me my sales
```

The correction to `GERMANY` lived in a tool message buried mid-conversation, while the Australia default stayed glued to the question on every call at the highest salience. The model re-resolved against Australia during execution and wrote a report about the wrong country.

The obvious fix was wrong. Gating injection on `plan["status"] == "locked"` would break multi-turn sessions because the previous question's locked plan still sits in the `plan` channel while a new question is entering the planning phase.

The actual fix was to write the rule into the prompt itself:

```python
"[User context - entities named explicitly in the question always win over these defaults, "
"and once a research plan is approved and locked, the locked plan's entities "
"win over these defaults]\n"
```

We also added one sentence in the orchestrator's execute step: "The [User context] block is for plan-time entity resolution only. Once the plan is locked, never re-resolve entities from it."

Lesson: when context should only apply at one phase of a multi-phase pipeline, encode that constraint in the context itself. A prompt rule beats a code gate when the code gate has to reason about shared state across turns.

## 2. A framework dependency swapped in a synthetic HumanMessage under our feet

The injection code scanned the message list in reverse to find the latest `HumanMessage` and attach the context block to it.

The bug was that `deepagents` unconditionally installs a summarization middleware that fires near the context window limit. When triggered, it rewrites the message list and inserts a synthetic `HumanMessage` containing the summary:

```python
messages = [
    HumanMessage(
        "Here is a summary of the conversation to date: ...",
        additional_kwargs={"lc_source": "summarization"},
    ),
    AIMessage("<most recent>"),
]
```

The original user message may already be evicted. Our reverse scan found the synthetic summary as the latest human message and attached the context block to it. The delimiter told the model "everything after `[/User context]` is the user's own words," so we accidentally labeled summarizer prose as user-authored content.

The fix was to skip framework-tagged synthetic messages:

```python
if msg.additional_kwargs.get("lc_source") == "summarization":
    continue
```

Lesson: when your code finds "the latest item of type X in a list," enumerate every party that can insert an X. Framework middleware can insert objects that pass `isinstance(msg, HumanMessage)` without being genuine user input.

## 3. A search loop continued past its target and decorated the wrong message

The original reverse-scan condition was:

```python
if isinstance(msg, HumanMessage) and isinstance(msg.content, str):
    # inject here
```

LangChain messages can carry list-form content for multimodal inputs. If the latest human message has list content, the condition fails and the loop continues backward until it finds an earlier string message.

That means the loop can attach turn 2's context block to turn 1's question while turn 2 receives no context at all.

The fix was to separate "not the target" from "the target exists but cannot be processed":

```python
for i in range(len(messages) - 1, -1, -1):
    msg = messages[i]
    if not isinstance(msg, HumanMessage):
        continue
    if msg.additional_kwargs.get("lc_source") == "summarization":
        continue
    if not isinstance(msg.content, str):
        logger.warning(
            "user_context not injected: latest human message has %s content",
            type(msg.content).__name__,
        )
        return request
    # inject and return
```

Lesson: in a backward search, failing the capability check should not silently retarget the search to an older item. When you find the right item but cannot act on it, stop and log.

## 4. User-influenced text inside a trusted block could forge its own closing delimiter

The context block uses a closing delimiter that tells the model where injected context ends and the user's own words begin. Part of that block comes from session summaries derived from prior conversation text that we do not fully control.

If session memory ever contained the literal string `[/User context]`, the composed message would have two closing markers and user-influenced text could escape the trusted block.

The fix was one line before embedding user-influenced content:

```python
def compose_with_context(content: str, context: str) -> str:
    if not context:
        return content
    context = _PARAGRAPH_BREAK.sub("\n", context)
    context = context.replace("[/User context]", "[/user context]")
    return f"{context}\n[/User context]\n\n{content}"
```

Lesson: this is prompt injection. Any time you embed external text inside a structured format that uses delimiters, escape your delimiter before embedding.

## 5. A three-file invariant had no structural owner and no error on violation

Our agent state uses `LastValue` channels, so any key not written on a given invoke inherits the previous checkpoint value. Starting a new conversation turn required writing three channels together: the message, the raw question, and the context block. Even when the context block is empty, it still must be written explicitly to reset the previous turn's value.

The bug was that this invariant was a hand-assembled dict in one file, the channels were declared in another, and the code that depended on them lived in a third. Nothing enforced the rule structurally.

That meant a future caller could innocently write:

```python
graph_input = {
    "messages": [{"role": "user", "content": question}],
}
```

and the previous turn's `raw_question` and `user_context` would silently persist into the new turn.

The fix was a factory function co-located with the channel declarations:

```python
def build_proposal_graph_input(
    question: str,
    user_info: dict | None,
    memory: list | None,
) -> dict:
    return {
        "messages": [{"role": "user", "content": question}],
        "raw_question": question,
        "user_context": build_proposal_user_context(user_info, memory),
    }
```

Lesson: there is a hierarchy of invariant enforcement: type signature, factory function, documented convention, and tribal knowledge. Critical invariants should sit as high in that hierarchy as practical.

## 6. The post-stream database write got cancelled when the client hung up

A deep run finishes, we stream an `end` event, then save the turn to the database.

The bug was that the frontend correctly closes the SSE connection as soon as it receives the `end` event. In Python and Starlette, a client disconnect raises `CancelledError` at the generator's next `await`.

```python
# BEFORE
yield end_event
await save_to_database(...)
```

`CancelledError` is a `BaseException` in Python 3.11+, not an `Exception`, so ordinary `except Exception` blocks never saw it.

Two silent failure modes followed:

- Fast disconnect: the save never starts, so the turn is missing from history entirely.
- Mid-save disconnect: one row is written, the paired row is cancelled, leaving an orphaned record.

The fix was to spawn a detached background task before yielding the terminal event:

```python
if event.type == "end":
    task = asyncio.create_task(save_to_database(...))
    _ACTIVE_TASKS.add(task)
    task.add_done_callback(_ACTIVE_TASKS.discard)
yield end_event
```

Lesson: any `await` inside an SSE generator that runs after a terminal event is a race against client disconnect. Create the task before yielding the terminal event and hold a strong reference to it.

## 7. Writer and reader used different keys for the same record

Chat history is stored in DynamoDB under `chat_id`. The approval payload marked `chat_id` as optional.

The writer used a fallback:

```python
key = payload.chat_id or payload.session_id
```

but every reader used only `chat_id`:

```python
history = get_history(payload.chat_id or "")
```

When the frontend omitted `chat_id`, the write succeeded under `session_id`. The next reader looked up `""` and found nothing. Deep mode looked stateless for that traffic slice with no error anywhere.

Quick mode had already solved this correctly by skipping the write entirely when `chat_id` is absent:

```python
if not chat_id:
    logger.info("skipping persistence: no chat_id")
    return
```

Lesson: if a field is optional, some callers will omit it. Writer and reader must handle that absence identically. Otherwise you create rows that no reader will ever find.

## 8. The lock release did not prove it still owned the lock

To prevent double execution on rapid approve clicks, we write a resume claim to DynamoDB when a run starts. Claims expire after 30 minutes. When the run finishes, we delete the claim.

The bug was that the delete was unconditional:

```python
table.delete_item(Key={"thread_id": thread_id})
```

If a long research run outlived the lease, a second request could acquire a new claim. When the first run eventually finished, its unconditional delete would remove the second run's claim and reopen the race window.

The fix was to store a per-claim UUID on acquire and require that token on release:

```python
token = str(uuid.uuid4())
table.put_item(Item={
    "thread_id": thread_id,
    "claim_token": token,
    "expires_at": now + 1800,
})

try:
    table.delete_item(
        Key={"thread_id": thread_id},
        ConditionExpression="claim_token = :token",
        ExpressionAttributeValues={":token": token},
    )
except ConditionalCheckFailedException:
    logger.info("claim release skipped: thread re-claimed after lease expiry")
```

Lesson: "release a lock" and "release your lock" are different operations. Always prove ownership on release.

## 9. Embedding stateful context in message records made an explicit reset invisible

After each completed turn, we saved session state, including resolved entities such as country, brand, and time period, as a field on the assistant message record in DynamoDB. The next turn scanned message history in reverse to find the most recent record with that field and injected it as context.

The bug was a three-step collapse of semantics.

First, the state field was coerced before storage:

```python
entity_groups = values.get("entity_groups") or None
```

An explicit reset to `[]` became `None`.

Second, the persistence layer skipped `None` fields:

```python
if md.entity_groups:
    item["entity_groups"] = md.entity_groups
```

The reset turn was written with no `entity_groups` attribute at all.

Third, the read path scanned backward for the most recent non-empty value:

```python
for msg in reversed(memory):
    groups = msg.get("entity_groups", [])
    if groups:
        return groups
```

So the scan skipped the reset turn and landed on an older populated turn, rehydrating stale state.

The fix was to move session state out of message records and into a purpose-built store with direct overwrite semantics:

```python
state_value = values.get("session_state")
if state_value is not None:
    store.put(session_id, state_value)

state_value = store.get(session_id)
```

`None` means the state channel was never set and the previous row remains untouched. `[]` means explicit reset and should overwrite the previous value.

Lesson: embedding session state inside event or message records creates two structural problems. It forces readers to scan history instead of doing direct lookup, and it makes empty-vs-absent semantics easy to destroy through convenience coercions like `x or None`.

## The pattern

All nine bugs shared one property: the code ran to completion and the operation succeeded in the narrow transport sense because nothing threw. What failed was the semantic contract.

| Bug | What "succeeded" | What actually happened |
| --- | --- | --- |
| Context overrides approved plan | `200 OK`, report generated | Report about wrong entities |
| Synthetic message targeted | `200 OK`, context injected | Context labeled summarizer prose as user words |
| Loop retargeted wrong message | `200 OK`, context injected | Context attached to a previous turn's message |
| Delimiter forged in block | `200 OK`, plan proposed | Smuggled text treated as user content |
| Convention-only channel write | `200 OK`, turn started | Previous context bled into a new turn |
| Save cancelled on disconnect | `200 OK`, end event sent | Database write never ran |
| Wrong key on write | `200 OK`, row written | Row written under a key no reader uses |
| Unconditional lock release | `200 OK`, claim deleted | Another request's claim was deleted |
| `[] or None` reset invisible | `200 OK`, turn answered | Follow-up planned against prior topic entities |

The review habits that caught these before they shipped were consistent:

- Test the content, not just the return code.
- Read library source, not just the docs.
- For every abstract pattern, such as injection, locking, or persistence, enumerate failure modes phase by phase and ask whether a transport-level success code actually means the semantic work completed.

Built on LangGraph, Python 3.11, DynamoDB, and Starlette SSE. The stack is incidental. Context injection targeting, lock ownership, async cancellation, and writer-reader key mismatch are universal failure classes.

Thanks and regards,  
Neelesh Sethi
