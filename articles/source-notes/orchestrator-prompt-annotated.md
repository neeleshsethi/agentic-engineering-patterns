# The Orchestrator: Prompt, Gate, and How to Build One

> Reconstructed from source notes. Genericized: product → "the platform"; brand → `BRAND_A`;
> data sources → `SOURCE_A`/`SOURCE_B`/`SOURCE_C`/`SOURCE_D`; the retrieval tool → `query_source`.
> Framework names (LangGraph, deepagents, DynamoDB) are real. The domain (pharmaceutical
> commercial analytics) is kept because the regulatory-routing rationale needs it.

This note has three parts: the **annotated system prompt** (what each line is load-bearing for),
the **gate internals** (`submit_plan`, `project_plan`), and a **build-it-yourself** walkthrough
that reconstructs the whole human-in-the-loop plan-approval pattern from `interrupt()` up.

The one idea underneath all of it:

> **The LLM generates content; the code manages the state machine.** Anywhere you find yourself
> trying to prompt the LLM into making a *state* decision ("only proceed if the plan is
> approved"), move it to code.

---

## Part 1 — The annotated system prompt

### Section 1: Identity

> *You are the platform's deep-thinking orchestrator for pharmaceutical commercial analytics.*

One sentence, three constraints packed in:

- **"orchestrator"** — not "assistant", not "analyst". The model's job is coordinating steps, not
  answering from its own knowledge. This word biases toward tool use over freeform generation.
- **"deep-thinking"** — signals that multi-step plans are expected. Without it, models sometimes
  collapse simple-looking questions into a direct answer, skipping the plan/approval flow.
- **"pharmaceutical commercial analytics"** — the domain anchor. Grounds entity interpretation
  ("brand" means a drug brand, "market" means a country market) without needing a glossary.

### Section 2: Data source routing

> *Use the routing guidance below to assign every research step its single best source. Respect
> every scope, out-of-scope, and country rule exactly as written. {routing_section}*

- **"every research step" / "its single best source"** — *single* is doing real work. Early
  iterations produced tags like `[source: SOURCE_A + SOURCE_B]` or `[source: BOTH]`, which the
  regex parser can't handle (it extracts one `\w+` token).
- **"exactly as written"** — the routing rules include country exclusions that are *regulatory,
  not stylistic*. An LLM's instinct is to treat guidance as advisory and route to the "most
  helpful" source. This phrase converts the routing section from advice into law — it matters most
  for cases like `Italy → SOURCE_C`, where the model would otherwise pick a source that must not
  serve that country.
- **`{routing_section}`** — the placeholder receives the same rendered per-source
  capability/scope/country rules the quick path's router prompts with. Sharing one rendering
  function means deep mode can never silently drift from quick mode's routing behavior. A rule
  fixed in one place is fixed in both.
- **Routing is per-step, not per-question.** A comparative question spanning two sources must
  produce two routed steps, each with its own assignment.

### Section 3: The workflow — five numbered steps

> *Workflow: plan, get approval, then execute.*

The header states the invariant ordering. The five numbered steps give the model a **phase machine
it can locate itself in**: at any point in a long conversation, the model matches its current
situation ("I just got refine feedback") to a numbered step and reads off what to do. Unnumbered
prose loses this — models skip phases when the phases don't have names.

#### Step 1: PLAN

> *Break the user's question into concrete research steps and record them with `write_todos`.
> Every retrieval step's content MUST end with routing tags, exactly in this form:*
> `<what to find out> [source: <SOURCE_ID>] [group: <N> parallel]`

- **"record them with `write_todos`"** — names the exact tool. The plan is not a message, not a
  markdown list in prose — it's a `write_todos` call. This is what makes the plan machine-visible.
- **"MUST end with routing tags, exactly in this form"** — the format contract, given by grammar
  and example. `project_plan()` parses these tags with two regexes into
  `PlanStep.assigned_source` and `PlanStep.exec_group`. The prompt is the *producer* side of the
  contract; the projection is the *consumer* side. "Exactly in this form" plus a literal template
  is the difference between 99% parse success and constant format drift.

> *Resolve the entities FIRST: when the question says "my …" or omits the country or brand, fill
> the gap from the `[User context]` block attached to the LATEST user message and from earlier
> turns of this conversation — BEFORE deciding routing and BEFORE asking any clarification.
> Entities named explicitly in the question always win over defaults.*

Three load-bearing pieces:

- **"FIRST … BEFORE deciding routing"** — an explicit ordering of internal reasoning. Routing
  rules are entity-scoped (source availability depends on country/brand), so routing before
  resolving entities produces routes invalid for the entities eventually chosen. The prompt fixes
  the evaluation order because the code can't — there's no code between the model's thoughts.
- **"attached to the LATEST user message"** — points the model at where the middleware injects the
  block. The block is transient (never checkpointed), so the model must know to look for it on the
  current message rather than scanning history.
- **"Entities named explicitly in the question always win over defaults"** — the priority stack.
  Without it, a user with AUSTRALIA defaults asking "give me GERMANY sales" occasionally got an
  AUSTRALIA plan, because the injected default sat at higher salience than the question's own
  words.

**The clarification ordering:** clarify *last*, only when question + conversation + context all
fail. This suppresses the model's tendency to ask permission-seeking questions ("Which country did
you mean?") when the answer is derivable — which would burn the user's one approval interaction on
noise.

**Right-size the plan:** ONE retrieval step per source, not one per metric. Each retrieval is a
full research request that can ask for many related metrics at once. A question one source fully
covers is a **two-step plan** (that retrieval + a synthesis step). Split a source's work into
several steps only when a later step genuinely needs an earlier step's results (sequential
groups) — never just to parallelize one source's metrics.

