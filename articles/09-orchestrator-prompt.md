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

```text
                    ┌─────────────────────────────────────┐
                    │  Can code enforce this deterministically?  │
                    └──────────────┬──────────────────────┘
                                   │
               ┌───────────────────┴────────────────────┐
               │ YES                                     │ NO
               ▼                                         ▼
  ┌─────────────────────────┐             ┌─────────────────────────┐
  │       USE CODE          │             │      USE THE PROMPT      │
  │                         │             │                         │
  │  Tool allow-list        │             │  Phase machine          │
  │  Gate (interrupt())     │             │  Evaluation order       │
  │  Plan status machine    │             │  Priority stack         │
  │  Concurrency claim      │             │  Grounding invariant    │
  │  Owner validation       │             │  What counts as done    │
  └─────────────────────────┘             └─────────────────────────┘
  Code never lies. It either                Prompt shapes judgment.
  runs or it doesn't.                       It can be overridden.
```

The rest of this article is that test applied to a real orchestrator, in both directions.

## What The Code Enforces (Not The Prompt)

Start with the boundaries, because they are the easy calls. None of these appear in the prompt at all.

**Tool availability.** The agent framework injects several built-in tools by default, including one that spawns sub-agents. A sub-agent would run its retrievals *outside* the approval gate — exactly the thing the whole design exists to prevent.

```text
❌ WRONG — using the prompt to forbid a tool:

  System prompt: "Do not use the spawn_subagent tool."

  Model sees tools: [write_todos, submit_plan, query_source, spawn_subagent, ...]
                                                              ↑
                                                    tool is visible
                                                    model sometimes calls it anyway
                                                    (prompts compete with instincts)
```

```text
✅ RIGHT — removing the tool before the model sees it:

  Middleware filters request:
    IN:  [write_todos, submit_plan, query_source, spawn_subagent, read_filesystem, ...]
    OUT: [write_todos, submit_plan, query_source]
                                                  ↑
                                        spawn_subagent is gone
                                        model cannot call what it cannot see
                                        this is a guarantee, not a request
```

```python
class AllowedToolsMiddleware(AgentMiddleware):
    def _filter(self, request):
        tools = [t for t in request.tools if t.name in self._allowed]
        return request.override(tools=tools)   # the model never sees the rest
```

The production orchestrator allowed exactly seven tools — the rest were invisible:

```text
write_todos      ← planning only (before approval)
submit_plan      ← the gate tool
query_cortex     ← retrieval (plan-gated, only after approval)
query_iqvia_gmi  ← retrieval (plan-gated, only after approval)
run_analysis     ← synthesis over already-held data
rebuild_artifacts← re-render charts from existing evidence
read_file        ← playbooks only, path-guarded by middleware
```

The framework can inject any number of others; the middleware makes them invisible.

In the production deep graph, seven middlewares formed the runtime contract around those tools:

| Order | Middleware | Job |
|---:|---|---|
| 1 | `UserContextMiddleware` | Inject profile, persona, and prior entity context into model calls. |
| 2 | `DataManifestMiddleware` | Render the held-data inventory so the model can see reusable evidence. |
| 3 | `AllowedToolsMiddleware` | Restrict the visible tools to the contract set. |
| 4 | `PlaybookReadGuard` | Refuse `read_file` outside the approved playbook area before the backend sees it. |
| 5 | `DeepExitPathMiddleware` | Generate the report and assembly outputs when there is unreported evidence. |
| 6 | `SufficiencyGateMiddleware` | Check completeness and allow one bounded gap-fill round before reporting. |
| 7 | `RetrievalBudgetMiddleware` | Meter retrieval rounds and route to exit when the budget is spent. |

