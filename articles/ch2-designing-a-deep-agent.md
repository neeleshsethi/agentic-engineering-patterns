---
summary: "You are asked to design a deep agent. The questions to ask, in order, and the principle that answers each one."
---

# Chapter 2 · Designing a deep agent

You have been asked to build an agent for a retail business. Commercial leaders will ask it multi-step sales questions, such as "compare BRAND_A sales in Germany and France, and tell me which channel drove the gap." It answers from four data sources, SOURCE_A through SOURCE_D, licensed per market. One requirement is fixed from the start. A human approves the research plan before anything expensive runs, because the plan spends real money on queries.

[Chapter 1](ch1-problem-and-plan.md) set out the problem. Runs take forty minutes, browsers disconnect, workers crash, and the worst failures return `200 OK` with the wrong answer. This chapter is the thinking you do before you draw a single box.

The method is to ask questions in the order a careful engineer would ask them. Each question has a tempting first answer. For each one you will see what that answer broke in this agent, the decision that replaced it, and the engineering principle behind the decision, with a small example from the real code. Each section ends with the Step where you build the answer. By the end, the twelve answers add up to the architecture in [Chapter 3](ch3-architecture-and-flow.md).

Twenty-four principles come up along the way. You do not need to memorize them. They are what you reach for when a question has no obvious answer, and each one gets a one-sentence definition where it first appears.

## What does "done" mean, and how will I know? { #what-done-means }

Start here, because every later answer gets checked against this one.

**The tempting answer.** Done means the run finished. The endpoint returned `200`, the stream closed, and a report arrived. You write tests that call the endpoint and assert a non-empty report.

**What goes wrong.** In this agent, a run with a clean `200` produced a confident report about Australia when the user asked about France. The evaluation side had the same problem one layer up. Between two evaluation snapshots, the non-pass rate fell by more than half, and it looked like a clean win. When the team paired the cases that appeared in both runs and split the verdicts apart, clarifying questions had dropped from about 20% to about 3%, and wrong-but-confident answers had risen from about 10% to about 15%. The agent had not become more certain. It had stopped saying when it was unsure.

**The decision.** Write down three layers of "done" before writing any code:

```text
Transport success:  the request finished, nothing threw
Semantic success:   the right context, state, plan, lock and persistence rules held
Task success:       the user got what they asked for, built from retrieved rows
```

Then build the checks that can tell the layers apart. That means tests that read back state across turns, and an evaluation with stable case ids and per-case verdicts that you compare as pairs.

**Explain the Number.** Before you trust or act on a number you measured, name what limits it and rule out everything else it could be measuring. The "non-pass rate halved" number was measuring two things at once. The case sets differed between snapshots, and the `clarify` verdict was counted as a failure. The fix keeps every verdict and compares only shared cases:

```python
def transitions(before: dict[str, str], after: dict[str, str]) -> Counter:
    shared = before.keys() & after.keys()
    return Counter((before[c], after[c]) for c in shared)

# The cell to watch: ('clarify', 'incorrect'), questions that became confident errors.
```

The same habit applies to performance numbers. When reasoning-token streaming started failing mid-run, the limiter had a name, DynamoDB's per-partition write ceiling of about 1,000 WCU per second. Persisting every replace-semantics delta wrote O(N²) bytes into one partition. Once you could name the limiter, the fix followed from it. The curator now coalesces live frames to at most one write per two seconds per step.

**Test Behavior, Not Implementation.** A test claims what the code's users can observe, so it should fail when that behavior breaks and keep passing when only the internals change. Step 1 asks for one test that catches a whole class of bug. A second turn passes empty context and asserts that nothing from the first turn survives. In sketch form:

```python
run_turn(thread, "sales in Germany", user_context="default country: Australia")
state = run_turn(thread, "sales in France", user_context="")
assert state["raw_question"] == "sales in France"   # state a caller can read back
assert state["user_context"] == ""
```

It reads checkpointed state, which any later request will see. It does not assert which helper built the input dict.

**Prove It Works.** Check the real artifact, not a proxy or a self-report. One production failure looked like a weak model ignoring an output-language instruction. Language detection worked. The instruction never reached the model. The habit that found it was opening the generation span in the trace and reading the exact prompt the model received, rather than the request span or the code that was supposed to build it. The regression test now asserts that the instruction is present at the model boundary. The same discipline applies to telemetry. Most deep-run traces reported zero cost, and that field became evidence only after someone checked it was populated.

**Where you build it.** The [evaluation bridge](bridge-evaluation.md) teaches the minimum evaluation loop. [Capstone 2](capstone-2-when-the-score-passes.md) applies it to this agent.

## Do I need a deep agent at all, and what is the smallest thing that works? { #do-you-need-a-deep-agent }

Now that you know what done means, ask how little machinery gets you there.

**The tempting answer.** The approval gate is the safe path, so every message goes through it. Every turn writes todos, submits a plan, pauses, and waits for approval.