> *The step-explosion guard.* Without this, "sales, share, and growth" produced three separate
> retrieval steps against the same source — three source calls, three times the latency and cost,
> and a plan too long for a human to meaningfully review. The line works by redefining the unit: a
> retrieval step is a *research request to one source*, not a metric.

**User sovereignty over routing — with one carve-out.** If the user names a source explicitly (by
ID or a recognizable alias), route the matching steps to THAT source: the user's explicit choice
beats your preference, and there's nothing to clarify. The single exception is an absolute
exclusion in the routing guidance (country/brand rules): then use the valid source and *say why in
the plan's scope*. This kills two failure modes: (a) the model "knows better" and silently
re-routes a user-named source; (b) the model treats a user-named source as ambiguity and asks a
clarification when the user already said which source.

**The ambiguity protocol — note what it is *not*: it's not "stop and ask".** When two similar
sources report the same metric at different grains and the question doesn't say which, the model
plans the most standard interpretation and *asks* — by passing a one-line question in
`submit_plan`'s clarification argument that names both options and the assumption the plan makes.
The answer arrives as plan feedback. This preserves the one-interrupt design (exactly one human
checkpoint); a separate clarification round-trip would add a second interrupt and a second HTTP
trip.

**The parser-protection rule:** `<SOURCE_ID>` is exactly ONE source ID. Never `+`, never `BOTH`.
When routing selects multiple sources for the same need, write one step PER source, each with its
own single `[source:]` tag, sharing the same `[group: N parallel]` number. The shared group number
keeps them parallel in the execution DAG without breaking the one-source-per-tag grammar.

**The no-coverage path:** if routing yields NONE for part of the question, do NOT create a
retrieval step for it, never write `[source: NONE]`, and never invent a source ID. Note the
uncovered part in the scope, and say in the final report that it could not be researched. (Three
escalating prohibitions because models tried all three.)

**The DAG grammar:** group numbers are execution ordering; `parallel`/`sequential` is the
concurrency mode within the ordering. This is how a flat todo list encodes a dependency graph
without any graph syntax. Finish with one synthesis step (e.g. `Synthesize the final report
[group: 3 sequential]`) — it gets a group tag but **NO source tag** (the one step that breaks the
"every retrieval step ends with both tags" pattern, spelled out because models either gave it a
bogus source tag or dropped its group tag).

#### Step 2: SUBMIT

> *Call `submit_plan` with a short report title, a one-line scope (brand, market, time window), and
> the entities the plan resolved — one `{"country":…, "brand":…, "period":…}` entry per distinct
> combination the research covers, leaving out anything the question does not pin down. Never call
> `query_source` before the plan is approved.*

- **The entities argument spec** is given exactly because these dicts flow through
  `sanitized_entity_groups()` into DynamoDB and power the next turn's carryover.
- **"Leaving out anything the question does not pin down"** prevents padding unresolved slots with
  guesses that would poison the next turn's defaults.
- **"Never call `query_source` before the plan is approved"** — the gate invariant in prose. Note
  the layering: it's also enforced by the ToolMessage flow (the model only gets the "execute now"
  instruction after approval). Belt and suspenders — prompt for the first pass, tool result for
  every pass after.

#### Step 3: REFINE

> *If `submit_plan` reports requested changes, update the todo list with `write_todos` to apply the
> feedback (keep the routing tags on every retrieval step), then call `submit_plan` again. The
> user's wording, scope, and step changes always win; never argue with feedback. Routing stays
> yours: if feedback asks for a source the guidance excludes for that country/brand (exclusions are
> absolute), keep the valid source and note why in one line of the resubmitted plan's scope.*

- **"update the todo list … then call `submit_plan` again"** — the two-call refine sequence, in
  order. Without it: the model calls `submit_plan` again with unchanged todos (the projection
  rebuilds the identical plan; the user sees their feedback ignored), or edits todos and doesn't
  resubmit (the run stalls with no pending gate).
- **"keep the routing tags on every retrieval step"** — tag preservation through edits. Rewording
  is exactly when the model is most likely to drop trailing tags; a dropped tag means
  `assigned_source = None`.
- **"never argue with feedback"** — suppresses the model's instinct to defend its plan in the tool
  response. The user only ever sees the re-presented plan, not the model's reasoning.
- **"Routing stays yours … exclusions are absolute"** — the one boundary feedback cannot cross,
  mirroring the PLAN-time carve-out.

#### Step 4: EXECUTE

