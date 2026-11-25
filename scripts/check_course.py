"""Check that the course's names agree: file slug, H1, and the labels of links to each page.

Run from the repo root: python3 scripts/check_course.py [--fix]   (exit 1 on any mismatch)

A link to a course page may use a prose label ("the worker lock"), a short label that names
the page's number ("Step 3"), or the page's full H1. A label that names a page title in any
other form (old titles, a bare topic, different capitalisation) must be the full H1; --fix
rewrites those.
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ARTICLES = ROOT / "articles"

SLUG_PREFIX = [
    (re.compile(r"^ch(\d+)-"), lambda m: f"Chapter {m[1]} · "),
    (re.compile(r"^step-(\d+[a-z]?)-"), lambda m: f"Step {m[1]} · "),
    (re.compile(r"^capstone-(\d+)-"), lambda m: f"Capstone {m[1]} · "),
    (re.compile(r"^bridge-"), lambda _: "Bridge · "),
]
TITLED_LABEL = re.compile(r"^(Chapter|Step|Capstone|Bridge|Deep dive)\b", re.I)
SHORT_LABEL = re.compile(r"^(Chapter|Step|Capstone) (\d+[a-z]?)$")
OLD_TITLES = [
    "Planning and Human Approval", "Human-in-the-Loop Plan Approval", "HITL Approval", "Identifiers",
    "Distributed Locks", "Nine Silent Failures", "LangGraph State", "Context Injection",
    "Clarifications", "Queue and worker execution", "State and Checkpoints", "Reactive Mode Prompt",
    "Plan Lifecycle", "Orchestrator Prompt", "The Orchestrator Prompt", "Code Components and Organization",
    "End-to-End Architecture and Orchestrator Flow", "Problem, Design Decisions, and Implementation Plan",
    "Carryover and the Data Manifest", "When the Score Passes and the Work Fails",
    "Streaming and background work", "Durable Async Agent Runs", "SSE Cancellation",
]
NEXT_LINE = re.compile(r"^\*{1,2}Next:\*{0,2}\s*\[[^\]]+\]\(([^)#]+\.md)")
LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)#\s]+\.md)(#[^)]*)?\)")


def nav_pages(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from nav_pages(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from nav_pages(value)


def h1(path: Path) -> str:
    return next((line[2:].strip() for line in path.read_text().splitlines() if line.startswith("# ")), "")


def main() -> int:
    config = yaml.safe_load(
        re.sub(r"!!python/name:\S+", "''", (ROOT / "mkdocs.yml").read_text())
    )
    pages = [p for p in nav_pages(config["nav"]) if p.endswith(".md")]
    errors = []

    for page in pages:
        path = ARTICLES / page
        if not path.exists():
            errors.append(f"nav lists missing page {page}")
            continue
        for pattern, prefix in SLUG_PREFIX:
            m = pattern.match(path.name)
            if m and not h1(path).startswith(prefix(m)):
                errors.append(f"{page}: H1 '{h1(path)}' should start with '{prefix(m)}'")

    titles = {(ARTICLES / p).resolve(): h1(ARTICLES / p) for p in pages if (ARTICLES / p).exists()}

    for page, following in zip(pages, pages[1:] + [None]):
        for line in (ARTICLES / page).read_text().splitlines():
            m = NEXT_LINE.match(line)
            if m and (ARTICLES / page).parent.joinpath(m[1]).resolve() != (ARTICLES / str(following)).resolve():
                errors.append(f"{page}: 'Next:' points at {m[1]}, but the nav's next page is {following}")
    topics = {t.split(" · ", 1)[-1].lower() for t in titles.values()} | {t.lower() for t in OLD_TITLES}
    fix = "--fix" in sys.argv

    for source in ARTICLES.rglob("*.md"):
        text = source.read_text()

        def check(m):
            label, target, anchor = m.group(1), m.group(2), m.group(3) or ""
            title = titles.get((source.parent / target).resolve())
            if title is None:
                return m.group(0)
            short = SHORT_LABEL.match(label)
            if short:
                if not title.startswith(f"{short[1]} {short[2]} · "):
                    errors.append(f"{source.relative_to(ROOT)}: link '{label}' points at '{title}'")
                return m.group(0)
            titled = TITLED_LABEL.match(label) or label.strip("`*").lower() in topics
            if titled and label != title:
                errors.append(f"{source.relative_to(ROOT)}: link '{label}' should read '{title}'")
                return f"[{title}]({target}{anchor})"
            return m.group(0)

        new = LINK.sub(check, text)
        if fix and new != text:
            source.write_text(new)

    for e in errors:
        print(e)
    print(f"{len(pages)} pages checked, {len(errors)} problems" + (" (label fixes applied)" if fix else ""))
    return 1 if errors and not fix else 0


if __name__ == "__main__":
    sys.exit(main())