The order is not decorative. LangChain runs `after_agent` hooks in reverse registration order, so the sufficiency gate must run before the exit path writes the report. The retrieval budget uses model-call hooks, so it can meter loop depth without perturbing that exit ordering.

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
┌──────────────────────────────────────────────────────────┐
│           ORCHESTRATOR PHASE MACHINE                     │
│                                                          │
│  ① PLAN      User asks a question                        │
│      │       Model breaks it into steps with write_todos  │
│      ▼                                                   │
│  ② SUBMIT    Model calls submit_plan                     │
│      │       Graph pauses — human sees the plan          │
│      ▼                                                   │
│  ③ REFINE   Human says "add X" → feedback arrives        │
│  (loop)      Model updates todos, calls submit_plan again │
│      │       ↑ repeats until human approves              │
│      ▼                                                   │
│  ④ EXECUTE   Plan is LOCKED — entities are FINAL         │
│              Model runs retrieval tools, writes report   │
│      ▼                                                   │
│  ⑤ FOLLOW-UP New question → back to ① (fresh cycle)     │
└──────────────────────────────────────────────────────────┘

⚠ Without numbered phases, the model loses track:
   - calls query_source at ② instead of waiting for ④
   - re-runs old plan at ③ instead of updating todos first
```

Unnumbered prose loses this — models skip phases when the phases have no names.

### Ordering the model's reasoning

Some rules the code physically cannot impose because there is no code between the model's thoughts. Evaluation order is one.

> Resolve the entities FIRST — fill any missing country or brand from context — BEFORE deciding routing and BEFORE asking any clarification.

```text
❌ WRONG ORDER — route first, resolve entities later:

  Question: "Give me my sales"

  Step 1: Pick source
    Model picks SOURCE_A (handles "sales" questions)

  Step 2: Resolve entities
    "my" → user default is JAPAN
    SOURCE_A does not cover JAPAN

  Result: plan routes to a source that cannot serve the entity
          → retrieval fails, or wrong data returned

──────────────────────────────────────────────────────────────

✅ RIGHT ORDER — resolve entities first, then route:

  Question: "Give me my sales"

  Step 1: Resolve entities
    "my" → user default is JAPAN, brand = PAXLOVID

  Step 2: Pick source
    JAPAN + PAXLOVID → SOURCE_B handles this combination

  Result: correct source chosen for the correct entity ✓
```

No code sits between "the model reads the question" and "the model picks a source." The prompt is the only place to fix this ordering.

### The priority stack

> Entities named explicitly in the question always win over the defaults in the context block.

```text
What the model actually sees (one flat prompt):

  ┌─────────────────────────────────────────┐  ← HIGH salience (top of prompt)
  │  [User context]                         │
  │  User defaults: AUSTRALIA / BRAND_A     │  ← injected by middleware
  │  [/User context]                        │
  │                                         │
  │  Give me GERMANY sales for BRAND_B      │  ← user's actual question
  └─────────────────────────────────────────┘  ← LOWER salience (bottom)

❌ WITHOUT priority rule:
   Model sees AUSTRALIA at high salience → plans for AUSTRALIA
   User asked for GERMANY → wrong country, 200 OK

✅ WITH priority rule in prompt:
   "Entities named in the question always override defaults"
   Model reads GERMANY in the question → applies priority rule → plans for GERMANY
