"""MkDocs build hooks.

Single-source the interactive lessons. The decks live in the repo-root
``lessons/`` directory (edited there, served locally from there). Rather than
keeping a second copy inside ``docs_dir`` (``articles/lessons/``) that has to be
hand-synced, this hook injects the root ``lessons/`` files into the build so
they publish at ``/lessons/...`` — one source of truth, no drift.
"""

from pathlib import Path

from mkdocs.structure.files import File


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
