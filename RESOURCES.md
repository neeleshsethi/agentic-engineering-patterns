# Production Deep Agent Engineering Resources

## Knowledge

- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)
  Primary framework reference for checkpoints, interrupts, state, streaming, and graph execution. Use for public API behavior.
- [AWS SQS FIFO queue documentation](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues.html)
  Primary source for FIFO ordering, message groups, visibility timeout, and retry behavior.
- [AWS DynamoDB conditional writes documentation](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
  Primary source for conditional writes used by locks, claims, and ownership fencing.
- [MDN Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
  Browser-facing reference for EventSource and `Last-Event-ID` reconnection behavior.
- `data/extracted/1392026-llamaparse/key-findings.md`
  Local OCR distillation from the screenshot batch. Use for production-specific architecture facts.
- `data/extracted/1392026-llamaparse/combined.md`
  Full local OCR extraction. Use when a lesson needs exact details beyond the distillation.

## Wisdom (Communities)

- Project source notes under `articles/source-notes/`
  Local design history and scar tissue. Use for advanced explanations after the beginner lesson has landed.

## Gaps

- Add official LangGraph links for the exact `put_writes` / pending resume mechanics if these lessons are later published with external citations.

---

## Interactive Lesson Tooling Research (2026-09-13)

Research question: what tools and hosting options best serve animated, interactive web-based lessons for this intern-facing production deep-agent course? Key constraints: (1) self-contained HTML at runtime — no server, (2) GitHub Pages hostable, (3) minimal build pipeline, (4) quiz widgets with immediate JS feedback, (5) step-through async sequence flows (plan → pause → approve → SQS → worker).

### Animated Diagram Tools

- [Manim Community — Output Settings](https://docs.manim.community/en/stable/tutorials/output_and_config.html)
  Manim renders to `.mp4` (default), `.webm`, `.gif`, and `.png` (last frame only). There is **no HTML or SVG output format**. The `.webm` flag (`--format=webm`) enables transparency. Embed story for a lesson: render to `.webm`, drop the file next to the HTML, use a `<video>` tag. Cannot animate inline SVG; the animation must be pre-rendered. Source: Manim Community v0.20.1 docs.

- [Motion Canvas — Image Sequence Export](https://motioncanvas.io/docs/rendering/image-sequence/)
  Motion Canvas renders to PNG/JPEG/WebP image sequences or to video via FFmpeg. Rendering **requires the dev server to be running** (it drives the render loop through the browser). Output files land in `/output` in the project directory. For lessons: pre-render to `.mp4`/`.webm` and embed with `<video>`. No interactive runtime; purely a production tool. Source: official Motion Canvas docs.

- [Reveal.js — Fragments](https://revealjs.com/fragments/)
  Reveal.js runs as a **self-contained HTML file** with no server (basic setup; `index.html` opened directly in a browser). Every `.fragment` element steps through sequentially before the next slide advances, controlled by `data-fragment-index`. Built-in effects: `fade-up`, `fade-down`, `fade-left`, `fade-right`, `grow`, `shrink`, `highlight-red/green/blue`, `current-visible`, etc. Custom effects via CSS `.fragment.effectname.visible`. Dispatches `fragmentshown`/`fragmenthidden` events for programmatic hooks. **Best fit for step-through sequence flows** (one arrow per keypress). External Markdown requires a local server; inline HTML Markdown does not. Source: revealjs.com/installation and revealjs.com/fragments.

- [D3.js — Getting Started](https://d3js.org/getting-started)
  D3 v7 runs in a single `<script src="cdn">` HTML file with zero build step — the official docs show a complete chart in a bare HTML file. The minified UMD bundle (`d3.v7.min.js`) must still be loaded from CDN or locally bundled. D3 has no built-in sequence-diagram layout; you build lanes, lifelines, and arrows manually with SVG `<line>`/`<path>` + `.transition()`. Effort is high (~200–400 LoC for a polished animated sequence diagram). For this course's step-through use-case, Reveal.js + static SVG frames is lower effort. Source: d3js.org/getting-started.

- [Rive — Web (JS) Runtime](https://rive.app/docs/runtimes/web)
  Rive's web runtime (`@rive-app/webgl2`, `@rive-app/canvas`) is available via CDN script tag. It **can** run in a self-contained HTML file. The runtime makes an additional network request for a `.wasm` file; to be fully offline/self-contained you must host the WASM locally (documented via the "preloading WASM" guide at help.rive.app/runtimes/overview/web-js/preloading-wasm). A lighter variant `@rive-app/canvas-lite` exists. Rive animations are authored in the proprietary Rive editor (web app) and exported as `.riv` files. Total overhead is the JS runtime + the WASM + the `.riv` asset. Good fit for polished looping diagrams; not well-suited for step-through narrative control without building state machine hooks. Source: rive.app/docs/runtimes/web; help.rive.app/runtimes/overview/web-js/preloading-wasm.

- [Lottie / lottie-web](https://github.com/airbnb/lottie-web)
  `lottie-web` is **237.5 kB minified / ~60 kB gzipped** (Bundlephobia: bundlephobia.com/package/lottie-web). The LottieFiles `@lottiefiles/lottie-player` web component is larger: ~334 kB / 84 kB gzipped (GitHub issue #166). The newer `dotlottie-web` (Rust + WASM) is available as an alternative. Animations are authored in Adobe After Effects + the Bodymovin plugin, or in the LottieFiles online editor, and exported as JSON. Playback is via `<script src="cdn">` in a single HTML file. `lottie.play()` / `.goToAndStop(frame)` allow programmatic step control. Good for pre-baked icon-scale animations; less practical for large sequence diagrams. Source: github.com/airbnb/lottie-web#1184; github.com/LottieFiles/lottie-player#166; bundlephobia.com/package/lottie-web.

- [Mermaid.js — Sequence Diagrams](https://mermaid.js.org/syntax/sequenceDiagram.html)
  Mermaid renders text-defined diagrams to static SVG via a `<script>` tag; no server required. The full library is **~2.7 MB minified** in v10.x (bundlephobia.com/package/mermaid). A `@mermaid-js/tiny` subset exists for smaller bundles. Sequence diagrams render as static SVG — **no built-in step-through animation**. The workaround is to export partial diagrams and use Reveal.js fragments (per mermaid-js discussion #4199). Natively supported in GitHub markdown, Notion, and Obsidian, so lesson source files render in-editor without a build. Source: mermaid.js.org/syntax/sequenceDiagram.html; github.com/orgs/mermaid-js/discussions/4199; bundlephobia.com/package/mermaid.

### Interactive Lesson Hosting

- [Scrimba](https://scrimba.com/)
  Scrimba is a **SaaS-only** platform (no self-hosting). It is code-centric: scrims record editor + browser interactions. Non-code slides (architecture diagrams, prose) are supported as static image slides inside a scrim. There is no public embed API for external static sites. Not suitable for GitHub Pages self-hosting. Source: scrimba.com/our-pricing; embed.ly/provider/scrimba.

- [Observable Framework — Deploying](https://observablehq.com/framework/deploying)
  `npm run build` produces a `dist/` directory of **fully static HTML/JS/CSS files** — no server process required at runtime. Deploy to GitHub Pages, Netlify, S3, or any static host. A GitHub Actions workflow (`observablehq/framework` discussion #1030) is the canonical path. Supports interactive D3/Plot/Inputs cells, data loaders (run once at build time), and JavaScript notebooks. Observable Cloud hosting was deprecated April 2025. The build pipeline is Node-based; requires `npm ci && npm run build`. For this course: strong data-viz story, but requires a Node build step; output is self-contained static files. Source: observablehq.com/framework/deploying; observablehq.com/release-notes/2024-03-05-framework-1-1-update.

- [Astro + MDX — GitHub Pages Deploy](https://docs.astro.build/en/guides/deploy/github/)
  Astro with `output: 'static'` (the default) pre-renders every page to plain HTML at build time. GitHub Actions deployment is the documented canonical path (`astro.config.mjs` → `site` + `base` → push to main → CI builds and publishes). Interactive React/Vue/Svelte "islands" are supported via `client:load` directives — only the island's JS ships to the browser. MDX files import React components directly. Build pipeline: `npm run build` → `dist/`. Suitable for mixed prose + animated component lessons with zero server at runtime. Source: docs.astro.build/en/guides/deploy/github; docs.astro.build/en/guides/integrations-guide/mdx.

- Plain HTML + Vanilla JS
  A complete quiz widget requires ~50–100 LoC of vanilla JS: `querySelectorAll`, `addEventListener('click')`, add a `.correct`/`.incorrect` CSS class, update a score counter. No dependencies, no build step, works offline. The `/teach` project (cited in a 2024 GitHub discussion) implements exactly this pattern: `assets/quiz.js`, plain markup, progressive enhancement — answers readable without JS, feedback added with JS. For step-through sequences: Reveal.js fragments (above) or `CSS keyframes` + a "Next" button advancing a `data-step` attribute on a parent element. Source: github.com/AymanKastali/researcher/issues/5; revealjs.com/fragments.

### All-in-One Platforms

- [Docusaurus — MDX and React](https://docusaurus.io/docs/markdown-features/react)
  Docusaurus v3 MDX compiles to React components. Custom React components can be imported directly into `.mdx` files or registered globally via `@theme/MDXComponents`. `npm run build` produces a `build/` directory of **fully static HTML** — "a Docusaurus site can generally work without JavaScript!" (docs.docusaurus.io/docs/deployment). GitHub Pages deployment is documented with `organizationName`/`projectName` config. Interactive animations can be embedded as React islands. Build pipeline required (Node + webpack/Rsbuild). Source: docusaurus.io/docs/markdown-features/react; docusaurus.io/docs/deployment.

- [Mintlify — Custom React Components](https://www.mintlify.com/docs/customize/react-components)
  Mintlify is a **SaaS-only** documentation platform. Custom React components are supported in MDX files using pre-injected hooks (`useState`, `useEffect`, `useRef`, etc.) — no imports needed. However, **third-party packages cannot be imported** (e.g. no Framer Motion, no D3, no Lottie). `React.lazy` and dynamic `import()` are not supported. Animation is limited to inline CSS or native browser CSS animation APIs. Not self-hostable; not deployable to GitHub Pages independently. Source: mintlify.com/docs/customize/react-components.

- [GitBook — Custom HTML/CSS/JS](https://docs.gitbook.com/developers/guides/interactivity)
  GitBook explicitly does **not** support injecting arbitrary JavaScript into pages. From the official docs: "building integrations that inject JavaScript into a space or page are not possible to build at this time." iframes are not supported due to content security policy. Interactivity is limited to ContentKit components built via the GitBook integration API (server-side event-driven UI, not client-side animation). Not suitable for animated sequence diagrams or quiz widgets. Source: docs.gitbook.com/help-center/.../can-i-edit-html-css-js-or-other-custom-code-on-gitbook; docs.gitbook.com/developers/guides/interactivity.

### What Production Teaching Sites Actually Use (2024–2025)

- **LangGraph docs** (langchain-ai.github.io/langgraph): Static MkDocs site (Material theme). Diagrams are pre-rendered Mermaid SVGs or static PNG images. No client-side animation. Sequence flow illustrations are PNG screenshots from the LangSmith trace UI.
- **Anthropic docs** (docs.anthropic.com): Static site; prose + code blocks. No animated diagrams. Claude's interactive chart generation (announced March 2026) is a chat-UI feature, not a docs-site feature.
- **CS50 / Harvard** (cs50.harvard.edu): Lecture notes are static HTML generated from Markdown. Diagrams are embedded PNG/GIF images. Quizzes use a custom Django-backed grader, not standalone HTML.
- **The dominant pattern in 2024–2025 technical docs**: Mermaid or pre-rendered PNG/SVG diagrams in MkDocs/Docusaurus, with Reveal.js for presentation-style step-through content.

### Recommendation for This Repo

Given the constraints (self-contained HTML, GitHub Pages, minimal pipeline, quiz feedback, async sequence flows):

1. **Primary: Reveal.js + inline Mermaid** — Open `index.html` in a browser, no server. Each sequence step is a Reveal.js fragment; Mermaid renders the static SVG inline. Total overhead: Reveal.js (~300 kB) + Mermaid (~2.7 MB, lazy-loadable). No build step for authoring.
2. **Build-pipeline path: Astro + MDX + React islands** — Full static HTML output, GitHub Pages CI, React animation components per lesson. Best if the course grows into a proper site with navigation, search, and versioning.
3. **Sequence animation: Reveal.js fragments are the lowest-effort path** for plan → pause → approve → SQS → worker flows. Each arrow is a fragment; the diagram is a static Mermaid SVG or hand-drawn SVG.
4. **Quiz widgets: vanilla JS, zero dependencies** — 50–100 LoC, works offline, no build step.
5. **Avoid**: GitBook (no JS), Mintlify (SaaS, no third-party libs), Scrimba (SaaS, code-centric), Manim/Motion Canvas (pre-render pipeline only, no interactive runtime).
