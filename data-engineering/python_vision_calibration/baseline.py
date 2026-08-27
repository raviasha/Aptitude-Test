from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

from textbook_chapters import build as legacy_build

from .models import RawBaselineRecord


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = PROJECT_ROOT / "data-engineering" / "textbook_chapters"
SOURCE_BANK_PATH = PROJECT_ROOT / "question-banks" / "quantitative_aptitude_complete_extended.json"
RAW_EXTRACTOR_VERSION = "legacy-python-raw-v1"

_CANDIDATE_FIELDS = (
    "key",
    "question_text",
    "category",
    "difficulty",
    "options",
    "correct_answer",
    "explanation",
    "solution_steps",
    "option_explanations",
    "chapter",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_hash(value: object) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _review_path(chapter: int) -> Path:
    if chapter < 1:
        raise ValueError("chapter must be a positive integer.")
    path = LEGACY_ROOT / "reviews" / f"chapter-{chapter:03d}.json"
    if not path.is_file():
        raise FileNotFoundError(f"No legacy chapter review exists for chapter {chapter}: {path}")
    return path


def _raw_config(review: Mapping[str, Any]) -> dict[str, object]:
    """Keep association configuration, deliberately excluding all repair input."""
    keys = (
        "chapter",
        "chapter_name",
        "source_chapter_name",
        "printed_question_count",
        "question_pages",
        "answer_pages",
        "solution_pages",
        "source_only_question_numbers",
        "allowed_missing_solution_markers",
    )
    return {key: deepcopy(review[key]) for key in keys if key in review}


def _extractor_hash() -> str:
    return _canonical_hash({
        "version": RAW_EXTRACTOR_VERSION,
        "legacy_builder_sha256": _sha256_path(LEGACY_ROOT / "build.py"),
    })


def _association_hash(source_pdf_hash: str, role: str, value: object) -> str:
    return _canonical_hash({"source_pdf_sha256": source_pdf_hash, "role": role, "association": value})


def _validate_association(record: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    association = record.get("source_association")
    if not isinstance(association, Mapping):
        raise ValueError(f"{record.get('record_id', 'record')} is missing raw source association.")
    hashes: dict[str, tuple[str, ...]] = {}
    for role in ("question", "answer", "solution"):
        values = association.get(role)
        if not isinstance(values, (list, tuple)) or not values or not all(isinstance(item, str) and item for item in values):
            raise ValueError(f"{record.get('record_id', 'record')} is missing raw {role} association.")
        hashes[role] = tuple(values)
    return hashes


def _candidate_from_raw(record: Mapping[str, object]) -> dict[str, object]:
    candidate = {field: deepcopy(record[field]) for field in _CANDIDATE_FIELDS if field in record}
    required = ("question_text", "options", "correct_answer", "solution_steps")
    missing = [field for field in required if field not in candidate]
    if missing:
        raise ValueError(f"{record.get('record_id', record.get('key', 'record'))} is missing raw candidate fields: {missing}")
    if not isinstance(candidate["options"], dict) or not candidate["options"]:
        raise ValueError(f"{record.get('record_id', record.get('key', 'record'))} has no raw options.")
    if not isinstance(candidate["solution_steps"], list) or not candidate["solution_steps"]:
        raise ValueError(f"{record.get('record_id', record.get('key', 'record'))} has no raw solution.")
    return legacy_build.normalize_record_text(candidate)


def _records_from_fixture(
    *,
    chapter: int,
    source_pdf: Path,
    raw_records: Iterable[Mapping[str, object]],
    config_hash: str,
    extractor_hash: str,
) -> tuple[RawBaselineRecord, ...]:
    source_pdf_hash = _sha256_path(source_pdf)
    output: list[RawBaselineRecord] = []
    for position, raw in enumerate(raw_records, start=1):
        record_id = raw.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            record_id = f"ch{chapter:02d}-q{position:04d}"
        association_hashes = _validate_association({**raw, "record_id": record_id})
        candidate = _candidate_from_raw(raw)
        source_hashes = {
            "source_pdf": (source_pdf_hash,),
            "raw_extractor": (extractor_hash,),
            "config": (config_hash,),
            "question": association_hashes["question"],
            "answer": association_hashes["answer"],
            "solution": association_hashes["solution"],
        }
        fingerprint_input = {
            "record_id": record_id,
            "chapter": chapter,
            "source_hashes": source_hashes,
            "candidate": candidate,
        }
        output.append(RawBaselineRecord(
            record_id=record_id,
            chapter=chapter,
            source_hashes=source_hashes,
            candidate=candidate,
            baseline_sha256=_canonical_hash(fingerprint_input),
        ))
    return tuple(output)


def _write_baseline(records: tuple[RawBaselineRecord, ...], chapter: int, work_root: Path) -> None:
    payload = b"".join(_canonical_bytes({
        "record_id": record.record_id,
        "chapter": record.chapter,
        "source_hashes": record.source_hashes,
        "candidate": record.candidate,
        "baseline_sha256": record.baseline_sha256,
    }) + b"\n" for record in records)
    _atomic_write(work_root / "baseline" / f"chapter-{chapter:03d}.jsonl", payload)


def build_raw_baseline_from_fixture(
    *,
    chapter: int,
    source_pdf: Path,
    work_root: Path,
    raw_records: Iterable[Mapping[str, object]],
    legacy_review: Mapping[str, object],
) -> tuple[RawBaselineRecord, ...]:
    """Test seam proving review-provided replacement text is never baseline input."""
    del legacy_review
    source_pdf = source_pdf.resolve()
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    config_hash = _canonical_hash({"fixture": "raw-baseline-v1", "chapter": chapter})
    records = _records_from_fixture(
        chapter=chapter,
        source_pdf=source_pdf,
        raw_records=raw_records,
        config_hash=config_hash,
        extractor_hash=RAW_EXTRACTOR_VERSION,
    )
    _write_baseline(records, chapter, work_root.resolve())
    return records


def _raw_legacy_records(chapter: int, source_pdf: Path) -> tuple[dict[str, object], ...]:
    """Associate raw legacy records without reading any review-provided fields."""
    review = legacy_build.load_json(_review_path(chapter))
    raw_config = _raw_config(review)
    if int(raw_config.get("chapter", 0)) != chapter:
        raise ValueError(f"Chapter review/config mismatch for chapter {chapter}.")
    total = int(raw_config["printed_question_count"])
    chapter_name = str(raw_config["source_chapter_name"] if "source_chapter_name" in raw_config else raw_config["chapter_name"])
    raw = legacy_build.source_questions(SOURCE_BANK_PATH, chapter_name)
    # Do not substitute review-only records.  Their absence is evidence that raw
    # Python has no candidate and must be reported rather than repaired here.
    source_only_numbers = {int(value) for value in raw_config.get("source_only_question_numbers", [])}
    aligned = legacy_build.align_raw_records(raw, total, source_only_numbers)
    question_candidates = legacy_build._marker_candidates(
        source_pdf,
        range(int(raw_config["question_pages"][0]), int(raw_config["question_pages"][1]) + 1),
        minimum_size=8.5,
        maximum_size=11.5,
        stop_at_answers=True,
    )
    questions = legacy_build._select_markers(question_candidates, total)
    solution_candidates = legacy_build._marker_candidates(
        source_pdf,
        range(int(raw_config["solution_pages"][0]), int(raw_config["solution_pages"][1]) + 1),
        minimum_size=7.5,
        maximum_size=10.5,
        stop_at_answers=False,
    )
    solutions = legacy_build._select_markers(
        solution_candidates,
        total,
        allowed_missing={int(value) for value in raw_config.get("allowed_missing_solution_markers", [])},
    )
    answers = legacy_build.parse_answer_key(
        source_pdf,
        [int(page) for page in raw_config["answer_pages"]],
        total,
    )
    source_pdf_hash = _sha256_path(source_pdf)
    records: list[dict[str, object]] = []
    for number in range(1, total + 1):
        source_record = aligned.get(number)
        if source_record is None:
            raise ValueError(f"ch{chapter:02d}-q{number:04d} lacks a raw Python candidate/source association.")
        raw_record = deepcopy(source_record)
        if number not in questions or number not in solutions:
            raise ValueError(f"ch{chapter:02d}-q{number:04d} lacks raw source/answer/solution association.")
        solution_pages = legacy_build.inferred_solution_pages(number, solutions, total)
        question_page, question_x0, question_top = questions[number]
        solution_page, solution_x0, solution_top = solutions[number]
        raw_record.update({
            "record_id": f"ch{chapter:02d}-q{number:04d}",
            "correct_answer": answers[number],
            "source_association": {
                "question": [_association_hash(source_pdf_hash, "question", {
                    "page": question_page, "x0": question_x0, "top": question_top,
                })],
                "answer": [_association_hash(source_pdf_hash, "answer", {
                    "pages": [int(page) for page in raw_config["answer_pages"]], "number": number,
                })],
                "solution": [_association_hash(source_pdf_hash, "solution", {
                    "page": solution_page, "x0": solution_x0, "top": solution_top, "pages": solution_pages,
                })],
            },
        })
        records.append(raw_record)
    return tuple(records)


def build_raw_baseline(chapter: int, source_pdf: Path, work_root: Path) -> tuple[RawBaselineRecord, ...]:
    """Create a raw-only, immutable calibration baseline for a legacy chapter.

    Page ranges and source associations come from the review configuration, but
    all correction text, answer overrides, marker overrides, rejections, and
    source-only replacements are intentionally ignored.
    """
    source_pdf = source_pdf.resolve()
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    review = legacy_build.load_json(_review_path(chapter))
    config_hash = _canonical_hash(_raw_config(review))
    records = _records_from_fixture(
        chapter=chapter,
        source_pdf=source_pdf,
        raw_records=_raw_legacy_records(chapter, source_pdf),
        config_hash=config_hash,
        extractor_hash=_extractor_hash(),
    )
    _write_baseline(records, chapter, work_root.resolve())
    return records