```

The code never sees the defaults — the context block is injected into the model request by middleware and never checkpointed — so only the model can apply the precedence, and only if told to.

## The Case That Has To Be Both

The sharpest example is a rule that is enforced by *neither* side cleanly. During execution, middleware still injects defaults on every model call — it does not know what phase the run is in. But the human already approved a plan with specific entities.

Quick vocabulary check:

- **Raw question** is only what the user typed.
- **Model input** is the full bundle the LLM receives after code adds context, history, tool results, and the raw question.

<div class="scrubber" data-scrubber markdown="0">
  <div class="scrubber-stage">
    <div class="scrubber-step">
      <span class="scrubber-time">Planning · default is only a fallback</span>
      <div class="scrubber-caption">Middleware adds the user's default country, Australia. The user's question explicitly asks for Germany, so the model should resolve Germany.</div>
      <pre class="scrubber-item">[User context] defaults: AUSTRALIA [/User context]
"Give me GERMANY sales"

resolved entity = GERMANY   <span class="ok">question wins</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Approval · Germany becomes final</span>
      <div class="scrubber-caption">The human approves the plan. Execution should now follow the locked plan's entities, not re-run entity resolution from scratch.</div>
      <pre class="scrubber-item">locked plan:
  country = GERMANY
  status = approved   <span class="ok">final for this run</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Execution · middleware injects again</span>
      <div class="scrubber-caption">On a later model call, the same context middleware adds Australia again. That default is now stale for this run, but it appears near the top of the prompt.</div>
      <pre class="scrubber-item">top of prompt:
  [User context] defaults: AUSTRALIA [/User context]   <span class="warn">freshly injected</span>

conversation history:
  [tool] Plan approved: GERMANY   <span class="ok">older message</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">Without invariant · wrong report</span>
      <div class="scrubber-caption">If the prompt lets the model re-resolve during execution, it may pick the high-salience Australia default and produce a normal-looking report for the wrong country.</div>
      <pre class="scrubber-item">model re-resolves from context
country = AUSTRALIA

report country = AUSTRALIA   <span class="bad">silent failure</span></pre>
    </div>
    <div class="scrubber-step">
      <span class="scrubber-time">With invariant · correct report</span>
      <div class="scrubber-caption">The prompt line is load-bearing: once the plan is locked, its entities are final. The context block may still be present, but it cannot override the approved plan.</div>
      <pre class="scrubber-item">prompt invariant:
  locked plan entities are FINAL
  never re-resolve from context during execution

report country = GERMANY   <span class="ok">correct</span></pre>
    </div>
  </div>
</div>

Germany did not appear randomly. It entered the run when the user explicitly asked for Germany, then became durable when the human approved a plan whose entities said Germany. The bug is that Australia kept re-entering later as injected default context.

There is no code that can detect "the model re-resolved an entity mid-execution." The prompt is the only thing holding this invariant. When this line was missing, the model produced a confident, well-formatted report for the wrong country.

The lesson: **know which invariants have no structural enforcement, and treat those prompt lines as load-bearing — nothing else is holding the weight.**

## Clarification Is A Control-Flow Event

A clarification is any turn where the system asks the user something instead of answering. The prompt shapes the wording, but the source of the pause decides the machinery:

| Type | When | Detector | User sees |
|---|---|---|---|
| Plan-gate clarification | At plan time | `submit_plan(clarification=...)` on the gate interrupt | Plan card plus one question or assumption |
| Terminal clarification | Before planning | The turn is classified as non-reportable | One question, no plan |
| Source elicitation | Mid-retrieval | A source result is marked `is_elicitation` | The source's question relayed to the user |
| Zero-retrieval guard | At exit | No current-cycle results, or the evidence set was already reported | No report over nothing |

The confusing case is source elicitation. Suppose one source returns promo ROI data while another asks, "retail or non-retail?" Deep does not ship a partial report. `pending_elicitation` is a whole-cycle gate: if any open step is waiting on the user, the report writer skips report generation. The completed source result stays in `source_results`, and the next user reply re-queries only the asking step.

That reply is **not** `Command(resume=...)`. Only the plan approval gate leaves a pending `interrupt()`. A source elicitation turn completes normally after relaying the question. The user's answer enters as a fresh graph invocation on the same `thread_id`, under the same locked `plan_id`; the carried result is reused, the clarified step gets a new `provenance_id`, and the changed evidence set can now report.

This is the boundary between prompt and code again:

- The prompt tells the model to relay the source's question and mention what data is already held.
- The exit middleware enforces "elicitation present means no report" even if the model tries to synthesize.
- The locked plan prevents a second approval loop and keeps carried evidence attached to the same cycle.

## Budgets Meter Depth, Not Breadth

A deep agent needs a real loop bound that fits its execution shape. Counting individual tool calls punishes parallel fan-out, which is exactly what a research agent should use to reduce wall-clock time. The production design instead meters retrieval rounds:

| Bound | Default | Behavior |
|---|---:|---|
| Retrieval rounds per plan cycle | 6 | Gracefully route to the exit path; sufficiency and reporting still run. |
| Graph recursion limit | 50 | Backstop only; hitting it is a terminal graph failure. |
| SQS receives | 3 | Attempts 1-2 can redeliver; attempt 3 is poison-pill handling. |

A round is one model turn that issued one or more retrieval calls. A turn that queries three sources in parallel spends one round, not three. Gap-fill turns from the sufficiency gate spend the same budget, and the budget resets when a genuinely new question opens a new plan cycle.

## Grounding: The Purely-Prompt Invariant

The clearest case of a prompt-only rule is grounding — making sure every number in the report came from a real tool call, not from the model's memory.

```text
❌ WITHOUT grounding rule:

  Step 2: query IQVIA for PAXLOVID market share
    → API timeout, retrieval fails

  Base prompt says: "keep iterating until the task is done"
  Model reads this as: "find another way to get the number"
  Model's other way: its training data

  Report includes:
  "PAXLOVID market share in Q3 2024: approximately 34%"
                                      ↑
                              fabricated from training data
                              looks identical to real data
                              in a regulated domain = compliance violation

