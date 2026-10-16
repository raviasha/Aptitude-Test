"""Refresh the 41 importable reasoning-bank records in latest/selection.json."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import zipfile


CHAPTER_RE = re.compile(r"new_reasoning_ch(\d{2})_.+\.zip", re.IGNORECASE)


def question_count(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        members = [
            name for name in archive.namelist()
            if name.startswith("questions/") and name.endswith(".jsonl")
        ]
        if not members:
            raise ValueError(f"No question JSONL in {path}")
        return sum(
            1
            for name in members
            for line in archive.read(name).decode("utf-8").splitlines()
            if line.strip()
        )


def refresh(root: Path, source_commit: str) -> None:
    latest = root / "question-banks" / "latest"
    chapter_dir = latest / "new-reasoning"
    paths = sorted(chapter_dir.glob("*.zip"))
    identities = []
    records = []
    for path in paths:
        match = CHAPTER_RE.fullmatch(path.name)
        if match is None:
            raise ValueError(f"Unexpected chapter filename: {path.name}")
        identities.append(int(match.group(1)))
        data = path.read_bytes()
        records.append({
            "path": f"new-reasoning/{path.name}",
            "source": f"question-banks/latest/new-reasoning/{path.name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "questions": question_count(path),
            "pages": 0,
            "source_commit": source_commit,
        })
    if identities != list(range(1, 42)):
        raise ValueError(f"Expected chapter identities 01-41, got {identities}")

    selection_path = latest / "selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    retained = [
        record for record in selection["packages"]
        if not record["path"].startswith("new-reasoning/")
    ]
    selection["selected_on"] = "2026-10-16"
    selection["packages"] = retained + records
    temporary = selection_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(selection, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(selection_path)
    print(f"selection.json: retained {len(retained)}, added {len(records)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    refresh(Path(__file__).resolve().parents[1], args.source_commit)


if __name__ == "__main__":
    main()