> *Once `submit_plan` reports the plan is approved and locked, run the research steps: call
> `query_source(source, sub_question)` for each retrieval step using that step's `[source]` tag,
> mark steps `in_progress` and `completed` with `write_todos` as you go, then write the final
> report. The locked plan's entities are FINAL — never re-resolve entities from the `[User
> context]` block during execution.*

- **"using that step's `[source]` tag"** — closes the plan→execution loop: the `source` argument
  comes from the tag the model wrote at plan time, which the human approved. Execution has no
  routing discretion left.
- **"mark steps `in_progress`/`completed` with `write_todos`"** — this is what makes the plan a
  live progress tracker. Every status-tick `write_todos` re-triggers the curator's projection,
  which re-emits the plan as an SSE frame with updated statuses — the frontend's progress UI is
  driven entirely by the model obeying this clause.
- **"The locked plan's entities are FINAL — never re-resolve"** — the highest-stakes sentence in
  the prompt. The `[User context]` block (with the user's defaults) is *still injected by
  middleware on every execution model call* — the middleware doesn't know what phase the run is in.
  Without this line, the model mid-execution would re-read "defaults: AUSTRALIA" at top salience
  and re-resolve entities the human already approved as GERMANY. **This is Bug 1** (see below).
  There is no code enforcement possible here; the prompt is the only thing standing between the
  injected default and a wrong-country report.

#### Step 5: FOLLOW-UP

> *A new user question after the report starts a NEW research cycle in this same conversation — the
> same workflow from step 1. Resolve references ("that brand", "same period", "what about
> GERMANY?") against the earlier turns; keep every entity the new question does not change, replace
> the ones it does. Write a FRESH todo list covering ONLY the new question (replace all previous
> steps — never re-run or carry over completed ones), and submit for approval before any retrieval,
> passing `submit_plan` the follow-up's FULL resolved entities — carried-over ones included, never
> just the delta.*

- **"a NEW research cycle … the same workflow from step 1"** — tells the model the gate applies to
  follow-ups too. Without it, models treat a follow-up as a continuation of the approved plan and
  go straight to retrieval — executing unapproved.
- **"a FRESH todo list … replace all previous steps"** — because `write_todos` replaces the whole
  list, but the model sees the old completed todos in state; without this it mixes old and new.
- **"FULL resolved entities — never just the delta"** — the entities argument must be the complete
  post-diff set. If the model passes only `{country: GERMANY}` (the delta), the persisted entity
  row loses the brand, and the next follow-up ("and the year before?") has nothing to carry the
  brand from. Delta-passing was the single most common follow-up defect before this clause.

### Section 4: Grounding rules

> *Ground the final report ONLY in `query_source` results. If a call returns `success=false`, mark
> that step `completed` with `write_todos` and state plainly in the report that this step's
> retrieval failed — never fill the gap from your own knowledge, and never state, estimate, or
> approximate metrics not present in a tool result. If every retrieval fails, report that and
> stop.*

This is a deliberate counterweight to the deepagents base prompt appended after it, which tells
the agent to *keep iterating until the task is done*. Combined with a failed retrieval, "keep
iterating" reads to a model like "find another way to produce the number" — and the model's other
way is its own training data. In a pharma analytics product, a model-remembered market share
presented next to real retrieved figures is indistinguishable from data: **a compliance problem,
not a quality problem.**

- **"ONLY in `query_source` results"** — the whitelist framing. Not "don't hallucinate" (which
  models agree with and then do) but a positive statement of the only admissible evidence source.
- **"mark that step `completed` (statuses are only `pending`/`in_progress`/`completed`)"** — the
  parenthetical exists because models invented a `failed` status, which the status map doesn't
  recognize (unknown statuses coerce to `pending`, making a finished-and-failed step look not yet
  started). A failed retrieval is a *completed* step whose outcome is reported in prose.
- **"never state, estimate, or approximate"** — three verbs because models negotiate: told not to
  state figures, they estimate ("approximately 40%"); told not to estimate, they approximate with
  ranges. Enumerating the verbs closes the ladder.
- **"If every retrieval fails, report that and stop"** — "and stop" counteracts the base prompt's
  keep-iterating pressure one final time: an honest empty-handed report is task completion.

### What's deliberately NOT in the prompt

Reading what a prompt omits tells you where the code is doing the work:

| Not in the prompt | Because the code enforces it |
|---|---|
| "Only proceed if the plan is approved" | The gate's `interrupt()` structurally blocks continuation; the approve/refine ToolMessage tells the model what's next |
| "Do not use the task / filesystem tools" | `AllowedToolsMiddleware` removes them from the request — the model never sees them |
| Plan status transitions, modification counting | Gate-owned fields; the model can't write them (the Plan is a read-only projection over its todos) |
| `plan_id` / owner validation, concurrency | Resume-endpoint preconditions and the DynamoDB claim |
| The `[User context]` block's own content rules | The block carries its own header — rules travel with the data they govern |

**The pattern:** the prompt shapes *judgment* (what to research, how to split it, how to interpret
feedback), and states invariants only where no structural enforcement exists (entity finality
during execution, grounding). Everything with a hard boundary — tool availability, gate
sequencing, state ownership — is code, and spends zero prompt tokens.

**Versioning note:** a `PROMPT_VERSION` (e.g. `"2.2"`) rides along as trace metadata, identifying
which prompt a deployment ran. The convention: bump once per shipped change, never per iteration
inside an unmerged branch — the version answers "which prompt produced this trace," not "which
local edit."

---

## Part 2 — Inside the gate: what `submit_plan` actually does

The prompt tells the model *when* to call `submit_plan`. This is what happens *inside* the call —
and the obvious way to build it (a `while True` refine loop inside the tool) is **not** how it
works, for a structural reason.

### Step 1: read the prior plan from state

```python
prior_raw = runtime.state.get("plan")
prior: Plan | None = None
if isinstance(prior_raw, dict) and prior_raw:
    try:
        prior = Plan(**prior_raw)
    except Exception:
        # A checkpoint written by an older Plan schema must not kill the run — rebuild from
        # todos; only id continuity is lost.
        logger.warning("Ignoring unreadable prior plan in state; rebuilding from todos")