──────────────────────────────────────────────────────────────

✅ WITH grounding rule:

  "Ground the report ONLY in approved tool results.
   If retrieval fails, mark the step completed and state
   that retrieval failed. Never estimate or approximate."

  Step 2: query IQVIA → fails

  Report includes:
  "PAXLOVID market share: retrieval failed for this step.
   Data not available in this report."
                          ↑
                    honest, auditable, compliant
```

Why this cannot be code: there is no way to detect at inference time whether a specific sentence in the model's output came from a tool result or from its training data. The enforcement surface is the prompt only.

Two wording details, each scar tissue from a real failure:

```text
"Never state, estimate, OR approximate"
         ↑        ↑           ↑
   Three verbs because the model negotiates one verb at a time:
   - told not to STATE → it estimates ("approximately 34%")
   - told not to estimate → it gives a range ("between 30-40%")
   - all three blocked → it stops
```

```text
"Mark the step COMPLETED" (not "failed")
         ↑
   The model invented a "failed" status the status map didn't recognize
   → silently coerced back to "pending"
   → finished-and-failed step looked un-started
   → executor retried it forever

   A failed retrieval IS a completed step. The outcome is prose, not data.
```

## Deliver The Next Instruction As A Tool Result

After the human approves or refines, the model needs to know what to do next. There are two places to put that instruction:

```text
OPTION A — system prompt only:

  System prompt: "Step 4: once locked, execute the research steps."
                          ↑
                  written at the TOP of context,
                  far from the model's current position
                  (the model just finished a tool call)

  Model's attention is on the most recent messages.
  System prompt instruction gets "forgotten" in long conversations.
  Model sometimes re-enters planning instead of executing. ✗

──────────────────────────────────────────────────────────────

OPTION B — ToolMessage (RIGHT next to where the model is):

  [system prompt] "Step 4: once locked, execute..."  ← pre-explains the concept

  ... many tool messages and model turns later ...

  [ToolMessage from submit_plan]
  "Plan approved and locked. Execute the research steps now,
   exactly as planned."                              ← immediate, high salience ✓

  Model is deciding what to call next.
  The ToolMessage return value is the last thing it read.
  It executes. ✓
```

System prompt and ToolMessage work together: the system prompt *teaches* the model what each message means ("step 4 = execute"), and the ToolMessage *triggers* it at the right moment with high salience.

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
