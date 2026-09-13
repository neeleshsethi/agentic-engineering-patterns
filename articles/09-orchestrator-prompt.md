# Step 8 · Orchestrator Prompt

Once an agent's control flow lives in code — a gate, a state machine, a set of endpoints — a question sharpens: what is the prompt still *for*? The answer is a dividing line worth stating up front.

> The prompt shapes judgment. The code enforces boundaries.

The prompt decides what the model should *think about* — how to break a question into steps, how to interpret feedback, what counts as evidence. The code decides what the model is *allowed to do* — which tools exist, when execution may start, who may approve. Every time you find yourself writing a prompt sentence that tries to guarantee a boundary, you have found something that belongs in code instead.

The running example is a pharmaceutical commercial-analytics orchestrator. Identifiers are generic (`BRAND_A`, `SOURCE_A..D`, `query_source`), but the domain matters here: in regulated analytics, some of these rules are law, not style, and that changes where they have to live.

> Terms used below — [gate](00-glossary.md#gate), [middleware](00-glossary.md#middleware), [ToolMessage](00-glossary.md#toolmessage), [grounding](00-glossary.md#grounding), [entity resolution](00-glossary.md#entity-resolution) — are in the [glossary](00-glossary.md). This article builds on [Human-in-the-Loop Approval](04-planning-and-human-approval.md).

## The Test: Prompt Or Structure?

For any rule the agent must follow, ask one question: **can code enforce it deterministically?**

- If yes, put it in code and spend zero prompt tokens on it. Prompt instructions compete with the model's other instincts and occasionally lose. Structure does not.
- If no — because enforcement would require reading the model's mind — the prompt is the only surface available, and the sentence has to be exact.

The rest of this article is that test applied to a real orchestrator, in both directions.

## What The Code Enforces (Not The Prompt)

Start with the boundaries, because they are the easy calls. None of these appear in the prompt at all.

**Tool availability.** The agent framework injects several built-in tools by default, including one that spawns sub-agents. A sub-agent would run its retrievals *outside* the approval gate — exactly the thing the whole design exists to prevent. The naive fix is a prompt line: "do not use the sub-agent tool." That is fragile. The structural fix removes the tool from the request before the model ever sees it:

```python
class AllowedToolsMiddleware(AgentMiddleware):
    def __init__(self, *, allowed: frozenset[str]) -> None:
        self._allowed = allowed

    def _filter(self, request):
        tools = [t for t in request.tools if t.name in self._allowed]
        return request.override(tools=tools)   # the model never sees the rest

    def wrap_model_call(self, request, handler):
        return handler(self._filter(request))
```

In the teaching examples, we often collapse retrieval behind one generic `query_source` tool. The production orchestrator had a larger but still fixed allow-list:

```text
write_todos
submit_plan
query_cortex
query_iqvia_gmi
run_analysis
rebuild_artifacts
read_file
```

The framework can inject any number of others; the middleware makes them invisible. The rule is a contract, not a request. Retrieval tools are plan-gated, synthesis tools work over already-held evidence, and `read_file` is guarded for playbook files only. Analyst or sub-agent tools are not directly reachable by the orchestrator.

**Gate sequencing, status, concurrency.** Whether the plan is approved, when it locks, how many times it was refined, whether two requests raced — all of that is code (the gate's `interrupt()`, `model_copy` on the plan status, a DynamoDB claim). See [Human-in-the-Loop Plan Approval](./04-planning-and-human-approval.md) for the mechanism. Here the point is only that these were *not* delegated to the prompt.

| Rule | Where it lives | Why not the prompt |
|------|----------------|--------------------|
| "Don't call the sub-agent / unguarded filesystem tools" | Allow-list middleware + read guard | Removed from the request, or fenced before execution |
| "Only proceed if the plan is approved" | The gate's `interrupt()` | Structural pause; the model cannot continue past it |
| Plan status transitions, modification count | Gate-owned fields (`model_copy`) | Not in the todos, so the projection cannot produce them from model output |
| `plan_id` / owner validation, one-resume-at-a-time | Endpoint preconditions + DynamoDB claim | Concurrency is not observable to a single model call |

## What The Prompt Shapes (Not The Code)

Now the other side: the judgments no code can make. These are where prompt wording is load-bearing, and where a wrong word ships a bug.

### The phase machine

The workflow is written as five numbered, labeled steps — PLAN, SUBMIT, REFINE, EXECUTE, FOLLOW-UP — not as prose. The numbering is not decoration. It gives the model a phase machine it can locate itself in: at any point in a long conversation it matches its situation ("I just received refine feedback") to a step and reads off what to do.

```text
Workflow: plan, get approval, then execute.
  1. PLAN       break the question into steps, record them with write_todos
  2. SUBMIT     call submit_plan; never call retrieval before approval
  3. REFINE     update the todos, then call submit_plan again
  4. EXECUTE    once locked, run the steps; entities are FINAL
  5. FOLLOW-UP  a new question starts a fresh cycle from step 1
```

Unnumbered prose loses this — models skip phases when the phases have no names. Observed without it: the model calls `query_source` before approval, or re-executes the old plan after feedback instead of updating the todos first.

### Ordering the model's reasoning

Some rules the code physically cannot impose because there is no code between the model's thoughts. Evaluation order is one.

> Resolve the entities FIRST — fill any missing country or brand from context — BEFORE deciding routing and BEFORE asking any clarification.

Routing rules are entity-scoped: whether `SOURCE_A` or `SOURCE_B` serves a "sales" question depends on the brand and country. Route before resolving entities and you route to a source that is invalid for the entity you later settle on. No code sits between "the model reads the question" and "the model picks a source," so the prompt has to fix the order.

### The priority stack

> Entities named explicitly in the question always win over the defaults in the context block.

Without this line, a user whose default market is `AUSTRALIA` asking for `GERMANY` sales occasionally got an `AUSTRALIA` plan, because the injected default sat at higher salience than the question's own words. The code never sees the defaults — the context block is injected into the model request by middleware and never checkpointed — so only the model can apply the precedence, and only if told to.

## The Case That Has To Be Both

The sharpest example is a rule that is enforced by *neither* side cleanly, and shows exactly why the dividing line exists.

During execution, the middleware still injects the user's default context ("defaults: AUSTRALIA") on every model call — it does not know what phase the run is in. But the human already approved a plan that resolved the market as `GERMANY`. So the prompt carries the invariant:

> The locked plan's entities are FINAL. Never re-resolve entities from the context block during execution.

There is no code enforcement possible here. Detecting "the model re-resolved an entity mid-execution" would require reading its intent. The prompt is the *only* thing standing between the injected default and a wrong-market report. When this line was missing, the model re-resolved `GERMANY` back to `AUSTRALIA` mid-run and produced a confident report for the wrong country — a silent failure that looked completely normal in the logs. (This is Bug 1 in the companion source notes.)

The lesson is not "prompts are unreliable." It is: **know which invariants have no structural enforcement, and treat those prompt lines as load-bearing, because nothing else is holding the weight.**

## Grounding: The Purely-Prompt Invariant

The clearest case of a prompt-only rule is grounding, and it is where the domain earns its keep.

> Ground the final report ONLY in approved tool results. If a retrieval call fails, mark the step completed and state plainly in the report that retrieval failed. Never state, estimate, or approximate a figure not present in a tool result. If every retrieval fails, report that and stop.

Why this cannot be code: deciding whether a number in the report came from a tool result or from the model's own memory is not tractable at inference time. The enforcement surface is the prompt; the only backstop is logging every `query_source` call so a post-hoc audit can catch fabricated figures.

Why it matters *here* specifically: this rule is a deliberate counterweight to the base agent prompt appended after it, which says "keep iterating until the task is done." Combined with a failed retrieval, "keep iterating" reads to a model as "find another way to get the number" — and the model's other way is its training data. In a general research tool a plausible guess might be acceptable. In pharmaceutical commercial analytics, a model-fabricated market share sitting next to real retrieved figures is indistinguishable from data. That is a compliance problem, not a quality problem.

Two wording details, each scar tissue from a real failure:

- **"Never state, estimate, or approximate"** — three verbs because the model negotiates. Told not to *state* figures, it estimates ("approximately 40%"); told not to estimate, it gives a range. Enumerating the verbs closes the ladder.
- **"Mark the step completed"** — because the model invented a `failed` status the status map did not recognize, which silently coerced the step back to `pending`, making a finished-and-failed step look un-started. A failed retrieval is a *completed* step whose outcome is reported in prose.

## Deliver The Next Instruction As A Tool Result

One structural choice supports the prompt: how the model learns what to do after a human decision. You could put it in the system prompt ("after approval, execute the steps"). Better is to return it as the tool result of `submit_plan`.

```python
# on approve
ToolMessage("Plan approved and locked. Execute the research steps now, exactly as planned.")
# on refine
ToolMessage("The user requested changes: ...\n"
            "Update the todo list, then call submit_plan again. Do not retrieve anything yet.")
```

A tool result has higher salience than the system prompt for what-to-do-next reasoning: the model just finished a tool call and is deciding what to call next, and the return value is right there in context. The system prompt's REFINE and EXECUTE steps *pre-explain* both messages, so when one arrives the model recognizes it as "I am now in step 3" or "step 4." Prompt and code cooperate: the code routes the human's decision into one of two instructions; the prompt taught the model what each one means.

## A Versioning Note

Ship the prompt with a version string (`PROMPT_VERSION = "2.2"`) carried as trace metadata, so you can tell which prompt produced a given run. The convention: bump once per shipped change, never per edit inside an unmerged branch. The version answers "which prompt produced this trace," not "which local draft."

## Guardrails

- For every rule, ask whether code can enforce it deterministically; if yes, it is not a prompt's job
- Remove forbidden tools from the request instead of forbidding them in prose
- Keep gate sequencing, status transitions, and concurrency entirely in code
- Reserve the prompt for judgment and for invariants with no structural enforcement
- Treat those unenforceable prompt lines as load-bearing — they are the only thing holding the invariant
- Enumerate the exact misbehaviors when the model negotiates around a rule
- Number and name workflow phases so the model can locate itself
- Deliver post-decision instructions as tool results, not only as system-prompt text

---

!!! check "You should now understand"
    - Why prompts should shape judgment but not enforce deterministic boundaries
    - Why forbidden tools should be removed from the request instead of prohibited in prose
    - Why a public `query_source` example may stand for several production retrieval tools
    - Why workflow phases should be numbered and named
    - Why grounding is a prompt-only invariant unless you add provenance and audit
    - Why post-approval instructions are more salient as tool results than buried prompt text

??? question "Try this"
    **You need the agent to never call retrieval tools before plan approval. Should that rule live in the prompt or code?**

    ??? success "Answer"
        Code. The approval gate should structurally pause execution, and middleware should remove tools that are not allowed in the current phase. The prompt can explain the workflow, but it should not be the only thing preventing an expensive or unauthorized tool call.

*Next: [Capstone · Nine Silent Failures](10-nine-silent-failures.md) — now use the real case study as a review exam for the whole system.*
