# Deep Dive · Clarifications

A clarification is any turn where the system asks the user something instead of writing the final answer. In a deep agent, that is not just UX copy. It changes which graph path runs, which state is carried, whether a report may be written, and whether the next user message resumes an interrupt or starts a fresh invocation.

The safe mental model is:

> Clarification is control flow. The wording is prompt work; the pause semantics are code.

## The Four Clarification Shapes

| Type | When | Detector | User sees | What continues |
|---|---|---|---|---|
| Plan-gate clarification | During plan submission | `submit_plan(clarification=...)` | Plan card plus one assumption or question | The approval/refine gate. |
| Terminal clarification | Before planning | The turn is classified as non-reportable because an entity or metric has no safe default | One question, no plan | The next user turn starts planning with the missing information. |
| Source elicitation | Mid-retrieval | A source result is marked `is_elicitation` | The source's question, relayed by the agent | The next user turn re-queries only the asking step. |
| Zero-retrieval guard | At exit | No current-cycle results, or the evidence set was already reported | Conversation text, not a report | The exit path refuses to write a report over nothing. |

The mistake is treating all four as the same kind of pause. Only the plan gate is a LangGraph `interrupt()`. Source elicitation looks like a pause to the user, but the graph run has completed normally.

## Plan-Gate Clarification

Plan-gate clarification happens while the model is proposing a plan. It is useful when the plan can proceed under a visible assumption, but the human should see that assumption before approving.

```text
User: "Compare sales performance across available sources."

Plan card:
  Step 1: Pull sales by market from SOURCE_A
  Step 2: Pull sales by channel from SOURCE_B

Clarification:
  "SOURCE_A and SOURCE_B use different market definitions. I will align on retail market unless you refine the plan."
```

This rides the existing approval surface. The user can approve the assumption or refine the plan. No separate clarification round-trip is needed.

Use this when:

- The ambiguity is attached to the plan itself.
- The human can resolve it by approving or refining the plan.
- The system should not run retrieval until the plan is accepted.

## Terminal Clarification

Terminal clarification happens before any plan exists. The system has no safe default for a required entity or metric, so it asks one question and stops.

```text
User: "Show me performance for the launch brand."

Agent:
  "Which launch brand should I use?"
```

The important invariant is that terminal clarification happens before `write_todos`. A submitted plan should be fully determined. If the agent writes vague todos and hopes to clarify during execution, the plan approval loses meaning: the human approved a shape that still contains unknowns.

Use this when:

- The missing value is required for routing or entity resolution.
- No safe default exists.
- The plan would be misleading without the answer.

## Source Elicitation

Source elicitation is the hard case. It happens after retrieval begins, when a downstream source asks back instead of returning final data.

```text
Step 1: query promo ROI      -> returns data
Step 2: query subnational    -> asks "retail or non-retail?"
```

The source's ask-back must be detected before the report writer runs. Do not rely on a second model to guess whether a source result is a clarification if the source protocol can say so.

| Source shape | How the flag is set |
|---|---|
| Native signaling | The client stamps `is_elicitation: true` on the result. |
| Status-code protocol | A deterministic classifier maps `input-required` to elicitation. |
| Prose-only replies | A narrow deterministic ladder catches clarification phrases and question-shaped no-table replies. |

The key distinction:

> Detection is deterministic before either mode sees the result. Acting on that detection is prompt-led in reactive, but code-enforced in deep.

| Mode | Detection | What stops the answer | Bookkeeping |
|---|---|---|---|
| Reactive | Deterministic `is_elicitation` flag when the source/client can set it; prompt backstop for question-shaped replies | Prompt rules tell the one-pass router to relay the source question instead of synthesizing. | Results stay available for the next turn. |
| Deep | Deterministic `is_elicitation` flag on `SourceResult`; deep treats that flag as the source of truth | `DeepExitPathMiddleware` checks `pending_elicitation()` and skips report generation in code. | The step stays open; the report is skipped until the user answers. |

So yes: **clarification handling is deterministic for deep at the report boundary.** The model is still prompted to relay the question politely, but it is not trusted to decide whether a report may be written. If a current-cycle step's latest successful result is an elicitation, `pending_elicitation()` blocks the report.

That "latest successful" qualifier matters. A failed retry should not accidentally clear an open source question, and an older elicitation should not block forever after the same step later returns real data. The predicate groups successful results by `step_id`, keeps the latest one per step, then asks whether any of those latest successes still carries `is_elicitation`.