```

The `plan` state channel holds the most recently submitted plan as a plain dict (it crosses the
checkpointer, so it can't be a Pydantic object). The gate is this channel's only writer. The prior
matters for two things: step-id stability across refinements, and detecting a new question cycle.

### Step 2: compute the projection from state

```python
plan = project_plan(
    runtime.state.get("todos") or [],   # what the model wrote via write_todos
    thread_id=_thread_id(runtime),      # seeds the deterministic plan_id
    prior=prior,                        # id stability + gate-owned field carryover
    title=title, scope=scope,           # from the model's submit_plan arguments
)
```

Note what the plan is built from: **the `todos` state channel, not from any argument the model
passed.** `submit_plan`'s arguments are only metadata (`title`, `scope`, `clarification`,
`entities`). The plan *content* is whatever the last `write_todos` call left in state. This is why
REFINE insists on "update the todo list with `write_todos`, then call `submit_plan` again": calling
`submit_plan` without editing todos first re-projects the identical plan, and the user sees their
feedback ignored.

The projection is **pure**: same todos + same prior → the same `Plan`, bit for bit. And `plan_id`
derives deterministically from `thread_id`. Both properties exist for one reason, which the next
step makes clear.

### Step 3: `interrupt()` — once, not in a loop

```python
new_cycle = prior is not None and prior.status == "locked"
decision, feedback = _decision_of(interrupt({
    "kind": "plan_approval",
    "thread_id": _thread_id(runtime),
    "owner_id": _owner_id(runtime),     # resume executes under the plan owner
    "plan": plan.model_dump(),
    ...
}))
```

**There is no `while True` around this.** Each `submit_plan` invocation calls `interrupt()`
exactly once and returns. The natural instinct — loop inside the tool: interrupt, get feedback,
apply it, re-present, repeat until approve — is *structurally impossible* here, and seeing why
illuminates the whole architecture:

> **The tool cannot apply feedback, because the tool cannot write todos.** The plan content lives
> in the `todos` channel, and the only writer of that channel is the model calling `write_todos`.
> Applying "add GERMANY" requires interpreting feedback and editing research steps — LLM work. A
> tool is plain Python; it has no model to call. So on refine, the gate's only possible move is to
> hand control back to the model with the feedback as its tool result.

**The refine "loop" is therefore the agent loop itself:**

```
model:  write_todos(...)
model:  submit_plan(...)            ── one gate call
gate:   project_plan(todos) → interrupt()  ── PAUSE, one HTTP round trip
gate:   returns refine ToolMessage
model:  write_todos(edited)         ── the AGENT interprets the feedback
model:  submit_plan(...)            ── a NEW gate call
gate:   project_plan(new todos) → interrupt()  ── a NEW PAUSE
        … until the resume says approve
```

This also keeps a principle the codebase names explicitly: **the agent is the sole writer of the
plan** — feedback is conversational, never a direct edit. Nothing in the code ever patches the
plan from feedback text. What the human approves is always something the model authored end to end.

**Why purity and determinism matter here:** everything before `interrupt()` re-runs on every
resume — LangGraph replays the node from the top and only then delivers the resume value as
`interrupt()`'s return. So steps 1–2 execute again on approve, again on each refine resume. Because
the projection is pure and `plan_id` is thread-derived, each replay rebuilds the identical plan —
the re-run is invisible. A random `plan_id`, a timestamp, or any side effect in steps 1–2 would
fork a "different" plan on every resume and break the endpoints' `plan_id` precondition checks.

### Step 4: normalize the decision — refuse to lock on garbage

```python
def _decision_of(resume_value):
    if isinstance(resume_value, dict):
        kind = str(resume_value.get("type", "")).lower()
        feedback = str(resume_value.get("feedback", "")).strip()
        return ("approve", "") if kind == "approve" else ("refine", feedback)
    if isinstance(resume_value, str):
        return "refine", resume_value.strip()
    return "refine", ""
```

The fail-safe direction is deliberate: anything that is not an explicit, well-formed approve is
treated as **refine**. A malformed resume payload, a bare string, an unknown type — none can lock a
plan. The worst outcome of a corrupt decision is one wasted refine round; the worst outcome of the
opposite default would be executing an unapproved plan.

### Step 5: return a `Command` — state write + the ToolMessage hint

```python
if decision == "approve":
    locked = plan.model_copy(update={"status": "locked"})
    return Command(update={
        "plan": locked.model_dump(),
        **entity_update,
        "messages": [ToolMessage(
            "Plan approved and locked. Execute the research steps now, exactly as planned.",
            tool_call_id=runtime.tool_call_id)],
    })
