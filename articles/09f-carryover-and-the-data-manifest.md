# Deep Dive · Carryover and the Data Manifest

A user asks a follow-up: *"and how does that compare to last quarter?"* The agent already pulled the underlying sales two turns ago — the answer is sitting in state. Re-querying the source would be slow, costly, and sometimes non-deterministic. But the retrieved data is **megabytes**, and pasting it back into the model's context every turn would bury the actual conversation, blow the token budget, and drown the one line that matters under a wall of stale rows. That second failure is [context poisoning](00-glossary.md): the window fills with low-value text and the model's attention degrades.

So there are two ways to lose. Re-query when you already hold the answer, and you are wasteful and non-deterministic. Paste the held data back to avoid re-querying, and you poison the window. The data manifest is how a deep agent avoids **both**.

The safe mental model is:

> Give the model an *index* of what it holds, never a *copy*. It should see the shape and age of every prior result in ~100 bytes, decide reuse-versus-refetch itself, and pull the megabytes by name only when a step truly needs them.

This is a [context-injection](03-context-injection.md) mechanism — same middleware seam, same delimiter discipline — specialized for carrying prior *results* forward.

## What Accumulates, And Why You Cannot Just Resend It

`state["source_results"]` keeps every verbatim retrieval for the life of the thread. It is an [`operator.add`](05-identifiers.md) channel, so it only grows — by design, because assembly, citations, and follow-ups all need the raw evidence later. A single retrieval can be thousands of rows; a long thread can hold megabytes.

Two things must both be true:

- The model must be able to **reuse** a prior turn's answer instead of re-querying.
- The context window must **not** carry those rows on every turn.

An index reconciles them. Here is the whole item that lives in state versus the single line the model is shown:

```json
{
  "provenance_id": "SUBNATIONAL:plan-chat-1-1:step-1:a3f9c2d1",
  "source": "iqvia_gmi",
  "sub_question": "NZ subnational sales by region, 2024",
  "success": true,
  "retrieved_at": "2026-09-10",
  "answer": "NZ subnational sales were led by Auckland (32%), then …",
  "data": [ { "region": "Auckland", "value": 41280 }, "… 412 rows …" ]
}
```

```text
- SUBNATIONAL:plan-chat-1-1:step-1:a3f9c2d1 | ok=True | rows=412 | retrieved 2026-09-10 | NZ subnational sales by region, 2024
```

The megabyte object collapses to ~100 bytes. The row *count* rides the wire; the rows themselves never do.

## One Line Per Result — The Schema

```python
# manifest.py:50-60
def _line(result: dict[str, Any]) -> str:
    question = str(result.get("sub_question") or "")[:_QUESTION_PREVIEW]  # 80 chars
    question = question.replace(MANIFEST_END, "[/held data]")  # can't forge the boundary
    retrieved = str(result.get("retrieved_at") or "")[:10]
    return (
        f"- {result.get('provenance_id', '?')} | ok={bool(result.get('success'))} "
        f"| rows={len(result.get('data') or [])} "
        f"| retrieved {retrieved or '?'} | {question}"
    )
```

Each line carries exactly five fields, and each earns its place:

| Field | Why it is on the line |
|---|---|
| `provenance_id` | The durable name the model uses to *reference* the result in a plan step. See [Identifiers](05-identifiers.md). |
| `ok` | Whether the retrieval succeeded — a failed result is not reusable. |
| `rows` | The *shape* of what is held, without the data. Lets the model judge whether the held answer could plausibly cover the new need. |
| `retrieved` | The staleness signal. A follow-up asking for "current" numbers must not reuse a week-old pull. |
| `question` (80 chars) | What that retrieval was *about*, so the model can match it to the new step. |

