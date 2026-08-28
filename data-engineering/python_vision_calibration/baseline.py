from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

import pdfplumber

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


def _with_missing_raw_candidate(
    *,
    record_id: str,
    source_association: Mapping[str, object],
) -> dict[str, object]:
    """Represent a source-backed record absent from the raw Python source bank."""
    return {
        "record_id": record_id,
        "question_text": "",
        "options": {},
        "correct_answer": "",
        "solution_steps": [],
        "source_association": deepcopy(dict(source_association)),
        "baseline_failures": ["missing_raw_candidate"],
    }


def _identity_tokens(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(re.findall(r"[a-z]+|\d+(?:\.\d+)?", value.casefold()))


def _candidate_identity_text(record: Mapping[str, object]) -> str:
    material = [record.get("question_text", "")]
    options = record.get("options")
    if isinstance(options, Mapping):
        material.extend(options.values())
    return " ".join(str(value) for value in material if isinstance(value, str))


def _identity_score(candidate_text: str, source_text: str) -> float:
    candidate = Counter(_identity_tokens(candidate_text))
    if sum(candidate.values()) < 4:
        return 0.0
    source = Counter(_identity_tokens(source_text))
    return sum((candidate & source).values()) / sum(candidate.values())


def _associate_raw_candidates_by_identity(
    *,
    chapter: int,
    raw_records: Iterable[Mapping[str, object]],
    identity_regions: Mapping[int, Mapping[str, object]],
) -> dict[int, dict[str, object]]:
    """Associate raw candidates only through hash-bound printed identities.

    A raw extractor may supply its own ``source_identity`` object. Otherwise a
    candidate must uniquely match one numbered source region through its own
    text anchor. Candidate order is validated after identity resolution so a
    reordered input cannot silently change which printed record it represents.
    """
    records = [deepcopy(dict(record)) for record in raw_records]
    expected = set(identity_regions)
    if expected != set(range(1, len(identity_regions) + 1)) or len(records) != len(expected):
        raise ValueError(
            f"unresolved raw/source identity for chapter {chapter}: "
            f"{len(records)} raw candidates for {len(expected)} numbered source regions."
        )

    resolved: list[tuple[int, dict[str, object]]] = []
    for raw in records:
        explicit = raw.get("source_identity")
        if explicit is not None:
            if not isinstance(explicit, Mapping):
                raise ValueError(f"unresolved raw/source identity for chapter {chapter}: malformed explicit identity.")
            number = explicit.get("number")
            declared_hash = explicit.get("question_region_sha256")
            region = identity_regions.get(number) if isinstance(number, int) and not isinstance(number, bool) else None
            if (
                region is None
                or not isinstance(declared_hash, str)
                or declared_hash != region.get("sha256")
            ):
                raise ValueError(
                    f"unresolved raw/source identity for chapter {chapter}: "
                    "explicit identity is not bound to the numbered question region."
                )
            explicit_score = _identity_score(
                _candidate_identity_text(raw),
                str(region.get("anchor_text", "")),
            )
            if explicit_score < 0.75:
                raise ValueError(
                    f"unresolved raw/source identity for chapter {chapter}: "
                    f"explicit identity does not match its candidate anchor (score={explicit_score:.3f})."
                )
        else:
            candidate_text = _candidate_identity_text(raw)
            scores = sorted(
                (
                    (_identity_score(candidate_text, str(region.get("anchor_text", ""))), number)
                    for number, region in identity_regions.items()
                ),
                reverse=True,
            )
            best_score, number = scores[0]
            second_score = scores[1][0] if len(scores) > 1 else 0.0
            if best_score < 0.75 or best_score - second_score < 0.15:
                key = raw.get("key", "candidate")
                raise ValueError(
                    f"unresolved raw/source identity for chapter {chapter}: {key!r} has no unique "
                    f"numbered source anchor (best={best_score:.3f}, margin={best_score - second_score:.3f})."
                )
        resolved.append((number, raw))

    numbers = [number for number, _ in resolved]
    if numbers != sorted(numbers):
        raise ValueError(f"unresolved raw/source identity for chapter {chapter}: reordered raw candidate identity.")
    if set(numbers) != expected or len(set(numbers)) != len(numbers):
        missing = sorted(expected - set(numbers))
        duplicates = sorted(number for number in set(numbers) if numbers.count(number) > 1)
        raise ValueError(
            f"unresolved raw/source identity for chapter {chapter}: "
            f"missing={missing}; duplicate={duplicates}."
        )
    return {number: raw for number, raw in resolved}


def _question_identity_regions(
    source_pdf: Path,
    question_markers: Mapping[int, tuple[int, float, float]],
    source_pdf_hash: str,
) -> dict[int, dict[str, object]]:
    """Extract independently hash-bound text regions for printed questions."""
    regions: dict[int, dict[str, object]] = {}
    with pdfplumber.open(source_pdf) as document:
        for number, (page_number, x0, top) in question_markers.items():
            page = document.pages[page_number - 1]
            left_column = x0 < page.width / 2
            following = [
                marker_top
                for later_number, (marker_page, marker_x0, marker_top) in question_markers.items()
                if later_number > number
                and marker_page == page_number
                and (marker_x0 < page.width / 2) == left_column
                and marker_top > top
            ]
            bottom = min(following) if following else page.height - 20
            left = 20 if left_column else page.width / 2 - 8
            right = page.width / 2 + 8 if left_column else page.width - 20
            box = (left, max(0.0, top - 2), right, min(page.height, bottom))
            anchor_text = page.crop(box).extract_text() or ""
            region_hash = _canonical_hash({
                "source_pdf_sha256": source_pdf_hash,
                "role": "question_identity",
                "number": number,
                "page": page_number,
                "box": [round(value, 3) for value in box],
                "anchor_text": anchor_text,
            })
            regions[number] = {"sha256": region_hash, "anchor_text": anchor_text}
    return regions


def _candidate_from_raw(record: Mapping[str, object]) -> dict[str, object]:
    candidate = {field: deepcopy(record[field]) for field in _CANDIDATE_FIELDS if field in record}
    supplied_failures = record.get("baseline_failures", [])
    failures = [value for value in supplied_failures if isinstance(value, str) and value] if isinstance(supplied_failures, list) else []
    defaults: dict[str, object] = {
        "question_text": "",
        "options": {},
        "correct_answer": "",
        "solution_steps": [],
    }
    for field, default in defaults.items():
        if field not in candidate:
            candidate[field] = default
            failures.append(f"missing_{field}")
    question_text = candidate["question_text"]
    if not isinstance(question_text, str):
        failures.append("invalid_question_text")
    elif not question_text.strip():
        failures.append("blank_question_text")
    options = candidate["options"]
    option_labels: set[str] = set()
    if not isinstance(options, dict):
        failures.append("invalid_options")
    elif not options:
        failures.append("missing_options")
    else:
        option_labels = {label for label in options if isinstance(label, str)}
        if option_labels not in ({"A", "B", "C", "D"}, {"A", "B", "C", "D", "E"}) or len(option_labels) != len(options):
            failures.append("invalid_option_labels")
        if any(not isinstance(value, str) or not value.strip() for value in options.values()):
            failures.append("invalid_option_text")
    answer = candidate["correct_answer"]
    if not isinstance(answer, str) or answer not in {"A", "B", "C", "D", "E"} or answer not in option_labels:
        failures.append("invalid_correct_answer")
    solution_steps = candidate["solution_steps"]
    if not isinstance(solution_steps, list):
        failures.append("invalid_solution_steps")
    elif not solution_steps:
        failures.append("missing_solution_steps")
    elif any(not isinstance(step, str) or not step.strip() for step in solution_steps):
        failures.append("invalid_solution_step")
    candidate = legacy_build.normalize_record_text(candidate)
    if failures:
        candidate["baseline_failures"] = sorted(set(failures))
    return candidate


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
        question_identity = raw.get("source_identity_sha256")
        if question_identity is not None:
            if not isinstance(question_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", question_identity):
                raise ValueError(f"{record_id} has an invalid raw question identity hash.")
            source_hashes["question_identity"] = (question_identity,)
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
    if len(raw) != total:
        raise ValueError(
            f"unresolved raw/source alignment for chapter {chapter}: "
            f"{len(raw)} raw records for {total} printed records; positional association "
            "is unsafe without independent raw candidate identifiers."
        )
    # Do not substitute review-only records.  Their absence is evidence that raw
    # Python has no candidate and must be reported rather than repaired here.
    question_candidates = legacy_build._marker_candidates(
        source_pdf,
        range(int(raw_config["question_pages"][0]), int(raw_config["question_pages"][1]) + 1),
        minimum_size=8.5,
        maximum_size=11.5,
        stop_at_answers=True,
    )
    questions = legacy_build._select_markers(question_candidates, total)
    source_pdf_hash = _sha256_path(source_pdf)
    identity_regions = _question_identity_regions(source_pdf, questions, source_pdf_hash)
    raw_by_number = _associate_raw_candidates_by_identity(
        chapter=chapter,
        raw_records=raw,
        identity_regions=identity_regions,
    )
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
    )
    answers = legacy_build.parse_answer_key(
        source_pdf,
        [int(page) for page in raw_config["answer_pages"]],
        total,
    )
    records: list[dict[str, object]] = []
    for number in range(1, total + 1):
        if number not in questions or number not in solutions:
            raise ValueError(f"ch{chapter:02d}-q{number:04d} lacks raw source/answer/solution association.")
        solution_pages = legacy_build.inferred_solution_pages(number, solutions, total)
        question_page, question_x0, question_top = questions[number]
        solution_page, solution_x0, solution_top = solutions[number]
        source_association = {
            "question": [_association_hash(source_pdf_hash, "question", {
                    "page": question_page, "x0": question_x0, "top": question_top,
            })],
            "answer": [_association_hash(source_pdf_hash, "answer", {
                    "pages": [int(page) for page in raw_config["answer_pages"]], "number": number,
            })],
            "solution": [_association_hash(source_pdf_hash, "solution", {
                    "page": solution_page, "x0": solution_x0, "top": solution_top, "pages": solution_pages,
            })],
        }
        raw_record = deepcopy(raw_by_number[number])
        raw_record.update({
            "record_id": f"ch{chapter:02d}-q{number:04d}",
            "correct_answer": answers[number],
            "source_association": source_association,
            "source_identity_sha256": identity_regions[number]["sha256"],
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