else:  # refine
    bumped = plan.model_copy(update={"modification_count": plan.modification_count + 1})
    return Command(update={
        "plan": bumped.model_dump(),
        "messages": [ToolMessage(
            f"The user requested changes to the plan: {feedback}\n"
            "Update the todo list with write_todos to reflect this feedback, then call "
            "submit_plan again. Do not start any retrieval yet.",
            tool_call_id=runtime.tool_call_id)],
    })
```

One return value does three jobs atomically:

1. **Writes the `plan` channel** — `locked` on approve, `modification_count`-bumped on refine. The
   status transition happens here, in code, via `model_copy` — the model never writes `status` or
   `modification_count` (they're not in the todos, so the projection can't produce them from model
   output; it only carries them from `prior`).
2. **Writes `entity_groups`** with new-cycle-aware semantics: within a refine cycle, an
   omitted/empty entities argument keeps the previous value; at a cycle boundary it resets to `[]`
   instead, because inheriting the previous question's entities would persist wrong-country
   carryover — empty beats wrong.
3. **Delivers the hint** — the ToolMessage is the only signal the model receives about what phase
   it's in now. This is the answer to "how does the orchestrator know whether to refine or
   execute": it reads its tool result. The decision was made by the human, routed by
   `_decision_of`, translated into one of two imperative instructions by the gate. The prompt's
   REFINE/EXECUTE steps pre-explain both messages, so when one arrives the model recognizes it as
   "I am now in step 3" or "I am now in step 4."

> **The gate in one sentence:** `submit_plan` is a pure projection plus a single `interrupt()`
> plus a `Command` — it turns the model's todos into the typed plan (replay-safely), pauses for
> exactly one human decision, refuses to lock on anything but an explicit approve, and answers the
> model with the one line of instruction that moves the workflow to its next numbered step — while
> the refine loop, the part that needs intelligence, stays where the intelligence is: in the
> model.

---

## Part 3 — Build it yourself

### Start with the naive version

A research agent without human-in-the-loop:

```
User sends question → LLM generates plan (internal, never shown) → Agent executes → returns answer
```

Everything in one HTTP request, one graph invocation, one SSE stream. **The problem isn't
correctness** — the LLM might execute the right plan. The problem is **trust and cost**: research
plans that hit external APIs, run queries, or browse the web are expensive and slow. If the LLM
misread the question, you want to catch that *before* execution. So you want to show the plan
first.

### The simplest human-in-the-loop: `interrupt()`

LangGraph provides one primitive for pausing a graph and waiting for human input: `interrupt()`.
Under the hood it serializes the current graph state to the checkpointer and raises a special
exception the runner intercepts. The HTTP response returns immediately with whatever streamed so
far. The graph is frozen in DynamoDB, waiting.

```python
from langgraph.types import interrupt, Command

def submit_plan(plan: Plan, thread_id: str) -> str:
    # Pause here. The graph suspends. The HTTP request finishes.
    human_decision = interrupt({"kind": "plan_approval", "plan": plan.model_dump()})
    # Everything above re-runs on every resume (see below). Below runs only after the human replies.
    return human_decision
```

The frontend renders the plan, the user clicks approve or requests changes, and sends a second
request:

```python
Command(resume={"type": "approve"})
# or
Command(resume={"type": "refine", "feedback": "add a step for competitive analysis"})
```

LangGraph finds the paused graph in the checkpoint, re-runs the node from the top, and delivers the
`Command` as `interrupt()`'s return value. **This is the entire mechanism.** Two requests, one
checkpoint, one `interrupt()` call. Everything else — the refine loop, the modification limit, the
approval gate — is built on top.

### The idempotency rule you cannot ignore

When a graph resumes, LangGraph **re-runs the current node from the beginning** — not from the
`interrupt()` call. Any side effect *before* `interrupt()` happens on every resume:

```python
def submit_plan(plan, thread_id):
    send_slack_notification(thread_id, plan)   # ← runs on EVERY resume
    return interrupt({...})
```

Refine the plan twice and that Slack notification fires three times. The fix: move side effects
*after* `interrupt()`, or make them idempotent.

### Designing the interrupt payload

The payload is the contract between backend and frontend — changing it later is a frontend
migration. It needs two things: **everything the human needs to decide** (plan steps, scope,
entities), and **everything the code needs to validate the decision** on resume:

```python
interrupt({
    "kind": "plan_approval",
    "plan_id": plan.plan_id,             # validate against the incoming approval
    "thread_id": thread_id,              # which thread
    "owner_id": owner_id,                # who originally proposed this
    "plan": plan.model_dump(),           # the human-readable content
    "interrupt_id": str(uuid.uuid4()),   # uniquely identify this interrupt
})
```

On the approve endpoint, read the payload back from the checkpointer *before* resuming:

```python
gate = read_gate_state(thread_id)   # reads interrupt payload from DynamoDB directly
if gate["plan_id"] != payload.plan_id:      raise HTTPException(409)  # stale tab
if gate["owner_id"] != requesting_user:      raise HTTPException(403)  # wrong user
# ...then Command(resume=...). If validation fails, the graph stays paused — no double execution.
```

### Adding the refine loop — and the design that DOESN'T work

The tempting design is a `while True` inside the gate tool:

```python
# DON'T do this
def submit_plan(plan: Plan) -> str:
    while True:
        decision = interrupt({"plan": plan.model_dump(), ...})
        if decision["type"] == "approve":
            plan.status = "locked"
            return "approved"
        plan = call_llm_to_update_plan(plan, decision["feedback"])   # ← the problem
        continue