Note the delimiter escape: the 80-char preview is user-influenced text, so it is neutralized (`MANIFEST_END → [/held data]`) exactly as [Context Injection](03-context-injection.md#two-subtle-ways-it-goes-wrong) escapes its own closing tag. Held content must never be able to forge the boundary of the trusted block.

## Capped By Attention, Not By Tokens

```python
# manifest.py:63-87
def build_data_manifest(source_results):
    results = [r for r in source_results if isinstance(r, dict)]
    if not results:
        return ""                               # nothing held -> inject nothing
    shown = results[-MANIFEST_MAX_LINES:][::-1]  # newest 30, newest-first
    lines = [_line(r) for r in shown]
    dropped = len(results) - len(shown)
    if dropped:
        lines.append(f"...plus {dropped} older result(s) held in state - "
                     "ask the user before assuming their content.")
    return (f"{MANIFEST_SENTINEL} - results already retrieved ... prefer reusing a held "
            "result over re-querying ... check `retrieved` for staleness. Reference results "
            "by provenance id\n" + "\n".join(lines))
```

The cap is the newest 30, newest-first — not a token budget. The reasoning is about *attention*, not accounting: reusable candidates are overwhelmingly recent, and a wall of ancient lines would bury the ones the model actually wants. Older results are not dropped from state; they collapse into a single honest rollup line — *"…plus N older result(s) held in state — ask the user before assuming their content."* That line is also the designated seam where a real `search_held_data` tool would plug in if a thread ever outgrows the ambient index.

Assembled, the block the model sees looks like this:

```text
[held data] - results already retrieved this thread. Prefer reusing a held result
over re-querying; check `retrieved` for staleness. Reference results by provenance id.
- SUBNATIONAL:plan-chat-1-1:step-1:a3f9c2d1 | ok=True | rows=412 | retrieved 2026-09-10 | NZ subnational sales by region, 2024
- CRP:plan-chat-1-1:step-2:b7e0aa14        | ok=True | rows=88  | retrieved 2026-09-10 | CRP channel share, NZ, 2024
...plus 3 older result(s) held in state - ask the user before assuming their content.
[/held data]
```

## Ambient, Not A Tool

The manifest is rebuilt from state and framed onto the latest genuine human message on **every** model call, through `wrap_model_call` — transient, never checkpointed, never accumulating, the same contract as `UserContextMiddleware`.

```python
# manifest.py:99-128 (abridged)
def _inject(self, request: ModelRequest) -> ModelRequest:
    manifest = build_data_manifest(...state source_results...)
    # framed onto the latest genuine HumanMessage; returns request unchanged if empty
    ...
def wrap_model_call(self, request, handler):        return handler(self._inject(request))
async def awrap_model_call(self, request, handler): return await handler(self._inject(request))
```

Why ambient and not a `search_held_data` tool the model calls when it wants? Because *the model can only reuse what it knows it holds — and it cannot ask about what it does not know exists.* A lookup tool costs extra model turns and, worse, fails **silently**: when the model does not think to call it, it simply re-queries, and nothing flags the missed reuse. Making the index ambient means the option to reuse is always in view. The cost is bounded precisely because the manifest is an index — 30 lines of ~100 bytes is cheap to carry every turn; 30 full results would not be.

## How Reuse Actually Happens

Reuse is not a hidden runtime optimization; it is a **plan step the human can see and approve**. At plan time the orchestrator writes a step with *no* `[source:]` tag that names the held result by its provenance id:

```text
# orchestrator_prompts.py:173-191 (the reuse rule)
Reuse before you re-query ... When a held result covers a step's information need
and the user is not asking for fresh numbers, do NOT create a retrieval step for it —
write the step WITHOUT a [source:] tag and name the held result's provenance id in the
step text (e.g. "Reuse the sales already retrieved
(SUBNATIONAL:plan-chat-1-1:step-1:a3f9c2d1) [group: 1 parallel]").
```

A retrieval step has a source tag and hits the source; a reuse step has none and points at evidence already in state. Because it is in the plan card, a reviewer sees "this turn reuses step-1's data" before approving.

## The Two Boundaries That Keep Reuse Honest

Reuse is only safe because of two hard rules. These are the anti-poisoning guarantees — they are what stop "reuse" from quietly becoming "hallucinate from a fading memory."

- **Prose-only reuse.** What the model holds from a past retrieval is *only the prose answer*, never the data rows — because the rows are not in context, only the pointer is. It may reuse a held answer's **text** when that text already states what the step needs. If the new question needs figures, rankings, or breakdowns the held prose does not literally contain, it must plan a fresh retrieval. It must **never** derive or estimate numbers from a held answer. The `rows=412` on the line tells the model data *exists* to be re-pulled by name; it does not put that data in the window.
- **Staleness check.** Every line carries its `retrieved` date. The instruction is to prefer a fresh retrieval when the held data looks stale, and the rolled-up older results explicitly say *"ask the user before assuming their content."* Age is a first-class input to the reuse decision, not an afterthought.

Together they draw the line: reuse the *conclusion* a prior turn already reached; re-fetch the *evidence* whenever the new answer would depend on numbers you are not currently holding.

## The Payoff, In One Sentence

The manifest is an index over state, not a copy of it: the model sees *what it holds* — provenance id, shape, and age, in ~100 bytes per line — and decides reuse-versus-refetch at plan time, while the megabyte payload stays parked in `source_results`, pulled by provenance id only when a step such as `run_analysis` actually needs it. Context stays small; reuse stays possible; nothing is estimated from memory.

## The Reactive Sibling

The reactive/quick path has the same idea under different names — a `[Carried data]` block, `carryover_reuse` rules, and an `answered_from_carried_data` flag — and the same two boundaries. One bug worth remembering (fixed in PR #1161): a turn framed *its own* same-turn fetch as *"[Carried data … earlier turns]"*, which is a subtle provenance lie. The manifest filters out the current turn's own results before labeling anything as previously retrieved, so "held earlier" always means *actually* earlier.

## Guardrails

- **Index, never copy.** Inject pointers plus shape and age. The moment the block contains data rows, you are back to poisoning the window.
- **Make the option ambient.** A reuse path the model has to remember to look up is a reuse path it will silently skip. Keep the index in view every turn.
- **Cap by attention.** Show the newest N, newest-first, and roll the rest into one honest line — do not let old entries bury fresh ones.
- **Forbid estimation from memory.** Prose answers may be reused; numbers must be re-fetched. Put the row count on the line so the model knows data exists to pull, not to invent.
- **Carry the age.** Staleness is part of the reuse decision. Every line gets its `retrieved` date, and old rollups tell the model to ask before assuming.
- **Escape your delimiter.** The user-influenced preview must never be able to forge the block boundary. See [Context Injection](03-context-injection.md#two-subtle-ways-it-goes-wrong).
