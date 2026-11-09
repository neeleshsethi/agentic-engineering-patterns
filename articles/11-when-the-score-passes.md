# Capstone 2 · When The Score Passes And The Work Fails

The first capstone was about code that returned `200 OK` while corrupting the meaning of a run. This one is about the same failure one layer up: **an evaluation that passes while the agent's work is wrong**, and **a tool call that succeeds while the user's task does not**.

> **Capstone.** Read [Nine Silent Failures](10-nine-silent-failures.md) first. That article is about infrastructure: state, locks, keys and persistence. This one is about the contract between the model, its tools and the evaluations we use to judge it. The system is the same commercial-strategy agent; every name below is generic.

The lesson from the first capstone was:

```text
Transport success:  the HTTP request finished
Semantic success:   the right context, state, lock, plan and persistence rules held
```

This article adds two more lines:

```text
Tool success:       the tool returned without error
Task success:       the user got the thing they asked for, built from real evidence

Score success:      the evaluation passed
Behavior success:   the agent made the right decision and said so honestly
```

Every failure below lives in one of those gaps.

## Part 1 · Nine more silent failures, at the model–tool contract

These came from production traces and regression tests after the first capstone shipped. In each case the infrastructure was correct: the right context reached the model, the run completed, the stream closed cleanly. What failed was the agent's behavior or the contract around it.

| Failure | What "succeeded" | What actually happened |
| --- | --- | --- |
| Output-language instruction never reached the model | Language detected, answer streamed | Answer came back in the default language |
| New topic inherited old scope | Follow-up answered | Previous entities and filters applied to an unrelated question |
| Source asked a question; parent swallowed it | Tool returned `success` | The source needed clarification; the user never saw the question |
| Clarification answer routed to the wrong agent | Answer delivered | User chose a source *inside* one agent; a different agent with the same name was called |
| Artifact described instead of delivered | Answer streamed | The agent wrote about the table it would build; no table arrived |
| Column acknowledged but not rendered | "Added the column" | The artifact was unchanged |
| Inline numbers not read from stored rows | Answer contained figures | The figures were never read from the retrieved data |
| Right sources, wrong query shape | Source-set check passed | A cross-scope question was sent as one combined query instead of one per source |
| One source's context leaked into another's query | Both sources answered | Market-specific context filtered a regional query |

### 1. Diagnose the boundary before blaming the model

The output-language failure looked like a weak model ignoring an instruction. It wasn't. Language detection worked, but its result never reached the prompt the model actually received.

The review habit: **before you call something model behavior, read the exact prompt at the model boundary.** In a tracing tool such as Langfuse, open the generation span, not the request span. If the instruction isn't there, it's an infrastructure bug, and no amount of prompt wording will fix it.

The fix had three parts: propagate the detected language into the loop prompt, state it near the opening and again after the answer-format rules, and add a regression test that asserts the instruction is present at the boundary.

A caution about what that fix proves. Propagation, wording and placement all changed in one change. It's tempting to say "moving the instruction later fixed it," but placement's own effect was never isolated. The defensible claim is the boundary test, not a placement result. (Part 3 shows how you would test placement properly.)

### 2. A successful tool status is not a successful task

Three failures share one root: the system treated "the tool returned" as "the task is done."

- **The swallowed question.** A data source sometimes returns a *question* instead of rows ("Which sales source: vendor or internal?"). The tool call succeeds. If the parent agent treats any non-error result as data, the question disappears and the agent answers from nothing.
- **Described, not delivered.** Asked to show a table, the agent wrote a paragraph describing the table. The model produced text; the turn completed.
- **Acknowledged, not rendered.** Asked to add a column that already existed in the data, the agent said it had added it, and returned the unchanged artifact.

The fix is a contract, not a better prompt:

```python
def handle_source_result(result, step):
    if result.kind == "question":
        # Relay to the user, tied to the source that asked and the open step.
        return Clarification(asked_by=result.source_id, step_id=step.id,
                             question=result.question)
    if result.kind == "rows":
        return Evidence(source_id=result.source_id, rows=result.rows)
    raise UnexpectedResult(result)
```

For artifacts, separate two kinds of follow-up:

- **Presentation-only** changes (sort, relabel, chart type) may rebuild from rows already held.
- **Data-changing** requests (new rows, new values, a different filter) must retrieve. A fail-closed guard rejects a "rebuild" that claims to change data without a retrieval in the same turn.

We learned the second rule the hard way: an earlier filter-to-rebuild shortcut let the render path keep every row while the answer claimed the filter was applied.

### 3. A clarification answer belongs to whoever asked

A third-party data vendor appeared twice in the system: as a source *inside* the sales agent, and as an agent of its own. The sales agent asked "vendor or internal?" The user said "vendor." The orchestrator matched the word and routed to the vendor *agent*.

Nothing in the infrastructure was wrong, and the provenance metadata in the orchestrator's own context showed the mismatch. The model bound the answer to the wrong referent by name.

The fix: a **clarification relay** that records who asked and which plan step is open, and routes the answer back to that asker. The model can still phrase the question; code decides where the answer goes.

```python
@dataclass(frozen=True)
class OpenClarification:
    asked_by: str      # source or agent id that asked
    step_id: str       # plan step waiting on the answer
    question: str

def route_answer(answer: str, pending: OpenClarification) -> Dispatch:
    # Never re-route by matching names in the answer text.
    return Dispatch(target=pending.asked_by, step_id=pending.step_id, user_choice=answer)
```

The general rule: **the scope of an answer is set by the question, not by the words in the answer.**

### 4. Context is per-source, not per-turn

Two failures came from context with the wrong scope:

- **Inherited scope.** A new, unrelated question picked up the previous turn's entities and filters. The carryover rules were written for follow-ups and applied to everything.
- **Cross-source contamination.** Context meant for a market-specific source (a country, a local metric definition) was injected into a regional source's query.

Both fixes are the same shape: resolve scope per turn (is this a follow-up or a new topic?) and inject context per source, not as one blob for the whole request.

### 5. Grounding means reading, not retrieving

The agent quoted figures inline after a retrieval step, but never read them from the stored rows. Retrieval happened; reading didn't. The action contract now requires a stored-row read before any number is quoted. An evaluation that only checks "did the agent retrieve?" would never catch this.

## Part 2 · Evaluations that don't lie

### Score decisions and artifacts separately

Our routing evaluation grades the agent's first decision: which sources it would call. It doesn't execute retrieval or check the final answer. That is useful and cheap, but it measures the **orchestration contract**, not the **truth of the answer**.

A passing tool-choice score can hide an unchanged table (failure 2). So keep two scores:

| Score | Question it answers | Example |
| --- | --- | --- |
| Decision correctness | Did the agent choose the right action? | Right sources, right mode, retrieve vs rebuild |
| Artifact correctness | Did the user get the right result? | Table actually changed, figures match stored rows |

Report them separately. A rise in one is not a rise in the other.

### Compare the same cases, not two summaries

Between two evaluation snapshots, the non-pass rate fell by more than half. It looked like a clean win. It wasn't, for two reasons:

1. The case sets differed (new cases were added, sources changed), so the two summaries weren't comparable.
2. "Non-pass" counted clarifying questions as failures.

Restricting to the cases present in both runs with unchanged expected labels, and looking at each verdict separately, told a different story:

| Verdict on the same cases | Before | After |
| --- | --- | --- |
| Correct | ~70% | ~82% |
| **Wrong but confident** | **~10%** | **~15%** |
| Asked for clarification | ~20% | ~3% |

Correctness improved, mostly because the agent asked less. But some cases that used to ask now answered **confidently and wrongly**. The uncertainty didn't go away; it stopped being disclosed.

The tool that shows this is a transition table over paired cases:

```python
from collections import Counter

def transitions(before: dict[str, str], after: dict[str, str]) -> Counter:
    """before/after map case_id -> verdict ('correct', 'incorrect', 'clarify')."""
    shared = before.keys() & after.keys()
    return Counter((before[c], after[c]) for c in shared)

# The cell to watch: ('clarify', 'incorrect') — questions that became confident errors.
```

The rule: **never report an improvement from two unpaired summaries.** Pair the cases, split the verdicts, and look at the transitions. The safety-relevant change often hides in one off-diagonal cell.

### Know where your labels come from

Be honest with yourself about three things:

- **Who wrote the expected labels.** Ours are engineering-authored or derived from rules in the question banks, not independent expert consensus. That's fine for regression; it's not ground truth.
- **Whether the holdout is really held out.** Our file called "holdout" deliberately includes questions that also appear as prompt examples. That makes it regression coverage, not a generalization test.
- **Which cases are known-open.** Some cases are flagged as known failures so they're still scored but don't fail the build. An unexpected pass should force someone to remove the flag, not pass silently.

### Check the shape, not just the set

"Did the agent pick the right sources?" is a set comparison. It misses a cross-scope question sent as one combined query when it should have been one query per source. Add query-shape assertions for the cases where shape matters.

## Part 3 · Claims you can and can't make

Production changes rarely isolate one variable. In one release, propagation, wording and placement of an instruction all changed together. You can claim "the instruction now reaches the model, and a test enforces it." You can't claim "placement fixed it."

If you want to know whether placement matters, design the experiment:

1. Freeze the instruction text, model, settings, tools, access and seeded conversation state.
2. Ensure the instruction reaches the model in every condition, so missing propagation can't masquerade as a placement effect.
3. Vary only the location (opening, after format rules, latest turn, a structured state block).
4. Randomize order, repeat each case several times, and score with rules declared in advance.
5. Report paired changes and regressions with uncertainty, on cases that weren't copied from prompt examples.

## Part 4 · Telemetry you shouldn't trust

- **Missing negative feedback is not success.** Most users don't click thumbs-down. Silence is not a score.
- **Cost fields can be empty.** Most of our deep-run traces reported zero cost. Treat a field as evidence only after checking it's populated.
- **Attribution from traces is often impossible.** When we tried to classify user-reported failures as model or infrastructure from traces alone, most stayed ambiguous. If you can't tell from the outside, you need the system (or the model) to record why it did what it did.

## The pattern

| Failure class | What looked fine | What to check instead |
| --- | --- | --- |
| Boundary | Instruction in the code | Instruction in the prompt the model received |
| Tool vs task | Tool returned success | Result kind: rows, question, or error |
| Artifact | Answer streamed | Artifact actually changed; data changes retrieved |
| Relay | Answer delivered | Answer routed to the asker, not a name match |
| Scope | Follow-up answered | Scope resolved per turn, context injected per source |
| Grounding | Retrieval happened | Numbers read from stored rows |
| Evaluation | Score went up | Paired cases, split verdicts, transitions |
| Labels | Holdout passed | Holdout independent of prompt examples |
| Telemetry | No complaints, zero cost | Fields populated; silence isn't success |

Review habits that would have caught these earlier:

- Read the prompt at the model boundary before blaming the model.
- Treat every tool result as a typed value (data, question, or error), never as "not an error, so data."
- Score decisions and artifacts separately.
- Pair evaluation cases and read the transitions, especially "used to ask, now confidently wrong."
- Write down what a change proves and what it doesn't.

The stack is incidental. These are failure classes of any agent that calls tools, asks questions and gets judged by a score.