```

**The problem is the marked line.** Applying "add a competitive analysis step" means interpreting
prose and editing research steps — LLM work. But you're inside a tool: plain Python, mid-way
through the agent's own graph. To regenerate the plan you'd make a *separate, out-of-band* LLM call
— a second brain that doesn't share the agent's system prompt, tools, or conversation context. Now
two different models author the plan; formats drift, context is lost, and the thing the human
approves was never seen whole by the agent that will execute it.

**The structural fix:** the tool never applies feedback — it hands the feedback back to the agent
as its tool result, and the loop becomes the agent loop itself. Each `submit_plan` call interrupts
exactly once and returns a `Command` (see Part 2, Step 5).

```
agent: writes plan steps (write_todos)
agent: submit_plan → interrupt → human: "add GERMANY too"
       ToolMessage: "user requested changes: add GERMANY too. Update the todo list, then call
                     submit_plan again."
agent: edits its steps (write_todos)        ← the AGENT interprets the feedback
agent: submit_plan → a NEW interrupt → human: approve
       ToolMessage: "Plan approved and locked. Execute now."
agent: executes
```

Each refine round is one full trip: a new tool call, a new `interrupt()`, a new HTTP request. **The
tool stays dumb; the agent stays the sole author of the plan.**

### Is the prompt or the code doing the magic? (the division of responsibility)

| Responsibility | LLM + Prompt | Code |
|---|---|---|
| Generate plan steps | ✓ | |
| Assign data source per step (tags in todo content) | ✓ | |
| Parse routing tags into typed fields | | ✓ |
| Resolve entities before routing (prompt-enforced order) | ✓ | |
| Interpret refinement feedback | ✓ | |
| Tell the model what to do after approve/refine (ToolMessage) | | ✓ |
| Decide which research tools to call | ✓ | |
| Synthesize final answer | ✓ | |
| Ground answer in tool results only | ✓ (prompt rule) | |
| Restrict available tools to the contract set (allow-list middleware) | | ✓ |
| Route approve vs. refine decision | | ✓ |
| Enforce plan status transitions | | ✓ |
| Count and limit modifications | | ✓ |
| Validate `plan_id` / `interrupt_id` | | ✓ |
| Prevent concurrent execution | | ✓ |

**The clean mental model: the LLM generates content; the code manages the state machine.**

### The state machine under the plan

```
DRAFT ──(interrupt: human sees plan)──► DRAFT (modification_count++)   [refine]
      └──(approve)──► LOCKED ──► EXECUTING ──► COMPLETE