**What goes wrong.** Many turns are not new questions. A user answers a source's ask-back ("in-store or online?"), adds a market ("also pull New Zealand"), or asks to re-sort a table. If those turns reopen the gate, the system mints a new `plan_id`. Evidence in this agent is filtered by `plan_id`, so the results already fetched drop out of the current cycle, and the human is asked to approve the same question twice. The opposite mistake costs as much. A one-pass agent that calls a tool and answers cannot survive a forty-minute job or a human pause.

**The decision.** Build two modes. Reactive mode is a one-pass router:

```text
decide -> call at most the needed data tool once -> answer
```

Deep mode is for runs where planning, approval, attribution and durable execution matter more than speed. Inside deep mode, the gate opens only for the first question on a thread or a genuinely new topic. One routing table decides this for every turn:

| Turn type | Plan workflow? |
|---|---:|
| First question on a thread | Yes |
| New-topic follow-up | Yes |
| Entity or metric gap with no safe default | No, ask one terminal clarification |
| Source elicitation reply | No, re-query only the asking step |
| Extension of the current ask | No, retrieve under the locked plan |
| Evidence reply or presentation change | No, answer from held evidence or rebuild once |

**Laziness Protocol.** Make the smallest change that solves the problem, and put each repeated choice behind one source of truth. "Does this turn need a plan?" is a choice every code path could make on its own. The routing table makes it once. Reactive mode is the same principle applied to the whole system. Most questions need no plan, so they get none.

**Subtract Before You Add.** Remove complexity first, then build on the simpler base, with no speculative guards beyond what the requirement needs. Deep mode has four kinds of clarification, and the obvious move is a `DeepClarificationMiddleware` to own them all. The agent has none. Each kind rides a seam that already exists:

```text
plan-gate clarification   -> submit_plan(clarification=...)
terminal clarification    -> the turn classifier, before any todos exist
source elicitation        -> the source client stamps is_elicitation
"may a report be written?" -> pending_elicitation() in the exit path
```

Each kind has a different lifetime, so one generic middleware would have had to tell them apart anyway.

**Where you build it.** [Step 8a](step-8a-reactive-mode-prompt.md) builds reactive mode. [Step 8c](step-8c-plan-lifecycle.md) builds the turn routing.

## What are the nouns, and what states can they be in? { #nouns-and-states }

You have decided a deep run exists. Before writing logic, name the things it is made of.

**The tempting answer.** One `run` record with one UUID, a few booleans (`approved`, `is_running`), and a state dict that each node reads and writes as it likes.

**What goes wrong.** Two bugs in this agent were shape bugs. First, LangGraph state keys default to `LastValue`, which keeps the old value when a turn does not write the key. A route that wrote only `messages` left last turn's `raw_question` in place, and a question about France got an answer about Germany. Second, a crashed worker's replacement re-emitted progress events. The reader deduplicated on the sequence number `seq`, which takes new values on replay, so every section appeared twice.

**The decision.** List the nouns, and give each one its own identity and lifecycle:

```text
thread    thread_id                  the conversation, stable forever
turn      cycle                      the question number inside a thread
plan      plan_id = plan-{thread}-{cycle}   stable across replay and refinement
pause     interrupt_id               new for every interrupt
attempt   run_id                     new for every execution attempt
event     idem_key = {task_id}#{n}   stable; seq only orders
tenure    claim_token                new for every lock acquisition
fact      provenance_id              {source}:{plan_id}:{step_id}:{hash}
```

Give every state key an explicit merge rule. Give the plan a state machine, and let code own its transitions.

**Foundational Thinking.** Get the data shapes right before writing logic, trace every access pattern, and ask what concurrent actors share. The state schema is where this happens:

```python
class DeepState(TypedDict):
    raw_question: NotRequired[str]                               # LastValue: replace
    messages: Annotated[list[AnyMessage], add_messages]          # reducer: append
    source_results: Annotated[list[SourceResult], operator.add]  # reducer: append
    step_status: Annotated[dict[str, str], operator.or_]         # reducer: merge
```

`step_status` is a merge because parallel branches update different plan steps in the same superstep. A `LastValue` there would let one branch erase another's status. That is the concurrency question asked at the schema, before any branch exists.

**Model the Domain.** Encode the domain in a structure, such as a state machine, a typed model, or a lookup table, instead of scattered conditionals. The plan lifecycle is a state machine in code:

```text
DRAFT --refine--> DRAFT (modification_count + 1)
DRAFT --approve--> LOCKED --> EXECUTING --> COMPLETE
```

The model writes plan content only in DRAFT. The `locked` transition happens in code, on approve, through `model_copy`. No prompt sentence says "only proceed if the plan is approved", because no branch anywhere needs to check.

**Type System Discipline.** Make illegal states unrepresentable, and parse external data into typed models at the boundary. Model output is external data. The agent writes plain todos, and `project_plan` parses them into a typed `Plan` every time one is needed. A malformed routing tag fails soft. The raw text stays visible in the step title and the routing field is left unset. The model can write inconsistent todos, but it cannot produce an invalid `Plan`. The cost of skipping this showed up in Capstone 1's Bug 9. An explicit reset `[]` passed through `values.get("entity_groups") or None`, became `None`, was skipped on write, and the next read rehydrated an older turn's entities. Empty and absent were different states, and the coercion merged them.

