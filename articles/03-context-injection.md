# Step 2 · Context Injection

A user types: "Actually, give me sales for **France**." The agent replies about Australia — because somewhere before the model call, your code helpfully attached "user's default country is Australia," and that injected line sat closer to the model's attention than the correction the user just typed.

The prompt looked complete. Every field was populated. The request returned `200 OK`. And the answer was wrong, because the *right* context was attached at the *wrong* boundary. That is context injection, and it is one of the easiest ways to build a system that looks stable and answers incorrectly.

> New to the vocabulary here — [middleware](00-glossary.md#middleware), [checkpoint](00-glossary.md#checkpoint), [state](00-glossary.md#state)? Each term is defined once in the [glossary](00-glossary.md).

## What context injection is

Context injection is adding text or data to the model input that the user did **not** type in that exact request: user defaults, account permissions, previously mentioned entities, retrieved documents, tool results, an approved plan.

The critical fact is what the model actually receives. Your code *knows* which parts came from memory, tools, or the user. The model does not — it sees **one flat, composed prompt**. Unless you label the boundaries, "trusted memory" and "the user's words" are indistinguishable to it:

```text
Database memory
  → "User default country is Australia"

User message
  → "Give me my sales"

Injected model input (what the model sees)
  → [User context]
     User default country is Australia
     [/User context]

     Give me my sales
```

This is *useful*: "my sales" is meaningless without the account context. It is *dangerous* because stale or over-salient context can quietly override a later user correction — exactly the France/Australia bug above. Note the continuity with [LangGraph State](02-state-and-checkpoints.md): a stale `user_context` value doesn't just sit in state, it gets **composed into the prompt** and steers the answer.

## Two subtle ways it goes wrong

Most context-injection bugs are not "we forgot the context." They are "we attached it to the wrong thing." Two that bite hard:

**1. Attaching context to a synthetic message, not the user's.** Some frameworks insert synthetic messages during history summarization. They may still be `HumanMessage` objects, but they are not user-authored. If you blindly compose context onto "the last human message," you may be labeling a machine-written summary as the user's latest words. Walk back to the *real* one:

```python
for i in range(len(messages) - 1, -1, -1):
    msg = messages[i]

    if not isinstance(msg, HumanMessage):
        continue

    if msg.additional_kwargs.get("lc_source") == "summarization":
        continue  # framework-created, not the user

    messages[i] = HumanMessage(
        content=compose_with_context(msg.content, context),
        additional_kwargs=msg.additional_kwargs,
    )
    break
```

**2. Not escaping your own delimiter.** If your format uses `[/User context]` to close the trusted block, then user-influenced memory must not be able to *contain* that exact marker — otherwise text inside the block can forge its own ending and impersonate a system boundary:

```python
def compose_with_context(content: str, context: str) -> str:
    safe_context = context.replace("[/User context]", "[/user context]")
    return f"[User context]\n{safe_context}\n[/User context]\n\n{content}"
```

This does not make prompt injection impossible — it closes one obvious escape hatch. Any time a delimiter separates trusted from untrusted text, the untrusted side must not be able to write the delimiter.

## The boundary question: *where* do you inject?

Context injection has a location, and the location is a design decision with a tradeoff:

```mermaid
flowchart TD
    A[HTTP request] --> B[build graph input]
    B --> C[checkpoint: load prior state]
    C --> D["middleware: inject request-time context
    ← safest boundary for per-request data"]
    D --> E[model call]
    E --> F[model response → written back to state]
    style D fill:#f0faf0,stroke:#388e3c
```

The safest place for **request-only** context is usually just before the model call, via [middleware](00-glossary.md#middleware). That keeps the [checkpoint](00-glossary.md#checkpoint) clean — you are not persisting per-request text into durable state where it can go stale and leak into a later turn. The tradeoff: the injected text is *invisible* when you inspect saved graph state, so you must be able to reconstruct it for debugging.

Because prompt assembly can differ across code paths, these bugs usually surface as **inconsistent** answers rather than hard failures — the same request works in one route and fails in another because the two routes compose the prompt differently.

## Guardrails

- **Centralize prompt construction.** One place assembles the final input, so there is one place to review — the same lesson as the input factory in [LangGraph State](02-state-and-checkpoints.md).
- Make context **sources** explicit in typed state, so a reviewer can see what came from where.
- In debug environments, **snapshot the final prompt** actually sent to the model.
- **Test prompt assembly independently** from model calls — it is pure string composition and deserves its own tests.
- Test the awkward inputs: summarized histories, multimodal messages, and **empty context** (does a correction survive, or does stale context win?).

---

---

!!! check "You should now understand"
    - Why context injection is useful: the user's text often lacks account, permission, entity, or memory context
    - Why context injection is dangerous: the model sees one composed prompt, not your internal source labels
    - Why request-only context usually belongs in middleware just before the model call
    - Why synthetic framework messages and forged delimiters are production correctness risks, not polish issues

??? question "Try this"
    **A user says "Actually, use France," but their account default is Australia. The context middleware still injects "default country: Australia" above the user's message. What should the system guarantee, and where should that guarantee live?**

    ??? success "Answer"
        The system should guarantee that explicit entities in the user's current message beat defaults from injected context. Some of that can be helped by prompt wording, because the model is the component resolving the ambiguous text. But the engineering guardrail is broader: label the injected block clearly, keep request-only context out of durable state, test the final composed prompt, and avoid reinjecting stale context in phases where a locked plan should already own the entities.

*Next: [Step 3 · Planning and Human Approval](04-planning-and-human-approval.md) — the agent now has state and context, so the next job is to produce an inspectable plan and pause before expensive work begins.*
