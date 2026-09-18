"""Build a guarded V2 chapter configuration from reviewed exercise manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .book_map import chapters

_ROLES = ("question", "answer_key", "solution")


def _require_manifest(value: Any, chapter_id: int) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Reviewed exercise manifest must be an object.")
    if value.get("schema_version") != 1 or value.get("artifact_type") != "logical-reasoning-reviewed-exercise-markers":
        raise ValueError("Unexpected reviewed exercise manifest type.")
    if value.get("chapter_id") != chapter_id:
        raise ValueError(f"Reviewed exercise manifest does not belong to Chapter {chapter_id}.")
    if not isinstance(value.get("exercise_id"), str) or not value["exercise_id"].strip():
        raise ValueError("Reviewed exercise manifest has no exercise_id.")
    exercise_key = "".join(character for character in value["exercise_id"].casefold() if character.isalnum())
    if chapter_id == 101 and exercise_key in {"exercise1j", "1j"}:
        raise ValueError("Exercise 1J is excluded from Chapter 101 at the user's direction.")
    return value


def _numbers(manifest: Mapping[str, Any]) -> tuple[list[int], list[int]]:
    internal = manifest.get("internal_numbers")
    printed = manifest.get("printed_numbers")
    if (
        not isinstance(internal, list)
        or not isinstance(printed, list)
        or not internal
        or len(internal) != len(printed)
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in internal + printed)
    ):
        raise ValueError("Reviewed exercise manifest needs equal non-empty integer number lists.")
    if internal != list(range(internal[0], internal[0] + len(internal))):
        raise ValueError("Reviewed exercise manifest internal numbers must be contiguous.")
    return internal, printed


def _markers(
    manifest: Mapping[str, Any], numbers: Sequence[int], page_start: int, page_end: int
) -> Mapping[str, Any]:
    markers = manifest.get("marker_overrides")
    if not isinstance(markers, Mapping):
        raise ValueError("Reviewed exercise manifest has no marker_overrides.")
    expected = {str(number) for number in numbers}
    for role in _ROLES:
        value = markers.get(role)
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("Reviewed exercise manifest marker keys must exactly match its internal numbers.")
        for number, marker in value.items():
            label = f"Reviewed {role} segments for record {number}"
            segments = marker.get("segments") if isinstance(marker, Mapping) else None
            if not isinstance(segments, (list, tuple)) or not segments:
                raise ValueError(f"{label} must be a non-empty list of fully bounded segments.")
            for segment in segments:
                if not isinstance(segment, Mapping) or any(
                    isinstance(segment.get(field), bool) or not isinstance(segment.get(field), int)
                    for field in ("page", "left", "top", "right", "bottom")
                ):
                    raise ValueError(f"{label} must define integer page, left, top, right, and bottom.")
                if not page_start <= segment["page"] <= page_end:
                    raise ValueError(f"{label} page must be within chapter pages {page_start}-{page_end}.")
                if not (0 <= segment["left"] < segment["right"] and 0 <= segment["top"] < segment["bottom"]):
                    raise ValueError(f"{label} must have non-negative origins and positive width and height.")
    return markers


def build_chapter_config(
    manifests: Sequence[Mapping[str, Any]],
    *,
    source_pdf: str,
    source_pdf_sha256: str,
    shared_contexts: Mapping[str, Any] | None = None,
    chapter_id: int = 101,
    work_root: str | Path | None = None,
) -> dict[str, Any]:
    """Merge reviewed evidence for a mapped chapter, retaining legacy 101 defaults.

    An explicit work root places the candidate in its chapter-specific directory.
    """

    chapter = next((entry for entry in chapters() if entry.pipeline_id == chapter_id), None)
    if chapter is None:
        raise ValueError(f"Unknown logical-reasoning chapter: {chapter_id}.")
    if not manifests:
        raise ValueError("At least one reviewed exercise manifest is required.")
    if not isinstance(source_pdf, str) or not source_pdf:
        raise ValueError("source_pdf must be a non-empty path.")
    if not isinstance(source_pdf_sha256, str) or len(source_pdf_sha256) != 64:
        raise ValueError("source_pdf_sha256 must be a SHA-256 digest.")

    merged = {role: {} for role in _ROLES}
    record_map: dict[str, dict[str, Any]] = {}
    expected_start = 1
    for raw in manifests:
        manifest = _require_manifest(raw, chapter_id)
        internal, printed = _numbers(manifest)
        if internal[0] != expected_start:
            raise ValueError("Reviewed exercises must form one contiguous internal-number sequence.")
        markers = _markers(manifest, internal, chapter.pdf_page_start, chapter.pdf_page_end)
        for internal_number, printed_number in zip(internal, printed):
            key = str(internal_number)
            for role in _ROLES:
                merged[role][key] = markers[role][key]
            record_map[key] = {
                "exercise_id": manifest["exercise_id"],
                "printed_number": printed_number,
            }
        expected_start = internal[-1] + 1

    total = expected_start - 1
    stem = f"logical_reasoning_{chapter.section_id}_ch{chapter.chapter_number:02d}_{chapter.slug}"
    candidate_dir = (Path(work_root) / f"chapter-{chapter_id}" if work_root is not None
                     else Path("question-banks/candidates"))
    return {
        "chapter": chapter_id,
        "chapter_name": f"{chapter.section_title}: {chapter.title}",
        "bank_name": f"A Modern Approach to Logical Reasoning — Chapter {chapter.chapter_number}: {chapter.title} (vision-verified)",
        "printed_question_count": total,
        "question_numbers": [1, total],
        "question_pages": [chapter.pdf_page_start, chapter.pdf_page_end],
        "answer_pages": [chapter.pdf_page_start, chapter.pdf_page_end],
        "solution_pages": [chapter.pdf_page_start, chapter.pdf_page_end],
        "intentional_exclusions": [],
        "exercise_exclusions": {
            "Exercise 1J": "Excluded at the user's direction because the printed source has an unresolved duplicated final number."
        } if chapter_id == 101 else {},
        "known_source_issues": {
            "813": {
                "reason": "printed_question_number_duplicate",
                "detail": "Exercise 1M prints source question 85 with the label 86; the record retains the source crop and maps to the matching answer-key entry 85."
            }
        } if chapter_id == 101 and record_map.get("813") == {
            "exercise_id": "Exercise 1M", "printed_number": 85
        } else {},
        "exercise_record_map": record_map,
        "marker_overrides": merged,
        "layout_boundaries": {},
        "shared_contexts": dict(shared_contexts or {}),
        "source_pdf": source_pdf,
        "source_pdf_sha256": source_pdf_sha256.lower(),
        "source_dpi": 180,
        "work_root": Path(work_root).as_posix() if work_root is not None else "tmp/logical-reasoning-v2",
        "candidate_path": (candidate_dir / f"{stem}_v2_candidate.zip").as_posix(),
        "published_path": f"question-banks/{stem}_all_vision_text_only.zip",
        "validation_viewports": [[1024, 768], [1600, 900]],
        "render_workers": 3,
        "field_media": {},
    }