Not in this agent yet: branded identifiers. Every id above is a plain `str`, so nothing stops a caller from passing a `run_id` where a `thread_id` belongs. A sketch of the fix:

```python
ThreadId = NewType("ThreadId", str)
RunId = NewType("RunId", str)

def enqueue_deep_run(thread_id: ThreadId, run_id: RunId) -> None: ...
```

With these types, the type checker rejects a swapped call before it runs.

**Where you build it.** [Step 1](step-1-state-and-checkpoints.md) builds the state channels. [Step 4](step-4-identifiers.md) builds the identifiers. [Step 3](step-3-planning-and-human-approval.md) builds the plan state machine.

## Where are the boundaries, and who may write what? { #boundaries }

You have the nouns. Now decide where outside data enters, and which rules the model may break.

**The tempting answer.** Put the user's defaults in the system prompt. Write the rules there too: "Do not use the spawn_subagent tool." "Only retrieve after the plan is approved."

**What goes wrong.** Three things broke. The middleware injected "default country: Australia" above a message that said "Actually, give me sales for France", and the injected default won. Some text inside the trusted context block came from conversation summaries, and if it ever contained the literal `[/User context]`, it would close the block early, so whatever followed would read as the user's own instruction. And the framework's built-in sub-agent tool stayed visible to the model, which sometimes called it anyway. A sub-agent runs its retrievals outside the approval gate.

**The decision.** Inject request-only context in middleware just before each model call. Label it, escape its delimiter, and keep it out of the checkpoint. Split every rule with one test. If code can enforce the rule deterministically, code enforces it and the prompt spends no tokens on it. Tools, the gate, plan status, owner checks and concurrency go to code. The prompt shapes judgment: reasoning order, the priority of explicit entities over defaults, and grounding.

**Boundary Discipline.** Validate and handle errors at system boundaries, trust typed data inside, and keep business logic in pure functions. Prompt assembly is a pure function, structured state in and a string out, with the guard at the exact point where untrusted text crosses in:

```python
def compose_with_context(content: str, context: str) -> str:
    safe_context = context.replace("[/User context]", "[/user context]")
    return f"[User context]\n{safe_context}\n[/User context]\n\n{content}"
```

Because it is pure, you can test it without a model. The approve endpoint follows the same rule. It checks `owner_id`, `plan_id` and `interrupt_id` once, at the HTTP boundary, and the code after that trusts the result.

**Encode Lessons in Structure.** When you find yourself writing the same instruction a second time, encode it as a check or as code. If the fix is structural, use only the structural fix. The tool rule became a middleware that removes tools from the request:

```python
class AllowedToolsMiddleware(AgentMiddleware):
    def _filter(self, request):
        tools = [t for t in request.tools if t.name in self._allowed]
        return request.override(tools=tools)   # the model never sees the rest
```

The prompt does not also say "don't call spawn_subagent." That sentence would be the symptom of a fix that is already in place. The same principle shows up outside the prompt. After a per-job `asyncio.run()` broke a cached HTTP client, a startup guard now turns any reintroduced per-job `asyncio.run()` into a named error.

Some lessons cannot be encoded. For those, the principle says to make the instruction more prominent and show the failure mode. Grounding is the clearest case. No code can tell whether a sentence came from a tool result or from training data. So the prompt line names every verb the model used to negotiate around it, "Never state, estimate, OR approximate", and it tells the model to mark a failed retrieval as completed, because a "failed" status the status map did not recognize had sent the executor into an endless retry.

**Where you build it.** [Step 2](step-2-context-injection.md) builds the injection boundary. [Step 8](step-8-orchestrator-prompt.md) applies the prompt-or-structure test to the whole orchestrator.

## Where does the human come in, and does anything wait on them? { #where-the-human-comes-in }

The requirement says a human approves the plan. Work out exactly where, and what happens while they think.

**The tempting answer.** Hold the request open while the human reads the plan, then resume when the approval callback arrives. For refinement, loop inside `submit_plan` and call an LLM to apply the feedback. When the agent is unsure about anything, ask the user.

**What goes wrong.** Approval can take minutes or days, and no load balancer holds a connection that long. The refine loop needs a second, out-of-band model that does not share the agent's prompt, tools or conversation, so two models end up writing one plan. The browser causes trouble even when nobody is approving anything. It closes the stream the moment it sees the `end` frame, which cancels any `await` after that frame, so a turn's save never ran and nothing logged it. And asking too often has its own cost, which you saw in the evaluation tradeoff above.

**The decision.** `interrupt()` writes the pause into the checkpoint, and the request ends. Between the plan request and the approve request, nothing is running. Approval is a second request that validates the pause, stages the decision, queues the work, and returns `202 queued`. Refine is the agent's own loop. The tool returns the feedback as a `ToolMessage`, and the agent edits its todos and calls `submit_plan` again. Progress streams to the browser over SSE. Required writes start before the terminal frame, and the research stream ends when run-state says the run is finished, not when a sentinel event arrives.