```

Each state has invariants:

- **DRAFT** — can be modified; cannot be executed.
- **LOCKED** — cannot be modified; can be executed; `modification_count` stops incrementing.
- **EXECUTING** — concurrent execution blocked by the DynamoDB claim.
- **COMPLETE** — terminal; checkpoint is final.

**The LLM only ever writes plan content in DRAFT. Every other transition is code.** If you conflate
these — letting the LLM modify a locked plan, or not enforcing the lock — you get **Bug 1** (the
entity-override bug): the model re-resolves, mid-execution, entities the human already committed
to, because nothing stopped it.

### Implementation path for an intern

Each step is runnable and testable before the next:

1. **Plan generation only.** Write `submit_plan` that takes a `Plan` and immediately returns it. No
   interrupt, no approval. Get the LLM to generate a reasonable plan and the graph to run to
   completion. Validate the plan content for several inputs.
2. **Add `interrupt()`.** Put `interrupt(plan.model_dump())` inside `submit_plan`. The graph now
   pauses. Resume from a test: `graph.invoke(Command(resume={"type": "approve"}))`. Verify the
   graph continues and the plan is accessible after resume.
3. **Add a second HTTP endpoint.** Build the approve endpoint. Have it call
   `read_pending_interrupt(thread_id)` (reads the checkpoint's pending writes — LangGraph provides
   this), validate the payload, then `graph.astream_events(Command(resume={"type": "approve"}),
   config)`. Test with two real HTTP requests.
4. **Add the refine path.** On a refine decision, have `submit_plan` return the feedback to the
   agent as a ToolMessage ("the user requested changes: … update your steps and call `submit_plan`
   again") instead of applying it. Verify the agent edits its plan and calls `submit_plan` again,
   producing a second `interrupt()`. Build the refine endpoint. **Do NOT loop inside the tool or
   call an LLM from inside it — the agent is the sole author of the plan.**
5. **Add validation.** Put `plan_id` and `interrupt_id` in the interrupt payload. Validate them on
   the approve and refine endpoints. Test the stale-tab case (two browser windows, approve from the
   stale one).
6. **Add the concurrency guard.** Write a DynamoDB claim with a conditional `put_item`. Verify that
   two simultaneous approve requests produce exactly one `200` and one `409`.

**The hardest step is 5** — idempotency bugs and stale-state bugs both appear here. Run the approve
endpoint twice on the same payload and verify the second call either returns `409` or is a safe
no-op.

### What makes this hard to get right

- **Idempotency before `interrupt()`.** Any code before `interrupt()` runs on every resume —
  initial proposal plus every refine. Developers who don't know this add notifications, counters,
  or state mutations before `interrupt()` and discover them firing multiple times in production.
- **Resume vs. invocation.** `graph.astream_events(Command(resume=…), config)` *looks* like a
  normal invocation. It isn't — it's a resume. It re-runs the current node from the top and
  delivers the `Command` as the interrupt's return value. Treat it like a fresh invocation (new
  messages list, expecting a start-from-scratch) and you get confusing re-execution.
- **The plan ID in two places.** The frontend sends a `plan_id` in the approve/refine payload; the
  backend has the actual one in the checkpoint. If they diverge (a refine that changed the plan),
  stale-tab detection fires. Keeping the frontend always sending the current `plan_id` requires the
  backend to return it in every interrupt event, and the frontend to track it across renders — a
  frontend contract, not just a backend concern.

---

## Part 4 — Why a special todo tool, and how todos become a Plan

**Why does the agent write a plan by calling `write_todos`, not by outputting a structured Plan
object directly?**

### The dual-purpose insight

`write_todos` is a built-in tool from the underlying agent framework. It manages a list of
`{content, status}` items in the `todos` state channel; a call replaces the full list, and the
agent reads the current list from state on the next invocation. The same data structure serves two
different phases:

- **At plan time:** the agent writes its research steps as todo items — each a natural-language
  description with routing/grouping tags embedded in the content text. *The todos are the plan.*
- **At execution time:** the agent marks each todo `in_progress` when it starts and `completed`
  when the result is back. Same tool, same channel, same items — now a live progress checklist.

If you asked the LLM to produce a plan as a single structured output, you'd need a *second*
mechanism for execution progress, and the two would need to stay in sync. `write_todos` unifies
them: the todo at position 2 is simultaneously the plan step and the progress tracker.

### The read-only Plan projection

**The agent writes todos. It never writes a `Plan`.** The typed `Plan` (step IDs, routing fields,
modification count, status) is always *derived* — rebuilt from the current todos by a pure function
every time it's needed. This is the key architectural decision: **the Plan is a read-only
projection over the agent's own working state.** The agent cannot corrupt the Plan by writing
inconsistent fields — it can only corrupt its own todos, and the projection acts as a sanitizing
layer between raw LLM output and the typed structure the frontend receives.

### What `project_plan` does — five things

**1. Parse routing and grouping tags out of content text.**

```python
# The LLM writes:
#   Query BRAND_A national sales [source: SOURCE_A] [group: 1 parallel]
SOURCE_TAG = re.compile(r"\[\s*source:\s*(\w+)\s*\]", re.IGNORECASE)
GROUP_TAG  = re.compile(r"\[\s*group:\s*(\d+)\s+(parallel|sequential)\s*\]", re.IGNORECASE)
```

Tags are extracted into `PlanStep.assigned_source` and `PlanStep.exec_group` and replaced with a
space in the title. The human-readable title stays clean; the machine-readable routing fields are
typed. A malformed tag fails **soft**: the tag text stays in the title (visible to the human at
approval time) and `assigned_source` is `None` (the executor knows to skip it).

**2. Map status strings.** deepagents uses `pending`/`in_progress`/`completed`; the frontend
contract uses `pending`/`active`/`done`. The projection translates.

**3. Assign stable step IDs across refinements** (the most complex part). When the human requests
changes and the agent rewrites some steps, the frontend needs to know which steps changed and which
stayed the same. IDs must be stable across reorder/reword. Three passes:

- **Pass 1 — exact title match.** A step whose content didn't change keeps its ID even if it moved.
- **Pass 2 — ordinal match.** A step whose content changed but is at the same position gets the
  prior step's ID at that position. Covers rewording.
- **Pass 3 — fresh IDs.** New steps get IDs that continue numbering past the highest the plan has
  ever held. **IDs are never reused** — if `step-3` is deleted and a step added, the new step gets
  `step-4`, never `step-3`.

```
Initial:      step-1 (query sales), step-2 (query market share), step-3 (synthesize)
After refine: step-1 (query sales), step-3 (synthesize, moved to pos 2), step-4 (new: competitive)
  Pass 1: step-3 matches by title → keeps step-3.   Pass 3: new step gets step-4, not step-2.
```

**4. Derive `plan_id` deterministically from `thread_id`.** First plan on a thread →
`plan-{thread_id}`. A new question on the same thread (after the prior locks) → `plan-{thread_id}-2`,
then `-3`. Deterministic because `submit_plan` re-runs on every resume — a random `plan_id` would
change per re-run and the stale-tab validation would spuriously reject valid resumes.

**5. Carry gate-owned fields from `prior`.** `status` and `modification_count` are not in the
todos; the projection carries them through from the prior projection. The gate increments
`modification_count` and sets `status = "locked"` via `model_copy`, never by touching todos. If
`prior.status == "locked"`, the projection detects a new question cycle and resets: `prior = None`,
new `plan_id`, `modification_count = 0`. The locked plan stays in state (it's the checkpoint) but
carries no fields forward.

### Two callers of the same projection

`project_plan` is called in two places:

- **The gate (`submit_plan`)** builds the Plan that goes into the interrupt payload and the
  checkpoint. This is the *authoritative* Plan — what the resume endpoints validate against. It
  reads from `runtime.state.get("todos")`.
- **The event curator** calls it on every `write_todos` event to produce a plan frame for the SSE
  stream. This is the *streaming-time* projection — it enables real-time plan rendering as the
  agent builds the plan step by step.

```python
# curator._on_todos_end: fires on every write_todos call
def _on_todos_end(self, data):
    todos = self._tool_input_dict(data).get("todos")   # reads from the EVENT, not state
    plan = project_plan(todos, thread_id=self.thread_id, prior=self._plan)
    self._plan = plan                                  # chains within one stream
    revision = "proposed" if prior is None else "refined"
    return self.emitter.emit_plan(plan.model_dump(), revision=revision)
