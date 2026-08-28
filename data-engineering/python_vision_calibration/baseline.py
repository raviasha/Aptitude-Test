from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

import pdfplumber
from pypdf import PdfReader

from textbook_chapters import build as legacy_build

from .models import RawBaselineRecord


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = PROJECT_ROOT / "data-engineering" / "textbook_chapters"
RAW_EXTRACTOR_VERSION = "source-region-python-raw-v2"

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
    source_identity: Mapping[str, object],
    source_association: Mapping[str, object],
) -> dict[str, object]:
    """Represent a source-backed record whose raw Python region parse is absent."""
    return {
        "record_id": record_id,
        "source_identity": deepcopy(dict(source_identity)),
        "question_text": "",
        "options": {},
        "correct_answer": "",
        "solution_steps": [],
        "source_association": deepcopy(dict(source_association)),
        "baseline_failures": ["missing_raw_candidate"],
    }


def _validated_source_identity(
    identity: object,
    *,
    chapter: int,
    record_id: str,
    expected_pdf_sha256: str | None = None,
) -> dict[str, object]:
    if not isinstance(identity, Mapping):
        raise ValueError(f"{record_id} requires an explicit source_identity.")
    required = {"number", "question_region_sha256", "pdf_sha256", "page", "box"}
    if set(identity) != required:
        raise ValueError(f"{record_id} requires an explicit source_identity with {sorted(required)}.")
    number = identity["number"]
    page = identity["page"]
    region_hash = identity["question_region_sha256"]
    pdf_hash = identity["pdf_sha256"]
    box = identity["box"]
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError(f"{record_id} has an invalid source question number.")
    if record_id != f"ch{chapter:02d}-q{number:04d}":
        raise ValueError(f"{record_id} does not match its explicit source_identity number.")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError(f"{record_id} has an invalid source page.")
    if not isinstance(region_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", region_hash):
        raise ValueError(f"{record_id} has an invalid question_region_sha256.")
    if not isinstance(pdf_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", pdf_hash):
        raise ValueError(f"{record_id} has an invalid pdf_sha256.")
    if expected_pdf_sha256 is not None and pdf_hash != expected_pdf_sha256:
        raise ValueError(f"{record_id} source_identity does not match the source PDF.")
    if (
        not isinstance(box, (list, tuple))
        or len(box) != 4
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in box)
        or not (float(box[0]) < float(box[2]) and float(box[1]) < float(box[3]))
    ):
        raise ValueError(f"{record_id} has an invalid source box.")
    return {
        "number": number,
        "question_region_sha256": region_hash,
        "pdf_sha256": pdf_hash,
        "page": page,
        "box": [round(float(value), 3) for value in box],
    }


def _candidate_from_question_region(
    *,
    chapter: int,
    chapter_name: str,
    identity: Mapping[str, object],
    region_text: str,
) -> dict[str, object]:
    """Parse one PDF region while retaining the identity created with it."""
    number = identity.get("number")
    record_id = f"ch{chapter:02d}-q{number:04d}" if isinstance(number, int) else f"ch{chapter:02d}-invalid"
    source_identity = _validated_source_identity(identity, chapter=chapter, record_id=record_id)
    text = legacy_build.normalize_extracted_text(region_text)
    text = re.sub(rf"^\s*{number}\s*\.\s*", "", text, count=1)
    labels = list(re.finditer(r"\(\s*([a-eA-E])\s*\)", text))
    failures: list[str] = []
    if "\ufffd" in text:
        failures.append("replacement_character")
    if re.search(r"(?m)^\s*[A-Za-z?]\s*$", text):
        failures.append("isolated_gutter_glyph")
    if labels:
        question_text = text[:labels[0].start()].strip()
        options: dict[str, str] = {}
        for index, match in enumerate(labels):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
            label = match.group(1).upper()
            if label in options:
                failures.append("duplicate_option_label")
            options[label] = text[match.end():end].strip()
    else:
        question_text = text.strip()
        options = {}
        failures.append("missing_options")
    raw: dict[str, object] = {
        "record_id": record_id,
        "source_identity": source_identity,
        "key": record_id,
        "question_text": question_text,
        "category": "Arithmetical Ability",
        "difficulty": "Medium",
        "options": options,
        "correct_answer": "",
        "explanation": "",
        "solution_steps": [],
        "option_explanations": {},
        "chapter": chapter_name,
    }
    if failures:
        raw["baseline_failures"] = failures
    return raw


def _region_box(
    page: pdfplumber.page.Page,
    *,
    number: int,
    marker: tuple[int, float, float],
    markers: Mapping[int, tuple[int, float, float]],
    stop_at_answers: bool,
) -> tuple[float, float, float, float]:
    page_number, x0, top = marker
    left_column = x0 < page.width / 2
    following = [
        marker_top
        for later_number, (marker_page, marker_x0, marker_top) in markers.items()
        if later_number > number
        and marker_page == page_number
        and (marker_x0 < page.width / 2) == left_column
        and marker_top > top
    ]
    bottom = min(following) - 0.5 if following else page.height - 20
    if stop_at_answers:
        answer_tops = [
            float(word["top"])
            for word in page.extract_words(extra_attrs=["size"])
            if str(word["text"]).upper() == "ANSWERS" and float(word["size"]) >= 11.5
        ]
        if answer_tops:
            bottom = min(bottom, min(answer_tops) - 0.5)
    left = 20.0 if left_column else float(page.width / 2 - 8)
    right = float(page.width / 2 + 8) if left_column else float(page.width - 20)
    return (left, max(0.0, top - 2), right, min(float(page.height), bottom))


def _source_region(
    document: pdfplumber.PDF,
    *,
    number: int,
    marker: tuple[int, float, float],
    markers: Mapping[int, tuple[int, float, float]],
    pdf_sha256: str,
    role: str,
    stop_at_answers: bool,
) -> dict[str, object]:
    page_number = marker[0]
    page = document.pages[page_number - 1]
    box = _region_box(
        page,
        number=number,
        marker=marker,
        markers=markers,
        stop_at_answers=stop_at_answers,
    )
    crop = page.crop(box)
    text = crop.extract_text(x_tolerance=2, y_tolerance=3) or ""
    words = [
        {
            "text": str(word["text"]),
            "x0": round(float(word["x0"]), 3),
            "top": round(float(word["top"]), 3),
            "x1": round(float(word["x1"]), 3),
            "bottom": round(float(word["bottom"]), 3),
        }
        for word in crop.extract_words()
    ]
    rounded_box = [round(float(value), 3) for value in box]
    digest = _canonical_hash({
        "pdf_sha256": pdf_sha256,
        "role": role,
        "number": number,
        "page": page_number,
        "box": rounded_box,
        "text": text,
        "words": words,
    })
    return {"page": page_number, "box": rounded_box, "text": text, "sha256": digest}


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
    for raw in raw_records:
        record_id = raw.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("Calibration fixtures require an explicit record_id.")
        source_identity = _validated_source_identity(
            raw.get("source_identity"),
            chapter=chapter,
            record_id=record_id,
            expected_pdf_sha256=source_pdf_hash,
        )
        association_hashes = _validate_association({**raw, "record_id": record_id})
        candidate = _candidate_from_raw(raw)
        source_hashes = {
            "source_pdf": (source_pdf_hash,),
            "raw_extractor": (extractor_hash,),
            "config": (config_hash,),
            "question": association_hashes["question"],
            "answer": association_hashes["answer"],
            "solution": association_hashes["solution"],
            "question_identity": (str(source_identity["question_region_sha256"]),),
        }
        fingerprint_input = {
            "record_id": record_id,
            "chapter": chapter,
            "source_hashes": source_hashes,
            "source_identity": source_identity,
            "candidate": candidate,
        }
        output.append(RawBaselineRecord(
            record_id=record_id,
            chapter=chapter,
            source_hashes=source_hashes,
            candidate=candidate,
            baseline_sha256=_canonical_hash(fingerprint_input),
            source_identity=source_identity,
        ))
    return tuple(output)


def _write_baseline(records: tuple[RawBaselineRecord, ...], chapter: int, work_root: Path) -> None:
    payload = b"".join(_canonical_bytes({
        "record_id": record.record_id,
        "chapter": record.chapter,
        "source_hashes": record.source_hashes,
        "source_identity": record.source_identity,
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


def _answer_key_source_evidence(
    source_pdf: Path,
    *,
    answer_pages: tuple[int, ...],
    total: int,
    pdf_sha256: str,
    chapter: int,
) -> dict[int, dict[str, object]]:
    reader = PdfReader(str(source_pdf))
    found: dict[int, dict[str, object]] = {}
    for page_number in answer_pages:
        page_text = reader.pages[page_number - 1].extract_text() or ""
        for match in re.finditer(r"(?<!\d)(\d+)\.\s*\(\s*([a-eA-E])\s*\)", page_text):
            number = int(match.group(1))
            if not 1 <= number <= total or number in found:
                continue
            answer = match.group(2).upper()
            found[number] = {
                "answer": answer,
                "page": page_number,
                "sha256": _canonical_hash({
                    "pdf_sha256": pdf_sha256,
                    "role": "answer_key",
                    "number": number,
                    "page": page_number,
                    "literal": match.group(0),
                    "page_text_sha256": _sha256_bytes(page_text.encode("utf-8")),
                }),
            }
    missing = sorted(set(range(1, total + 1)) - set(found))
    if missing:
        raise ValueError(
            f"source-association blocker for chapter {chapter}: "
            f"answer-key source is missing numbered records {missing}."
        )
    return found


def _raw_source_records(chapter: int, source_pdf: Path) -> tuple[dict[str, object], ...]:
    """Extract candidates and provenance together from numbered PDF regions."""
    review = legacy_build.load_json(_review_path(chapter))
    raw_config = _raw_config(review)
    if int(raw_config.get("chapter", 0)) != chapter:
        raise ValueError(f"Chapter review/config mismatch for chapter {chapter}.")
    total = int(raw_config["printed_question_count"])
    chapter_name = str(raw_config["chapter_name"])
    try:
        question_candidates = legacy_build._marker_candidates(
            source_pdf,
            range(int(raw_config["question_pages"][0]), int(raw_config["question_pages"][1]) + 1),
            minimum_size=8.5,
            maximum_size=11.5,
            stop_at_answers=True,
        )
        questions = legacy_build._select_markers(question_candidates, total)
    except ValueError as error:
        raise ValueError(f"source-association blocker for chapter {chapter}: question markers: {error}") from error
    source_pdf_hash = _sha256_path(source_pdf)
    try:
        solution_candidates = legacy_build._marker_candidates(
            source_pdf,
            range(int(raw_config["solution_pages"][0]), int(raw_config["solution_pages"][1]) + 1),
            minimum_size=7.5,
            maximum_size=10.5,
            stop_at_answers=False,
        )
        solutions = legacy_build._select_markers(solution_candidates, total)
    except ValueError as error:
        raise ValueError(f"source-association blocker for chapter {chapter}: solution markers: {error}") from error
    answers = _answer_key_source_evidence(
        source_pdf,
        answer_pages=tuple(int(page) for page in raw_config["answer_pages"]),
        total=total,
        pdf_sha256=source_pdf_hash,
        chapter=chapter,
    )
    records: list[dict[str, object]] = []
    with pdfplumber.open(source_pdf) as document:
        for number in range(1, total + 1):
            question_region = _source_region(
                document,
                number=number,
                marker=questions[number],
                markers=questions,
                pdf_sha256=source_pdf_hash,
                role="question",
                stop_at_answers=True,
            )
            solution_region = _source_region(
                document,
                number=number,
                marker=solutions[number],
                markers=solutions,
                pdf_sha256=source_pdf_hash,
                role="solution",
                stop_at_answers=False,
            )
            identity = {
                "number": number,
                "question_region_sha256": question_region["sha256"],
                "pdf_sha256": source_pdf_hash,
                "page": question_region["page"],
                "box": question_region["box"],
            }
            raw_record = _candidate_from_question_region(
                chapter=chapter,
                chapter_name=chapter_name,
                identity=identity,
                region_text=str(question_region["text"]),
            )
            solution_text = legacy_build.normalize_extracted_text(str(solution_region["text"]))
            solution_text = re.sub(rf"^\s*{number}\s*\.\s*", "", solution_text, count=1)
            raw_record.update({
                "correct_answer": answers[number]["answer"],
                "solution_steps": [line.strip() for line in solution_text.splitlines() if line.strip()],
                "source_association": {
                    "question": [question_region["sha256"]],
                    "answer": [answers[number]["sha256"]],
                    "solution": [solution_region["sha256"]],
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
        raw_records=_raw_source_records(chapter, source_pdf),
        config_hash=config_hash,
        extractor_hash=_extractor_hash(),
    )
    _write_baseline(records, chapter, work_root.resolve())
    return records