**Never Block on the Human.** The human supervises asynchronously. Proceed on reversible work, present the result, and let the human course-correct, saving confirmation for actions that cannot be undone. This agent applies it at two levels. At the system level, it stops only where the requirement demands, before research that spends real money, and even there no process waits:

```text
POST /chat/stream?mode=deep   -> plan streamed, interrupt() persisted, request ends
   ... minutes or days pass; no process is running ...
POST /deep/{thread_id}/approve/async -> validate, stage decision, queue, 202 queued
```

At the agent level, it proceeds under a stated assumption instead of opening a separate question round:

```text
Clarification:
  "SOURCE_A and SOURCE_B use different market definitions. I will align on
   retail market unless you refine the plan."
```

The reactive path has a matching rule. `ClarificationGuardMiddleware` sees that the previous turn already asked a question, and injects a default-and-declare instruction so the agent never asks twice in a row. The agent asks a terminal question only when a required entity or metric has no safe default.

**Experience First.** When implementation convenience conflicts with the user's experience, choose the experience. The user includes whoever maintains the code next. The plan card shows "query SOURCE_A for BRAND_A sales by market", not "do research", because a human who cannot evaluate a plan approves it blindly. When one source returns data and another asks back, the message names both facts:

```text
"I have the promotion lift figures. For regional sales, the source asks:
in-store or online?"
```

The stream ends on durable run-state, so after a worker crash the browser gets a terminal frame with a reason and never spins forever.

**Where you build it.** [Step 3](step-3-planning-and-human-approval.md) builds the gate and the `202` path. [Step 5](step-5-streaming-and-background-work.md) builds streaming that the work outlives. [Step 8b](step-8b-clarifications.md) builds the four clarification shapes.

## What happens when it crashes halfway? { #crash-halfway }

The approved run now executes on a background worker for tens of minutes. Assume the worker dies at the worst possible line.

**The tempting answer.** Catch the exception and retry the run from the start. Delete the queue message as soon as it arrives so nobody else picks it up. Deduplicate feed lines by their sequence number.

**What goes wrong.** SQS delivers at least once, and LangGraph re-runs an interrupted node from its beginning. A Slack notification placed before `interrupt()` fires once on the proposal and once more on every resume. A replayed node re-emits its feed lines with new `seq` values, so a `seq`-based reader shows them twice. A message deleted on receive is gone when the worker dies, and the approved run never happens.

**The decision.** Every step reads durable state and applies only the difference. The approve path writes in a fixed order. Workers delete the queue message last. Every identity that has to be recognized after a crash is derived from position, not from a counter or the clock.

**Make Operations Idempotent.** Design each operation to converge on the same end state no matter how many times it runs or where the previous run stopped. Ask "what if this runs twice?" and "what if the last run crashed at every possible point?" The approve path answers both through its write order:

```text
checkpoint resume decision -> run-state queued -> queue message
```

A lost message can be re-sent. A lost approval cannot be rebuilt. The feed answers the same questions with two values that behave in opposite ways on replay:

```text
Attempt 1   seq 3  taskB#0  Querying ONLINE...
            seq 4  taskB#1  ONLINE 88 rows        <- crash before commit
Attempt 2   seq 5  taskB#0  Querying ONLINE...    <- new seq, same idem_key: skipped
            seq 6  taskB#1  ONLINE 88 rows        <- new seq, same idem_key: skipped
            seq 7  taskC#0  Report ready          <- shown
```

The report gets the same treatment. `cycle_stamp = {plan_id, sorted provenance_ids}` is stored as `reported_cycle`, so a redelivery that reaches the exit path with the same evidence set writes nothing. The principle also says anything a run leaves behind must be recognizable as stale by the next run. A lock left by a crashed worker carries `expires_at`, and the acquire condition compares it against the current time. DynamoDB's `ttl` sweeper can run hours late, so no lock logic depends on it.

**Where you build it.** [Step 7](step-7-queue-and-worker-execution.md) builds the worker ritual and the write order. [Step 7a](step-7a-event-feed-and-idempotency.md) builds the replay-stable feed. [Step 4](step-4-identifiers.md) explains which ids must stay stable on replay.

## What happens when two things happen at once? { #two-at-once }

Crashes are one source of repeated work. Concurrency is the other. A user double-clicks Approve, and two API replicas receive the clicks fifty milliseconds apart.

**The tempting answer.** Add a `threading.Lock` around the resume. Or, to be safe, one global lock around all runs. Store everything in one table so it is all in one place.

**What goes wrong.** A `threading.Lock` lives in one replica's memory, and the other replica cannot see it. Both resumes run, the plan executes twice, and the checkpoint keeps whichever finished last. A global lock serializes threads that never touch each other. One table for checkpoints and feed lines means a feed write failure can take down a real run.

