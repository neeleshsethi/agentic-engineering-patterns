"""MkDocs build hooks.

Single-source the interactive lessons. The decks live in the repo-root
``lessons/`` directory (edited there, served locally from there). Rather than
keeping a second copy inside ``docs_dir`` (``articles/lessons/``) that has to be
hand-synced, this hook injects the root ``lessons/`` files into the build so
they publish at ``/lessons/...`` — one source of truth, no drift.
"""

import re
from pathlib import Path

import yaml
from mkdocs.structure.files import File

READING_ORDER = "<!-- reading-order -->"


def on_files(files, config):
    root = Path(config["config_file_path"]).parent
    lessons_dir = root / "lessons"
    if not lessons_dir.is_dir():
        return files

    for path in sorted(lessons_dir.rglob("*")):
        if not path.is_file():
            continue
        src_path = path.relative_to(root).as_posix()  # e.g. "lessons/index.html"
        if files.get_file_from_path(src_path) is not None:
            continue  # already provided by docs_dir; don't duplicate
        files.append(
            File(
                src_path,
                src_dir=str(root),
                dest_dir=config["site_dir"],
                use_directory_urls=config["use_directory_urls"],
            )
        )
    return files


def _front_matter(path):
    text = path.read_text()
    meta = yaml.safe_load(text.split("---", 2)[1]) if text.startswith("---") else {}
    body = text.split("---", 2)[2] if text.startswith("---") else text
    title = next(line[2:].strip() for line in body.splitlines() if line.startswith("# "))
    return title, (meta or {}).get("summary", "")


def _render_section(entries, docs_dir):
    lines = []
    for entry in entries:
        target = next(iter(entry.values())) if isinstance(entry, dict) else entry
        if not target.endswith(".md"):
            continue
        title, summary = _front_matter(Path(docs_dir) / target)
        lines.append(f"- [{title}]({target}) — {summary}" if summary else f"- [{title}]({target})")
    return lines


def on_page_markdown(markdown, page, config, files):
    """Render the home page's reading list from the nav, so the order lives in one place."""
    if READING_ORDER not in markdown:
        return markdown
    out = []
    for section in config["nav"]:
        if not isinstance(section, dict):
            continue
        (name, entries), = section.items()
        if not isinstance(entries, list):
            continue
        out += [f"### {name}", "", *_render_section(entries, config["docs_dir"]), ""]
    return markdown.replace(READING_ORDER, "\n".join(out))