```

The curator reads from the tool's *input* (what the model passed to `write_todos`), not the state
channel — so it can project a Plan and emit a frame *before* `write_todos` even writes to state.
The frontend sees the plan build step by step. The curator maintains its own `_plan` prior that
chains within a single stream, but re-syncs to the gate's authoritative Plan after `submit_plan`
completes (the `Command` carries the final Plan). This keeps the two projections consistent: the
streaming view and the checkpoint-authoritative view converge at every gate call. The projection is
computed twice — once for streaming, once for checkpointing — from the same pure function.

---

## Part 5 — Two more structural decisions

### The tool allow-list: structural contract instead of prose

deepagents injects several built-in tools into every agent by default: `write_todos` (planning),
`ls`/`read_file` (filesystem), and `task` (spawn sub-agents). **The sub-agent tool is particularly
dangerous** for this workflow: if the orchestrator delegates to a sub-agent, that sub-agent's
retrieval calls run *outside* the approval gate and *outside* the event curator that manages plan
state.

The naive fix is prose in the prompt ("Do not use `task`, `ls`, or `read_file`"). This is fragile —
prompt instructions compete with the base prompt the framework appends, and models occasionally use
what's available even when told not to. The structural fix intercepts the model request before it
reaches the LLM and removes the disallowed tools:

```python
class _AllowedToolsMiddleware(AgentMiddleware):
    def __init__(self, *, allowed: frozenset[str]) -> None:
        self._allowed = allowed

    def _filter(self, request):
        tools = [t for t in request.tools if t.name in self._allowed]
        return request.override(tools=tools)   # the model never sees tools it shouldn't call

    def wrap_model_call(self, request, handler):
        return handler(self._filter(request))
```

The orchestrator is allowed exactly three tools: `write_todos`, `submit_plan`, `query_source`. The
framework can inject any number of others — the middleware makes them invisible. **The contract is
structural; the prompt doesn't mention it at all.** This pattern generalizes: whenever the LLM
should never call a tool in a given context, removing it from the request is more reliable than
prompting against it.

### The ToolMessage as the continuation signal

After the human decision, the gate needs to tell the LLM what to do next. Two options: put it in
the system prompt (Option A), or return it as the tool result of `submit_plan` (Option B).

**We use Option B.** The tool result has higher salience than the system prompt for
what-to-do-next reasoning — the model just completed a tool call and is deciding what to call next;
the return value is immediately in context.

```python
# On approve:
ToolMessage("Plan approved and locked. Execute the research steps now, exactly as planned.")
# On refine:
ToolMessage(f"The user requested changes to the plan: {feedback}\n"
            "Update the todo list with write_todos to reflect this feedback, then call "
            "submit_plan again. Do not start any retrieval yet.")
```

The instruction is immediate, unambiguous, and appears at the exact point of decision. "Do not
start any retrieval yet" in the refine result exists because, without it, models occasionally call
`query_source` between the refine ToolMessage and the next `submit_plan` — executing against a plan
that isn't approved yet.

### Routing encoded in plan content, not a separate routing step

**When does the agent decide which source to query?** Two options:

- **Option A — route at execution time.** The agent generates a generic plan ("query sales data"),
  then a routing function picks the source at execution. The human approves a plan with *no* source
  assignments.
- **Option B — route at plan time.** The agent assigns a source to every retrieval step before
  presenting the plan. The human approves a plan that already specifies which source each step
  hits.

**We chose Option B.** The human needs to see the routing to meaningfully approve. "Query sales
data" is vague; "query subnational account-level sales (SOURCE_B)" is reviewable. If routing
happens after approval, the human approved a plan they couldn't actually evaluate. To make routing
machine-readable without a separate schema field, we encoded it as content tags the LLM appends to
every todo — the two regexes in `project_plan` (Part 4) pull them into typed fields. The LLM writes
natural language; the code extracts structure. The plan the human approves is simultaneously
human-readable and a fully-specified execution DAG.

---

## Part 6 — The grounding rules, one more time (the universal principle)

> Ground the final report ONLY in `query_source` results. If a call returns `success=false`, mark
> that step completed and state plainly in the report that this step's retrieval failed — never
> fill the gap from your own knowledge.

This is domain-specific but the principle is universal for any agent querying authoritative data:
**LLMs will fill gaps with plausible-sounding numbers.** In a general-purpose research tool that
might be acceptable; in pharmaceutical commercial analytics, a model-fabricated sales figure that
looks like a real data point is a compliance problem.

The grounding rule lives in the *prompt*, not code, because detecting whether a number came from a
tool result or from model knowledge is not tractable at inference time. The enforcement surface is
the prompt; the secondary defense is logging every `query_source` call and its result, so a
post-hoc audit can catch reports that cite figures not present in any tool result.
