#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values
from llama_parse import LlamaParse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract screenshot text with LlamaParse."
    )
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="File containing LLAMAPARSE_API_KEY.",
    )
    return parser.parse_args()


def load_api_key(env_file: Path) -> str:
    values = dotenv_values(env_file)
    key = values.get("LLAMAPARSE_API_KEY")
    if not key:
        raise SystemExit(f"LLAMAPARSE_API_KEY not found in {env_file}")
    return key


def make_parser(api_key: str) -> LlamaParse:
    return LlamaParse(
        api_key=api_key,
        result_type="markdown",
        language="en",
        high_res_ocr=True,
        premium_mode=True,
        invalidate_cache=True,
        user_prompt=(
            "Extract all visible text from this screenshot in reading order. "
            "Preserve code, terminal text, headings, labels, bullet-like structure, "
            "and any architecture or implementation notes. Do not summarize."
        ),
        verbose=False,
        show_progress=False,
    )


def find_images(input_path: Path) -> list[Path]:
    if input_path.is_file():
        files = [input_path]
    else:
        files = sorted(
            p
            for p in input_path.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic", ".heif"}
        )
    if not files:
        raise SystemExit(f"No image files found in {input_path}")
    return files


def main() -> int:
    args = parse_args()
    input_path = args.input_dir
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    api_key = load_api_key(args.env_file)
    files = find_images(input_path)

    combined: list[str] = [
        "# OCR Extraction: iCloud Photos 1392026",
        "",
        f"Source: `{input_path}`",
        f"Images parsed: {len(files)}",
        "",
    ]

    for index, image_path in enumerate(files, start=1):
        print(f"[{index}/{len(files)}] {image_path.name}", flush=True)
        try:
            # The current client can close its async event loop after a parse in
            # some environments. A per-image client keeps batch extraction stable.
            parser = make_parser(api_key)
            documents = parser.load_data(str(image_path))
            text = "\n\n".join(
                getattr(document, "text", str(document)) for document in documents
            ).strip()
        except Exception as exc:  # Keep going; preserve failures in the artifact.
            text = f"_OCR failed: {exc.__class__.__name__}: {exc}_"
            print(f"  failed: {exc}", file=sys.stderr, flush=True)

        stem = image_path.stem
        (output_dir / f"{stem}.md").write_text(text + "\n", encoding="utf-8")

        combined.extend(
            [
                f"## {image_path.name}",
                "",
                text if text else "_No text recognized._",
                "",
            ]
        )

    (output_dir / "combined.md").write_text(
        "\n".join(combined).rstrip() + "\n", encoding="utf-8"
    )
    print(f"Wrote {output_dir / 'combined.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
