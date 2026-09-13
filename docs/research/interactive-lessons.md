# Interactive & Animated Lessons on a Static MkDocs Material Site

**Summary.** For teaching timing/concurrency concepts (locks, conditional writes, checkpoints/interrupt-resume, SSE, sequence/idempotency keys, zombie fencing) on a static MkDocs Material site deployed to GitHub Pages, the highest-leverage approach is a two-tier one: **Mermaid `sequenceDiagram`/`stateDiagram` for structure** (officially supported by Material, zero JS to author, renders server-free) plus a **user-driven step-through ("scrubber") component for timing** — which we already have. Neither needs a build step or server, both survive `prefers-reduced-motion`, and both align with the "explorable explanations" tradition of letting the reader drive rather than watch. Heavier options (GSAP/anime.js/Motion, live simulations, in-browser Python via Pyodide) are viable on a static host but cost far more effort-per-lesson and, for auto-playing motion, work against accessibility. This document cites primary sources for every non-obvious claim; where a fact could not be verified from a first-party source it is called out inline.

Sources discipline: every claim below links to the page that *owns* the fact (official docs, the source repo, or the author's own site). Secondary write-ups are not used.

---

## 1. Diagrams-as-code in MkDocs Material (Mermaid)

Material for MkDocs has **first-party Mermaid support**. Per the official reference page, the **officially supported** diagram types are: **flowcharts, sequence diagrams, state diagrams, class diagrams, and entity-relationship diagrams**. Other Mermaid types (pie, gantt, user journey, git graph, requirement) "may work" but are *not* officially supported and "don't work well on mobile" ([Material for MkDocs — Diagrams](https://squidfunk.github.io/mkdocs-material/reference/diagrams/)).

Enable it with the `pymdownx.superfences` custom fence — the exact block from the official docs:

```yaml
markdown_extensions:
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
```

The docs state "no further configuration is necessary," and that the integration works with instant loading and automatically adopts the site's fonts/colors and light/dark schemes ([Material for MkDocs — Diagrams](https://squidfunk.github.io/mkdocs-material/reference/diagrams/)).

**Relevance to us:** `sequenceDiagram` maps directly onto SSE streaming, lock acquisition handshakes, and idempotency-key exchanges; `stateDiagram-v2` maps onto LangGraph checkpoint/interrupt-resume lifecycles. This is already enabled in our `mkdocs.yml` (the `pymdownx.superfences` mermaid fence is present). **Limitation:** Mermaid is static per render — it shows the *shape* of an interaction but does not let the reader step through *time*. That gap is exactly what the scrubber fills.

---

## 2. Explorable / explainable explanation patterns

**Bret Victor, "Explorable Explanations" (2011).** The founding essay. Victor's stated goal is for "text to be used as an *environment to think in*," and he argues a *reactive document* lets readers "play with the author's assumptions and analyses, and see the consequences" — turning passive readers into active explorers ([Bret Victor — Explorable Explanations](http://worrydream.com/ExplorableExplanations/)). A key design constraint from the essay: interactivity must augment, not replace, readable prose (the page should still read as static text if you never touch a control).

**Nicky Case & the community — explorabl.es.** A hub/"movement" of interactive, playable explanations maintained by "artists, coders & educators," open-source on GitHub. Its stated mission is to "reunite play and learning" ([explorabl.es](https://explorabl.es/)). This is the canonical catalog of the genre and a good source of pattern ideas (sliders, draggable state, step controls).

**Scrollytelling** is the adjacent pattern (narrative text triggers state changes as you scroll). It is a legitimate technique but adds motion-on-scroll complexity and reduced-motion concerns; the reader-driven step control (our scrubber) achieves the same "you drive the reveal" pedagogy with less risk.

**Takeaway for us:** the tradition favors **reader-controlled** interaction over auto-play. Our scrubber (prev/next/dots + optional play) is squarely in this tradition.

---

## 3. Landmark interactive distributed-systems teaching artifacts (verified)

All four exist and were verified against their own sites/repos:

- **The Secret Lives of Data — Raft** ([thesecretlivesofdata.com/raft](http://thesecretlivesofdata.com/raft/)). A **guided, step-through** visualization of the Raft consensus algorithm. The Raft project itself characterizes it as "more guided and less interactive," making it "a gentler starting point" ([raft.github.io](https://raft.github.io/)). *Technique: guided animated walk-through.* (Note: the tool's own page returns mostly nav markup to a fetch; its nature is confirmed via the Raft site's description of it.)

- **Raft — raft.github.io** ([raft.github.io](https://raft.github.io/)). The official Raft site embeds **RaftScope** (by Ongaro): "a Raft cluster running in your browser… Five servers are shown on the left, and their logs are shown on the right," and you "can interact with it to see Raft in action." *Technique: live in-browser simulation you manipulate directly* (the authors note it is "still pretty rough around the edges").

- **Jepsen — jepsen.io** ([jepsen.io](https://jepsen.io/)). Not an animation, but the reference body of work on distributed-systems *correctness*. Jepsen "aims to improve the safety of distributed databases, queues, consensus systems, etc." via an open-source testing library plus in-depth analyses. Run by Kyle Kingsbury. *Technique: rigorous testing + written analyses (great source material for lesson content on consistency).* 

- **Gossip Glomers (Fly.io + Kyle Kingsbury) & Maelstrom** ([fly.io/dist-sys](https://fly.io/dist-sys/)). "A series of distributed systems challenges brought to you by Fly.io," built with Jepsen's author. **Maelstrom** (built on Jepsen) routes messages between nodes you implement, injects failures, and "perform[s] verification checks based on the consistency guarantees required by each challenge." *Technique: hands-on, do-it-yourself challenges rather than an embedded widget.*

**Pattern spectrum learned from these:** *guided step-through* (Secret Lives) → *live simulation you poke* (RaftScope) → *build-it-yourself* (Gossip Glomers). For a docs site, the guided step-through is the cheapest to author and the most reliable; live simulation is highest-effort/highest-payoff for a few flagship lessons.

---

## 4. Lightweight animation libraries (static-site fit, license, maintenance)

| Library | License | Static-site drop-in (no build) | Size / notes | Primary source |
|---|---|---|---|---|
| **CSS-only step-through** | n/a (platform) | Yes — pure CSS/HTML; toggle classes | 0 JS deps; our scrubber uses this + tiny JS | (browser platform) |
| **anime.js v4** | **MIT** | Yes — via ESM/CDN import map (`https://esm.sh/animejs@4`); v4 is ESM-first (npm is the documented default) | base bundle ~24.5 KB; modular imports 0.22–6.41 KB. Browser reqs: Chrome/Edge 89+, Safari 16.4+, FF 108+ | [animejs.com](https://animejs.com/), [license (MIT)](https://github.com/juliangarnier/anime/blob/master/LICENSE.md), [install/CDN](https://animejs.com/documentation/getting-started/installation/) |
| **GSAP** | **"No Charge" standard license — free for everyone** (incl. plugins) since 2025-04-30, post-Webflow; source on GitHub. Main restriction: can't build a competing no-code visual-animation tool | Yes — CDN script tag, no build | Professional-grade; larger surface area than needed for step diagrams | [gsap.com](https://gsap.com/), [standard license](https://gsap.com/community/standard-license/), repo: github.com/greensock/GreenSock-JS |
| **Motion (formerly Motion One)** | **MIT, open source** | Yes — ESM import or jsDelivr global `<script>`, no build | mini HTML/SVG `animate()` "just 2.3kb"; hybrid engine (browser + JS). *Could not confirm from first-party quick-start whether the mini path is strictly the Web Animations API* | [motion.dev](https://motion.dev/), [quick start](https://motion.dev/docs/quick-start) |

Notes:
- **anime.js v4 is ESM-first.** The official install docs lead with npm/ESM; browser-without-build use is done via an **import map** pointing at a CDN such as esm.sh — verified from anime's own installation docs and the esm.sh no-build CDN ([anime install](https://animejs.com/documentation/getting-started/installation/), [esm.sh](https://github.com/esm-dev/esm.sh)). This is fine on GitHub Pages but is one more moving part than a single `<script>` global.
- **GSAP's licensing change is material:** as of 2025-04-30 it is free for everyone including all plugins, with the sole practical caveat being you may not use it to build a competing no-code animation builder ([GSAP standard license](https://gsap.com/community/standard-license/)). Not a concern for a teaching site.
- **Motion** has the smallest documented footprint (2.3 KB mini) and a clean CDN global path, making it the lightest "real" library if we outgrow CSS.

**All four work on a static host with no server.** The real cost is authoring time and accessibility discipline, not hosting.

---

## 5. Runnable-code embeds (optional, for a static site)

- **In-browser Python — Pyodide.** CPython compiled to WebAssembly/Emscripten; runs Python (and many packages incl. NumPy/pandas) entirely in the browser with **no server**, and you can self-host it from a static server ([Pyodide docs](https://pyodide.org/en/stable/), [Pyodide repo](https://github.com/pyodide/pyodide)). **License: Mozilla Public License 2.0** ([LICENSE](https://github.com/pyodide/pyodide/blob/main/LICENSE)). Tradeoff: **large initial download** (the CPython/WASM runtime is multi-megabyte; the first-party docs describe self-hosting from releases but *do not* state an exact size, so treat "several MB, slow cold start" as the known-order-of-magnitude, not a cited figure). Good for a *few* "run this idempotency-key check yourself" cells; bad as a default on every page.
- **JS sandboxes.** Client-only editors (e.g. embedding a small `<script type="module">` playground) run natively with no runtime download and are the lighter option when the concept can be shown in JS. Full sandbox platforms (Sandpack, StackBlitz) are heavier and pull third-party runtimes/iframes — usable but they add external dependencies and network calls, which cuts against the "static, self-contained, reliable" goal.

**For concurrency/timing specifically, runnable code is a weak fit:** the interesting behavior is *interleaving across time and across nodes*, which a single-threaded in-page REPL does not surface well. A step-through of an interleaving is more legible than asking the reader to run code.

---

## 6. Recommendation for our case

Given a static MkDocs Material site on GitHub Pages, an existing custom **scrubber** step-through component (prev/next, dots, optional play, keyboard `←/→`, and autoplay disabled under `prefers-reduced-motion`), and Mermaid already enabled, the highest learning-per-effort combination is:

1. **Mermaid `sequenceDiagram` + `stateDiagram-v2` for structure (default, every lesson).** Officially supported, zero authoring JS, server-free, theme-aware, and it inherits light/dark automatically ([Material for MkDocs — Diagrams](https://squidfunk.github.io/mkdocs-material/reference/diagrams/)). Use sequence diagrams for SSE/lock/idempotency handshakes and state diagrams for the LangGraph checkpoint/interrupt-resume lifecycle. This is the "read it as static text" baseline the explorable-explanations tradition insists on ([Bret Victor](http://worrydream.com/ExplorableExplanations/)).

2. **The existing scrubber for timing/concurrency (the differentiator).** Static diagrams cannot show *interleaving over time* — the whole point of zombie fencing, conditional-write races, and sequence-key ordering. Reader-controlled stepping (not auto-play) is exactly what the explorable tradition favors ([explorabl.es](https://explorabl.es/)) and mirrors the guided step-through that made The Secret Lives of Data effective ([raft.github.io](https://raft.github.io/) describing it). We already own this component, it's dependency-free, and it already honors `prefers-reduced-motion` and keyboard nav — so the marginal cost is only authoring steps, not building infrastructure.

3. **A tiny animation library only for one or two flagship "live" lessons — pick Motion or GSAP.** If a concept genuinely needs continuous motion (e.g. a lock's TTL countdown racing a zombie writer), reach for **Motion** (MIT, ~2.3 KB mini, CDN global, no build — [motion.dev](https://motion.dev/)) or **GSAP** (now free for everyone, CDN, no build — [gsap.com](https://gsap.com/) / [license](https://gsap.com/community/standard-license/)). Keep it opt-in and behind a reduced-motion guard.

**Why not the others (for the default path):**
- **Live simulations (RaftScope-style)** are the highest payoff but also the highest build/maintenance cost; reserve for a single flagship page, not the standard lesson template.
- **Pyodide / runnable code** is server-free but heavy (multi-MB cold start, MPL-2.0) and a poor fit for *timing* concepts; consider it only for isolated "verify the invariant yourself" cells, never as a per-page default.

**Accessibility tie-in.** Both recommended tiers are safe by construction: Mermaid renders as static SVG; the scrubber already reads `prefers-reduced-motion: reduce` and suppresses autoplay while keeping manual stepping. Any tier-3 animation must be wrapped in the same `matchMedia("(prefers-reduced-motion: reduce)")` guard the scrubber already uses, so motion is always opt-in and the lesson remains fully usable without it.

---

### Sources
- Material for MkDocs — Diagrams: https://squidfunk.github.io/mkdocs-material/reference/diagrams/
- Bret Victor, Explorable Explanations: http://worrydream.com/ExplorableExplanations/
- explorabl.es (Nicky Case / community): https://explorabl.es/
- The Secret Lives of Data — Raft: http://thesecretlivesofdata.com/raft/
- Raft (official, RaftScope): https://raft.github.io/
- Jepsen: https://jepsen.io/
- Gossip Glomers / Maelstrom (Fly.io + Kyle Kingsbury): https://fly.io/dist-sys/
- anime.js: https://animejs.com/ · install/CDN: https://animejs.com/documentation/getting-started/installation/ · MIT license: https://github.com/juliangarnier/anime/blob/master/LICENSE.md
- esm.sh (no-build CDN): https://github.com/esm-dev/esm.sh
- GSAP: https://gsap.com/ · standard "no charge" license: https://gsap.com/community/standard-license/
- Motion: https://motion.dev/ · quick start: https://motion.dev/docs/quick-start
- Pyodide: https://pyodide.org/en/stable/ · repo: https://github.com/pyodide/pyodide · MPL-2.0 license: https://github.com/pyodide/pyodide/blob/main/LICENSE