**The decision.** First remove the sharing. Different threads share nothing, so they run fully in parallel. Truth and narration go in separate tables. Each thread's lock lives under its own partition key. Then, where one writer really is required, serialize structurally. LangGraph does not support two runs on one thread, so that invariant gets a FIFO message group, a conditional-write claim with a tenure token, and a write fence on the data itself.

**Separate Before Serializing Shared State.** When concurrent actors might write the same object, first ask whether they need the same object, and give each its own if not. Serialize structurally only when one shared writer is a real invariant. The separation shows up in the table layout:

| Table | Item | PK | Owner |
|---|---|---|---|
| `{env}-agent-checkpoints` | checkpoint | LangGraph's own scheme | the saver |
| `{env}-agent-checkpoints` | resume claim | `CLAIM#{thread_id}` | the API, one approve at a time |
| `{env}-agent-events` | feed line | `{thread_id}`, SK `seq` | the worker's emitter |
| `{env}-agent-events` | run lock | `lock#{thread_id}` | the worker, one per thread |

Every lock has its own partition, so there is no hot lock row. An outage of the events table quiets the feed while the run keeps executing. The reader's dedup set lives per connection in API memory, so the durable log never has to track what each user has seen. Where serialization is real, it lives in the transport and the data, not in a convention:

```python
sqs.send_message(QueueUrl=queue_url, MessageBody=body, MessageGroupId=thread_id)

table.put_item(
    Item={"PK": f"CLAIM#{thread_id}", "SK": "RESUME", "claim_token": token, "expires_at": now + 1800},
    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
    ExpressionAttributeValues={":now": now},
)
```

The second click's write fails with `ConditionalCheckFailedException`, and the API returns `409`. Every renew and release is conditional on `claim_token`, so a frozen worker that wakes up after losing its lease cannot delete the new holder's claim. Capstone 1's Bug 8 was the version that released unconditionally.

**Where you build it.** The [DynamoDB and SQS bridge](bridge-dynamodb-and-sqs.md) teaches the two primitives. [Step 6](step-6-distributed-locks.md) builds the claim, the lease and the fence. [Step 7](step-7-queue-and-worker-execution.md) builds the two tables and the per-thread group.

## How much should the model see? { #how-much-the-model-sees }

The run is durable and serialized. Now look at it from the model's side. A user asks "and how does that compare to last quarter?", and the sales it needs were pulled two turns ago.

**The tempting answer.** Paste the prior results back into the context so the model can reuse them. Or give it a `search_held_data` tool to look them up.

**What goes wrong.** One retrieval can be thousands of rows, and a long thread holds megabytes. Pasting them back buries the conversation, uses up the token budget, and drowns the one line that matters. The lookup tool fails without any signal. When the model does not think to call it, it re-queries, and nothing records the missed reuse. Reuse can also turn into invention, when the model derives new figures from a fading memory of old ones.

**The decision.** Show the model an index, never a copy. `DataManifestMiddleware` rebuilds a `[held data]` block on every model call. Each result gets one line of about 100 bytes, showing the newest 30, newest first, with one rollup line for the rest. Reuse is a plan step the human can see, and it may reuse prose but never numbers.

**Guard the Context Window.** A full context window degrades reasoning, so keep summaries in the working context and route bulk payloads somewhere else. Here the bulk payloads stay in `state["source_results"]`, and the model sees this:

```text
[held data] - results already retrieved this thread. Prefer reusing a held result
over re-querying; check `retrieved` for staleness. Reference results by provenance id.
- REGIONAL:plan-chat-1-1:step-1:a3f9c2d1 | ok=True | rows=412 | retrieved 2026-09-10 | NZ sales by region, 2024
...plus 3 older result(s) held in state - ask the user before assuming their content.
[/held data]
```

`rows=412` tells the model that data exists to fetch by name. It does not put the rows in the window. The cap is 30 lines because attention is the limit, not tokens. The same principle sets the loop bound. `RetrievalBudgetMiddleware` caps a plan cycle at six retrieval rounds, and a turn that queries three sources in parallel spends one round.

**Where you build it.** [Step 8d](step-8d-carryover-and-data-manifest.md) builds the manifest and the reuse rules.

## How will the next engineer find their way around the code? { #finding-your-way }

The design now has gates, middlewares, workers, locks and a feed. Picture the engineer who opens it six months from now because a report came back about the wrong country.

**The tempting answer.** Put it all in one `agent.py`. Or split it by phase into load, plan, execute and save.

**What goes wrong.** Capstone 1's Bug 5 was an invariant spread across three files with no owner. Starting a turn meant writing three `LastValue` channels together. The hand-built dict lived in one file, the channel declarations in another, and the readers in a third. A caller that wrote only `messages` silently kept the previous turn's context. Phase-named modules make this worse, because the same domain rule gets repeated at every phase.

**The decision.** Group modules by the boundary they own, so every silent failure has one obvious first file. Declare each state channel next to its only writer. Keep shared names in one contract module.

**Minimize Reader Load.** Count the layers between a reader's question and its answer and the hidden state they must hold in their head, then cut both. The test is whether a new reader can answer "where does X come from?" in under thirty seconds. For `raw_question`, the answer is one file:

