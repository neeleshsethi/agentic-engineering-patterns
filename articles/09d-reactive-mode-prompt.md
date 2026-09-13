# Deep Dive · Reactive Mode And Its Prompt

Reactive mode is the contrast case to deep mode. It does not run a plan-gate loop, does not pause for approval, and does not iterate through long research steps. It is a one-pass router.

```text
decide -> call at most the needed data tool once -> answer
```

## Prompt Shape

The reactive system prompt is assembled as one template with several layers in a fixed order:

| Prompt layer | Job |
|---|---|
| Role and one-pass mandate | Tell the model this is not a deep research loop. |
| Persona context | Inject the user's profile/persona. |
| Tool decision rule | Decide whether to use carried data, refresh, query, clarify, or rebuild artifacts. |
| Source routing guidance | Show the source catalog and routing cards. |
| User context | Provide default brand, country, access pairs, and market rules. |
| Conversation and memory context | Add prior messages, memory, glossary, and business rules. |
| Shared rule blocks | Entity resolution, coreference, business defaults, clarification, source routing, parallel-vs-chain, carryover reuse, elicitation relay, rebuild. |
| Action branches | Choose exactly one: data query, direct conversation answer, terminal clarification, or presentation rebuild. |
| After-tool rules | Synthesize returned prose/tables; retry only within the per-turn budget. |
| Fidelity rules | Preserve tables, caveats, footnotes, and source details. |
| Final answer schema | Return the required JSON response shape. |

The important design point is that reactive mode spends prompt text on judgment because there is no plan gate to hold the model inside a multi-step workflow.

## Tool Decision Rule

The reactive prompt's first job on data questions is deciding whether to call a tool.

| Situation | Prompt instruction |
|---|---|
| Explicit refresh | Always query. |
| No carried data | Query. |
| Carried data fully covers the ask and is fresh enough | Answer from carried data and declare its age. |
| Carried data partially covers the ask | Query for the full ask this turn. |
| Missing entity or metric has no safe default | Ask one clarification. |
| User wants a presentation-only change | Rebuild artifacts once. |

That makes reactive fast, but it also means correctness depends heavily on the prompt's branch ordering.

## Clarification In Reactive

Clarification in reactive is mostly prompt-led. The shared `clarification` rule block handles entity or metric gaps, and the `elicitation_relay` rule block tells the model to surface a source's question instead of answering over it.

Reactive has one narrow code guard:

```text
ClarificationGuardMiddleware
  -> if the previous turn already asked a clarification
  -> inject a default-and-declare instruction
```

That middleware enforces "never two clarification turns in a row." It does not decide whether the current turn should clarify. The prompt and model still decide that.

## Source Elicitation In Reactive

Source elicitation detection can still be deterministic before reactive sees the result. A source adapter can stamp `is_elicitation`, or a deterministic parser can classify `input-required`.

The difference is what happens next:

| Mode | Report boundary |
|---|---|
| Reactive | The prompt tells the model to relay the source question. |
| Deep | `pending_elicitation()` blocks report generation in code. |

Reactive can relay an elicitation correctly, but it depends more on prompt obedience. Deep has a deterministic report gate.

## Deep Versus Reactive

| Dimension | Reactive | Deep |
|---|---|---|
| Workflow | One pass | Plan, approve, execute |
| Approval | None | Human gate via `interrupt()` |
| Retrieval loop | Bounded quick turn | Bounded research cycle |
| Clarification decision | Prompt-led, with a narrow guard | Distributed across gate, source clients, provenance, sufficiency, and exit path |
| Source elicitation report block | Prompt relay | Code-enforced `pending_elicitation()` gate |
| Best for | Fast answers and small follow-ups | Durable, auditable research |

Reactive is not weaker by default. It is optimized for a different job: answer quickly when the question can be resolved in one pass. Deep exists for runs where planning, approval, attribution, and durable execution matter more than speed.

---

!!! check "You should now understand"
    - Why reactive mode is a one-pass router
    - How the reactive prompt is layered
    - What `ClarificationGuardMiddleware` does and does not do
    - Why deep handles source elicitation more deterministically at the report boundary
