"""Build bound exercise-heading context routes for logical-reasoning page review."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


def _exercise_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Exercise id must be text.")
    match = re.fullmatch(r"(?:Exercise\s+)?(\d+[A-Za-z0-9]*)", " ".join(value.split()), re.I)
    if match is None:
        raise ValueError(f"Unrecognized exercise id: {value!r}")
    return f"Exercise {match.group(1).upper()}"


def _positive(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive integer.") from error
    if isinstance(value, bool) or result <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return result


def _add(routes: dict[int, dict[str, set[Any]]], page: int, context: int, exercise: str) -> None:
    entry = routes.setdefault(page, {"context_pages": set(), "exercise_ids": set()})
    entry["context_pages"].add(context)
    entry["exercise_ids"].add(exercise)


def _final(routes: Mapping[int, Mapping[str, set[Any]]]) -> dict[str, Any]:
    return {"pages": {
        str(page): {
            "context_pages": sorted(entry["context_pages"]),
            "exercise_ids": sorted(entry["exercise_ids"]),
        }
        for page, entry in sorted(routes.items())
    }}


def routes_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    routes: dict[int, dict[str, set[Any]]] = {}
    seen = False
    for row in rows:
        seen = True
        exercise = _exercise_id(row.get("exercise_id"))
        first = _positive(row.get("exercise_first"), "exercise_first")
        last = _positive(row.get("exercise_last"), "exercise_last")
        heading = _positive(row.get("question_first"), "question_first")
        question_last = _positive(row.get("question_last"), "question_last")
        answer_first = _positive(row.get("answer_first"), "answer_first")
        answer_last = _positive(row.get("answer_last"), "answer_last")
        terminal = _positive(row.get("expected_terminal_number"), "expected_terminal_number")
        if not first <= heading <= question_last <= last or not first <= answer_first <= answer_last <= last:
            raise ValueError(f"Invalid route bounds for {exercise}.")
        if terminal <= 0:
            raise ValueError(f"Invalid terminal number for {exercise}.")
        for page in range(first, last + 1):
            _add(routes, page, heading, exercise)
    if not seen:
        raise ValueError("At least one route row is required.")
    return _final(routes)


def routes_from_reviewed_manifests(manifests: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    routes: dict[int, dict[str, set[Any]]] = {}
    seen = False
    for manifest in manifests:
        seen = True
        exercise = _exercise_id(manifest.get("exercise_id"))
        provenance = manifest.get("review_provenance")
        if not isinstance(provenance, list) or not provenance:
            raise ValueError(f"Missing review provenance for {exercise}.")
        pages = sorted({_positive(item.get("page") if isinstance(item, Mapping) else None, "page")
                        for item in provenance})
        heading = pages[0]
        for page in range(pages[0], pages[-1] + 1):
            _add(routes, page, heading, exercise)
    if not seen:
        raise ValueError("At least one reviewed manifest is required.")
    return _final(routes)


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--routes-csv", type=Path)
    source.add_argument("--reviewed-manifests", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        parser.error("Output already exists; use --force to replace it.")
    if args.routes_csv:
        with args.routes_csv.open(newline="", encoding="utf-8-sig") as stream:
            payload = routes_from_rows(csv.DictReader(stream))
    else:
        paths = sorted(args.reviewed_manifests.glob("exercise-*.json"))
        payload = routes_from_reviewed_manifests(
            json.loads(path.read_text(encoding="utf-8")) for path in paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if args.force else "x"
    with args.output.open(mode, encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(json.dumps({"status": "written", "path": str(args.output),
                      "pages": len(payload["pages"])}))


if __name__ == "__main__":
    main()