```python
# proposal_context.py
class _UserContextState(AgentState):
    raw_question: NotRequired[str]
    user_context: NotRequired[str]

def build_proposal_graph_input(question: str, user_context: str) -> dict: ...
```

The channel declaration, the one writer, and the middleware that reads it sit together. LangGraph merges this schema with `DeepState` in `gate.py` at compile time, so co-location costs nothing at runtime. The package map then works as a review index:

| If the bug is about... | Look first in... |
|---|---|
| A plan approved, refined or locked incorrectly | `gate.py`, `plan.py` |
| A stale default changed model behavior | `proposal_context.py`, `manifest.py` |
| A run executed twice or did not resume | `resume_claim.py`, `resume_decision.py`, `run_queue.py`, `worker_lock.py` |
| Progress duplicated or disappeared | `curator.py`, `event_log.py` |

**Where you build it.** [Step 8e](step-8e-code-components-and-organization.md) maps every module group.

## In what order do I build it so each piece proves itself? { #build-order }

You know what to build. The last design question before you start is the order.

**The tempting answer.** Build the whole graph, wire in the queue and the worker, then test end to end once everything exists.

**What goes wrong.** Step 3 names its hardest unit, adding `plan_id` and `interrupt_id` validation, because that is where idempotency and stale-state bugs both surface. If the queue, the worker and the lock are already stacked on top when that first goes red, you cannot tell which layer broke.

**The decision.** Build in units that each end in a check you can run. Write the check as a script or test that anyone can rerun, not as a one-time manual try.

**Sequence work into verifiable units.** Break the work into small units that each end in a state you can check, and do not start the next one until the current one is green. Step 3 lays out its own build this way:

```text
1. Plan generation only. No interrupt. Validate content.
2. Add interrupt(). Resume from a test with Command(resume={"type": "approve"}).
3. Add the approve endpoint. Two real requests.
4. Add the refine path. Feedback returns as a ToolMessage.
5. Add plan_id and interrupt_id validation. Test the stale-tab case.
6. Add the async approve path. Stage, write queued, enqueue, return 202.
7. Add the claim. Two simultaneous approves give one run and one conflict.
```

The series runs in the same order. State comes before context, and context before approval. Identifiers come after you have seen a plan pause and resume. Locks come after the browser can double-click Approve. Prompt design comes after code already owns the boundaries.

**Build the Lever.** For any non-trivial work, build the tool that does or proves it, such as a script or a rerunnable check, so a reviewer can rerun it instead of trusting you. Each unit here has a lever. Step 5's lever is a forced-disconnect test that closes the client right after the first terminal frame and asserts that the save still completes. Step 6's is a takeover scenario. A acquires, A's lease expires, B acquires, A tries to release, and B's claim must remain. For the model's behavior, the lever is the evaluation loop, with a versioned dataset, a code grader where the check can be stated exactly, and per-case verdicts:

```python
def grade_sources(case, run) -> str:
    return "correct" if run.sources_called == case["expected_sources"] else "incorrect"
```

Its sixth rule turns every production failure into a case before the fix lands, so the dataset grows wherever the agent has actually failed.