The deterministic deep path has three owners:

1. **Source adapter/client** stamps `SourceResult.is_elicitation`.
2. **`provenance.py`** owns `pending_elicitation()`, the shared predicate that asks, "is any current-cycle step's latest successful retrieval still waiting on the user?"
3. **The report gates call that predicate before output is written.** `SufficiencyGateMiddleware` should not grade a report-ready evidence set while elicitation is pending, and `DeepExitPathMiddleware` must skip report generation when `pending_elicitation()` is true.

That is the "middleware gate" in deep: the model may phrase the relayed question, but the sufficiency/exit path code decides whether a report is allowed.

Deep needs that code backstop because the failure mode is expensive: a polished report written over "which market definition?" looks like success and is wrong.

## Partial Source Clarification

When one source returns data and another asks back, deep should not ship a partial report.

`pending_elicitation` is a whole-cycle gate: if any current step is still waiting on the user, the entire report waits. That does not mean the successful result is discarded. It stays in the accumulating `source_results` channel.

The user-facing question should mention both facts:

```text
"I have the promo ROI figures. For subnational sales, the source asks:
retail or non-retail?"
```

When the user answers, the system re-queries only the asking step. The already completed result is reused from state.

## Elicitation Reply Is Not Resume

This distinction catches teams because the user experience looks like a pause. Internally, it is not the same pause as plan approval.

| Question | Plan approval | Source elicitation reply |
|---|---|---|
| Graph state left behind | Pending `interrupt()` | None; the previous run completed normally |
| How the user's response enters | `Command(resume={"type": "approve"})` or an equivalent staged resume value | Fresh graph invocation on the same `thread_id` |
| Does it re-plan? | Not applicable | No; the plan stays locked |
| What is reused? | The checkpointed plan and state | Completed source results from `source_results` |
| What runs? | SQS worker resumes approved execution | The ordinary deep reply path re-queries the open step |

Why this matters: re-planning would mint a new `plan_id`, which would orphan the carried result from the current cycle. Current-cycle evidence is filtered by plan identity, so keeping the locked plan is what lets the clarified turn reuse already-fetched data safely.

## Zero-Retrieval Guard

The exit path should refuse to write a report when there is no current-cycle evidence. That can happen when:

- A source elicitation is pending.
- Retrieval produced no usable result.
- SQS redelivered a run whose evidence set was already reported.

The guard prevents a report from being generated over nothing or over the same `reported_cycle` twice. This is a code boundary, not a prompt preference.

## There Is No DeepClarificationMiddleware

Clarification in deep rides existing seams:

- The `submit_plan` tool handles plan-gate clarification.
- The turn classifier handles terminal clarification before planning.
- Source clients set `is_elicitation`.
- Provenance helpers answer "is any current step waiting on the user?"
- The sufficiency and exit path middlewares decide whether a report can be written.

That is better than a generic "clarification middleware" because each kind of clarification has a different lifetime. A plan-gate clarification resumes an interrupt; a source elicitation reply is a new invocation; a terminal clarification has no plan yet.

## Review Checklist

- Does the system clarify entity or metric gaps before writing todos?
- Does source elicitation have a deterministic flag before the model sees the result?
- Does the report writer refuse to run while `pending_elicitation()` is true?
- Does a source elicitation reply enter as a fresh invocation on the same `thread_id`, not `Command(resume=...)`?
- Does the reply keep the locked `plan_id` and re-query only the open step?
- Does the user-facing question mention any data already fetched, so the user knows only part of the work is pending?

---

!!! check "You should now understand"
    - The difference between plan-gate, terminal, source, and zero-retrieval clarifications
    - Why source elicitation is not the same as plan approval resume
    - Why partial source clarification blocks the whole report but keeps completed data
    - Why deep clarification is distributed across gate, source clients, provenance, sufficiency, and exit path

??? question "Try this"
    **A turn queries two sources. One returns data; the other asks the user to choose retail or non-retail. The user answers on the next turn. Should the system call `Command(resume=...)`, re-open the plan gate, or invoke the graph normally on the same thread?**

    ??? success "Answer"
        Invoke the graph normally on the same `thread_id`. The elicitation turn did not leave a pending `interrupt()`, so there is nothing to resume. The plan stays locked, the completed source result remains in `source_results`, and only the asking step is re-queried with the user's answer folded in.
