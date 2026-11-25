"""One-shot codemod: rename course pages so file slug, nav label and H1 agree.

Run from the repo root: python3 scripts/restructure_course.py
Safe to rerun: pages already moved are skipped, link rewrites are idempotent.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTICLES = ROOT / "articles"

# old file -> (new file, H1). Reading order lives in mkdocs.yml nav.
PAGES = {
    "01-problem-design-implementation-plan.md": ("ch1-problem-and-plan.md", "Chapter 1 · Problem, design, and implementation plan"),
    "02-end-to-end-architecture-and-orchestrator-flow.md": ("ch2-architecture-and-flow.md", "Chapter 2 · End-to-end architecture and orchestrator flow"),
    "02-state-and-checkpoints.md": ("step-1-state-and-checkpoints.md", "Step 1 · State and checkpoints"),
    "03-context-injection.md": ("step-2-context-injection.md", "Step 2 · Context injection"),
    "04-planning-and-human-approval.md": ("step-3-planning-and-human-approval.md", "Step 3 · Planning and human approval"),
    "05-identifiers.md": ("step-4-identifiers.md", "Step 4 · Identifiers that hold a run together"),
    "06-streaming-and-background-work.md": ("step-5-streaming-and-background-work.md", "Step 5 · Streaming and background work"),
    "07-distributed-locks.md": ("step-6-distributed-locks.md", "Step 6 · Distributed locks"),
    "08-queue-and-worker-execution.md": ("step-7-queue-and-worker-execution.md", "Step 7 · Queue and worker execution"),
    "09e-event-feed-and-idempotency.md": ("step-7a-event-feed-and-idempotency.md", "Step 7a · The event feed and idempotency"),
    "09-orchestrator-prompt.md": ("step-8-orchestrator-prompt.md", "Step 8 · Orchestrator prompt"),
    "09d-reactive-mode-prompt.md": ("step-8a-reactive-mode-prompt.md", "Step 8a · Reactive mode and its prompt"),
    "09a-clarifications.md": ("step-8b-clarifications.md", "Step 8b · Clarifications"),
    "09c-plan-lifecycle.md": ("step-8c-plan-lifecycle.md", "Step 8c · Plan lifecycle"),
    "09f-carryover-and-the-data-manifest.md": ("step-8d-carryover-and-data-manifest.md", "Step 8d · Carryover and the data manifest"),
    "09b-code-components-and-organization.md": ("step-8e-code-components-and-organization.md", "Step 8e · Code components and organization"),
    "10-nine-silent-failures.md": ("capstone-1-nine-silent-failures.md", "Capstone 1 · Nine silent failures in one agent"),
    "11-when-the-score-passes.md": ("capstone-2-when-the-score-passes.md", "Capstone 2 · When the score passes and the work fails"),
    "00-glossary.md": ("glossary.md", "Glossary"),
}

LINK = re.compile(r"(\]\(|href=\")((?:\.\.?/)*)(?:articles/)?((?:\d{2}[a-z]?)-[a-z0-9-]+)\.md")


def move_pages():
    for old, (new, _) in PAGES.items():
        if (ARTICLES / old).exists():
            subprocess.run(["git", "mv", f"articles/{old}", f"articles/{new}"], cwd=ROOT, check=True)


def set_heading(path: Path, title: str):
    lines = path.read_text().split("\n")
    assert lines[0].startswith("# "), path
    lines[0] = f"# {title}"
    path.write_text("\n".join(lines))


def rewrite_links(path: Path):
    text = path.read_text()

    def sub(m):
        prefix, dots, stem = m.groups()
        target = PAGES.get(f"{stem}.md")
        if not target:
            return m.group(0)
        new_stem = target[0][:-3]
        if path.suffix == ".html":
            # Lessons publish at /lessons/; articles publish as directory URLs.
            return f"{prefix}../{new_stem}/"
        if path.parent == ROOT:
            return f"{prefix}articles/{new_stem}.md"
        return f"{prefix}{dots}{new_stem}.md"

    new = LINK.sub(sub, text)
    if new != text:
        path.write_text(new)


def main():
    move_pages()
    for new, title in PAGES.values():
        set_heading(ARTICLES / new, title)
    targets = list(ARTICLES.rglob("*.md")) + list((ROOT / "lessons").glob("*.html"))
    targets += [ROOT / "README.md", *(ROOT / "examples").rglob("*.md"), *(ROOT / "docs").rglob("*.md")]
    for path in targets:
        rewrite_links(path)


if __name__ == "__main__":
    main()