**Where you build it.** [Step 3](step-3-planning-and-human-approval.md#an-implementation-path) gives the unit sequence for the gate. The [evaluation bridge](bridge-evaluation.md#the-minimum-evaluation-loop) gives the evaluation loop.

## When it breaks, how do I fix it for real? { #fixing-it-for-real }

It will break anyway. Before one release, code review found nine bugs in this agent, and none of them crashed.

**The tempting answer.** Patch the line where the symptom showed up. If a scan finds the wrong record, make the scan smarter. If the model uses a stale default, stop injecting the default in that phase.

**What goes wrong.** Capstone 1's Bug 1 is the clearest case. During execution, middleware injected "default country: Australia" on every model call, including calls after the human had approved a plan for Germany. The fresh default sat near the top of the prompt, the approval sat lower in history, and the report came back about Australia. The obvious fix was to gate injection on `plan["status"] == "locked"`. That fix broke multi-turn sessions.

**The decision.** Trace each symptom to the first wrong decision. When a fix fails, write down what it assumed before trying another.

**Fix Root Causes.** Trace each symptom back to the first place the system did something wrong and fix it there. A guard counts as a fix only when the state it handles is legitimate. Bug 9 shows the trace. Its symptom was a follow-up planned against an older topic's entities. The cause went three levels deep:

```text
entity_groups = values.get("entity_groups") or None   # [] reset becomes None
if md.entity_groups: item["entity_groups"] = ...       # None is not written
for msg in reversed(memory): if groups: return groups  # scan lands on an older turn
```

Patching the scan would hide the bug. The first wrong decision was storing session state inside message records, so the fix moved it to a purpose-built store with direct overwrite, where `[]` means reset and `None` means never set. Bug 3 shows the guard rule from the other side. A multimodal message with list content is a legitimate state, so the code that broke on it, a backward scan that silently retargeted an older message, was the root cause. The fix stops and logs at that point. The principle also says to check for the pattern, not just the instance. In Bug 7, quick mode had already handled a missing `chat_id` correctly by skipping the write.

**Attack the Premise.** When fixes that share one premise keep failing the same check, write the premise down and test it directly before writing another fix that assumes it. The plan-status gate rested on one sentence:

```text
Premise: the plan channel tells a model call which phase it belongs to.
Test:    turn 2 asks a new question on the same thread.
         The plan channel still holds turn 1's LOCKED plan.
         Turn 2 is planning, but the gate reads "locked" and drops the defaults it needs.
Result:  premise false. plan is a sticky LastValue channel, and phase is not stored in it.
```

The series records one failed fix here, and the principle's trigger is two. Writing the premise down after the first failure was cheaper than a second fix built on the same belief. With the premise gone, code had no reliable phase signal. The rule moved into the context block, which the model reads alongside the conversation that shows the approval:

```python
"[User context - entities named explicitly in the question always win over these defaults, "
"and once a research plan is approved and locked, the locked plan's entities "
"win over these defaults]\n"
```

**Where you build it.** [Capstone 1](capstone-1-nine-silent-failures.md) walks through all nine bugs. [Step 8](step-8-orchestrator-prompt.md#the-case-that-has-to-be-both) steps through Bug 1.

## When the requirements change, what do I do with the design I already have? { #when-requirements-change }

The last question is about time. Requirements arrive after code exists. In this agent the biggest one was human approval before anything expensive runs.

**The tempting answer.** Bolt it on. Chapter 1's exercise puts it in a colleague's words: "Easy, just interrupt the function, wait for the approval callback, then resume." Later, once approval works, keep the approve request streaming execution back to the browser, because that is convenient for local testing.

**What goes wrong.** The bolted-on version holds a connection open for days. The streaming approve request ties a forty-minute run to one HTTP response and one API process, so a closed laptop or a deploy kills research the human already approved.

**The decision.** Treat approval as a requirement that was there from the start, which changes the topology. Then converge on the target, an approve that returns `202 queued` and a worker that resumes with `None`, rather than maintaining both paths. Before committing to an unfamiliar design, compare real alternatives.

**Redesign From First Principles.** When a new requirement arrives, do not bolt it on. Ask what you would build if it had been a foundational assumption, and carry the change through every reference. With approval assumed from day one, a turn spans several HTTP requests. That needs durable checkpointed state that any replica can load, a pause that serializes to storage instead of blocking a thread (`interrupt()`), and a separate endpoint that delivers the decision. It also changed the payload, which now carries `plan_id`, `interrupt_id` and `owner_id` so the second request can prove it is approving the pause the human saw.

**Outcome-Oriented Execution.** In a planned rewrite, converge on the verified end state instead of keeping intermediate states smooth with throwaway compatibility code. The synchronous approve stream helped with local testing, but it was never the production target. The series teaches only the target path, and the learning record states the rule directly:

```text
approval writes the resume decision into the checkpoint, writes run-state as queued,
enqueues SQS FIFO, and returns 202 queued; a worker later resumes with None.
The older synchronous approval stream is only a legacy/interim path.
```

**Migrate Callers Then Delete Legacy APIs.** When a new internal API replaces an old one, migrate every caller and delete the old API in the same wave, and update the tests to assert the new contract. The factory from Bug 5 is the small example. Once `build_proposal_graph_input` existed, every route that started the graph moved onto it, and the hand-built dicts went away, so a forgotten key could no longer happen. For the synchronous approve stream, the series records that it is legacy but not when it was deleted. The principle says when. Delete it in the same change that moves its last caller and its tests onto the `202` path, so nobody new copies the old shape.

**Exhaust the Design Space.** For a decision with no precedent, build two or three competing designs and compare them side by side before you commit. Context injection was that kind of decision, and four designs were compared:

```text
system prompt injection     rejected: the orchestrator prompt is request-invariant and cached
checkpointed message        rejected: the block would pile up in history across turns
tool call                   rejected: an extra round trip, and the model can skip it
middleware before the call  chosen:   injected after the checkpoint is read, never stored
```

The data manifest went through the same comparison, an ambient index against a `search_held_data` tool, and the tool lost because it fails without a signal. Bug 1's fix was a third comparison, a code gate against a prompt rule, and the prompt rule won only after the code gate's premise failed.

**Where you build it.** [Step 3](step-3-planning-and-human-approval.md) builds the gate that approval requires. [Chapter 3](ch3-architecture-and-flow.md) shows the async approve path end to end. [Capstone 1](capstone-1-nine-silent-failures.md#why-middleware-for-context-injection) records the four injection designs.

## The design on one page { #the-design-on-one-page }

| Question | Decision | Principles | Built in |
|---|---|---|---|
| [What does "done" mean?](#what-done-means) | Separate transport, semantic and task success; pair eval cases and keep per-case verdicts | Explain the Number, Test Behavior, Not Implementation, Prove It Works | [Capstone 2](capstone-2-when-the-score-passes.md), [evaluation bridge](bridge-evaluation.md) |
| [Do I need a deep agent at all?](#do-you-need-a-deep-agent) | Reactive mode for one-pass answers; open the plan gate only for a new question | Laziness Protocol, Subtract Before You Add | [Step 8a](step-8a-reactive-mode-prompt.md), [Step 8c](step-8c-plan-lifecycle.md) |
| [What are the nouns and states?](#nouns-and-states) | One id per concept with a stated replay behavior; a merge rule per state key; the plan state machine in code | Foundational Thinking, Model the Domain, Type System Discipline | [Step 1](step-1-state-and-checkpoints.md), [Step 4](step-4-identifiers.md), [Step 3](step-3-planning-and-human-approval.md) |
| [Where are the boundaries?](#boundaries) | Inject context in middleware, escaped and never checkpointed; code enforces tools and the gate, prompts shape judgment | Boundary Discipline, Encode Lessons in Structure | [Step 2](step-2-context-injection.md), [Step 8](step-8-orchestrator-prompt.md) |
| [Where does the human come in?](#where-the-human-comes-in) | `interrupt()` persists the pause; approve returns `202 queued`; proceed under declared assumptions; work outlives the browser | Never Block on the Human, Experience First | [Step 3](step-3-planning-and-human-approval.md), [Step 5](step-5-streaming-and-background-work.md), [Step 8b](step-8b-clarifications.md) |
| [What happens when it crashes halfway?](#crash-halfway) | Fixed write order; delete the message last; identity from execution position | Make Operations Idempotent | [Step 7](step-7-queue-and-worker-execution.md), [Step 7a](step-7a-event-feed-and-idempotency.md) |
| [What happens when two things happen at once?](#two-at-once) | Separate tables and per-thread partitions; FIFO group, token-fenced claim and write fence where one writer is required | Separate Before Serializing Shared State | [Step 6](step-6-distributed-locks.md), [DynamoDB and SQS bridge](bridge-dynamodb-and-sqs.md) |
| [How much should the model see?](#how-much-the-model-sees) | An ambient index of held results, capped at 30 lines; reuse prose, re-fetch numbers | Guard the Context Window | [Step 8d](step-8d-carryover-and-data-manifest.md) |
| [How will the next engineer find their way?](#finding-your-way) | Modules grouped by boundary; each channel declared beside its only writer | Minimize Reader Load | [Step 8e](step-8e-code-components-and-organization.md) |
| [In what order do I build it?](#build-order) | Units that each end in a rerunnable check; the eval loop as the check for behavior | Sequence work into verifiable units, Build the Lever | [Step 3](step-3-planning-and-human-approval.md#an-implementation-path), [evaluation bridge](bridge-evaluation.md#the-minimum-evaluation-loop) |
| [How do I fix it for real?](#fixing-it-for-real) | Fix at the first wrong decision; write down and test the premise of a failed fix | Fix Root Causes, Attack the Premise | [Capstone 1](capstone-1-nine-silent-failures.md) |
| [When the requirements change?](#when-requirements-change) | Redesign around approval; converge on the async path; migrate callers and delete the old path; compare designs first | Redesign From First Principles, Outcome-Oriented Execution, Migrate Callers Then Delete Legacy APIs, Exhaust the Design Space | [Step 3](step-3-planning-and-human-approval.md), [Chapter 3](ch3-architecture-and-flow.md), [Capstone 1](capstone-1-nine-silent-failures.md#why-middleware-for-context-injection) |

## From answers to architecture

Put the twelve answers next to each other and the system takes shape. Durable state and explicit identities give you a checkpoint that any process can reload. The human's pause is a record in that checkpoint, so approval becomes a second request that stages a decision and queues a pointer. A per-thread queue group and a fenced claim give each thread one writer. Truth and narration need separate homes, so the worker writes curated events to their own table and the browser tails them. Chapter 3 draws that system end to end, with the four durable records, the order approval writes them, and the worker that resumes with no input. Read it as the architecture these answers produce.

---

!!! check "You should now understand"
    - The twelve design questions, and why they come in this order
    - The tempting first answer to each, and the real bug it caused in this agent
    - Which principle settles each question, and the code or state trace that shows it
    - Which Step builds each answer

??? question "Try this"
    **A teammate proposes adding a line to the orchestrator prompt: "Never call query_market_data before the plan is approved." Which design question does this belong to, and what do you tell them?**

    ??? success "Answer"
        It belongs to the boundaries question. Code can enforce this rule deterministically, so it belongs in structure, not in the prompt. The gate's `interrupt()` already stops execution until the human resumes, and `AllowedToolsMiddleware` controls which tools the model can see. A prompt sentence would compete with the model's other instincts and sometimes lose. Encode Lessons in Structure also says that once the structural fix exists, you do not repeat it as text.

*Next: [Chapter 3 · End-to-end architecture and orchestrator flow](ch3-architecture-and-flow.md). The architecture these answers produce, traced from the first question to the worker's last write.*
