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

from textbook_chapters import build as legacy_build

from .models import RawBaselineRecord


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = PROJECT_ROOT / "data-engineering" / "textbook_chapters"
RAW_EXTRACTOR_VERSION = "source-region-python-raw-v3"

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


def _source_region_digest(
    *,
    pdf_sha256: str,
    role: str,
    number: int,
    page: int,
    box: list[float],
    text: str,
    words: list[dict[str, object]],
) -> str:
    """Hash only the literal source region bound to one record and role."""
    return _canonical_hash({
        "pdf_sha256": pdf_sha256,
        "role": role,
        "number": number,
        "page": page,
        "box": box,
        "text": text,
        "words": words,
    })


def _missing_solution_source_evidence(
    *,
    pdf_sha256: str,
    number: int,
    previous: tuple[int, float, float] | None,
    following: tuple[int, float, float] | None,
) -> dict[str, object]:
    """Bind an absent numbered solution to its surrounding source anchors."""
    evidence = {
        "pdf_sha256": pdf_sha256,
        "role": "solution",
        "number": number,
        "status": "missing_numbered_solution",
        "previous_marker": list(previous) if previous is not None else None,
        "following_marker": list(following) if following is not None else None,
    }
    return {"status": "missing_numbered_solution", "sha256": _canonical_hash(evidence)}


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


def _source_marker_candidates_from_words(
    words: Iterable[Mapping[str, object]],
    *,
    page_number: int,
    minimum_size: float,
    maximum_size: float,
) -> list[tuple[int, int, float, float]]:
    """Find printed record numbers without using any reviewed marker override.

    Most textbook records begin in the two conventional margin bands.  A few
    compact worked solutions form a horizontal grid, so dotted numbers on the
    same baseline as a conventional marker are also source anchors.  Bare
    numbers are accepted only in a conventional band; this covers PDF glyph
    loss such as Chapter 1 solution 197 without admitting formula operands.
    """
    parsed: list[tuple[int, float, float, float, bool]] = []
    for word in words:
        text = str(word.get("text", ""))
        match = re.fullmatch(r"(\d+)(\.)?", text)
        if not match:
            continue
        try:
            x0 = float(word["x0"])
            top = float(word["top"])
            size = float(word["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (minimum_size <= size <= maximum_size):
            continue
        parsed.append((int(match.group(1)), x0, top, size, match.group(2) == "."))

    conventional = lambda x0: 35 <= x0 <= 75 or 300 <= x0 <= 340
    selected: list[tuple[int, int, float, float]] = []
    for number, x0, top, _size, dotted in parsed:
        if conventional(x0):
            selected.append((number, page_number, x0, top))
            continue
        if not dotted or not (35 <= x0 < 300):
            continue
        same_row = [
            candidate
            for candidate in parsed
            if candidate[4]
            and candidate[1] < 300
            and abs(candidate[2] - top) <= 1.0
        ]
        if any(conventional(candidate[1]) for candidate in same_row) and len(same_row) >= 2:
            selected.append((number, page_number, x0, top))

    unique = {(number, page, round(x0, 6), round(top, 6)) for number, page, x0, top in selected}
    return sorted(unique, key=lambda item: (0 if item[2] < 300 else 1, item[3], item[2]))


def _prefer_dotted_source_markers(
    candidates: Iterable[tuple[int, int, float, float]],
    *,
    bare_coordinates: set[tuple[int, float, float]],
) -> list[tuple[int, int, float, float]]:
    """Discard a bare numeric lookalike when the same dotted marker exists."""
    candidate_list = list(candidates)
    dotted_numbers = {
        number
        for number, page, x0, top in candidate_list
        if (page, round(x0, 6), round(top, 6)) not in bare_coordinates
    }
    return [
        candidate
        for candidate in candidate_list
        if candidate[0] not in dotted_numbers
        or (candidate[1], round(candidate[2], 6), round(candidate[3], 6)) not in bare_coordinates
    ]


def _source_marker_page_words(
    words: Iterable[Mapping[str, object]],
    *,
    stop_at_answers: bool,
) -> list[Mapping[str, object]]:
    word_list = list(words)
    if stop_at_answers:
        answer_tops = [
            float(word["top"])
            for word in word_list
            if str(word["text"]).upper() == "ANSWERS" and float(word["size"]) >= 11.5
        ]
        cutoff = min(answer_tops) if answer_tops else float("inf")
        return [word for word in word_list if float(word["top"]) < cutoff]
    solution_bottoms = [
        float(word["bottom"])
        for word in word_list
        if str(word["text"]).upper() == "SOLUTIONS" and float(word["size"]) >= 11.5
    ]
    start = max(solution_bottoms) if solution_bottoms else 0.0
    return [word for word in word_list if float(word["top"]) > start]


def _source_marker_candidates(
    source_pdf: Path,
    pages: range,
    *,
    minimum_size: float,
    maximum_size: float,
    stop_at_answers: bool,
) -> list[tuple[int, int, float, float]]:
    candidates: list[tuple[int, int, float, float]] = []
    bare_coordinates: set[tuple[int, float, float]] = set()
    with pdfplumber.open(source_pdf) as document:
        for page_number in pages:
            words = document.pages[page_number - 1].extract_words(extra_attrs=["size"])
            page_words = _source_marker_page_words(words, stop_at_answers=stop_at_answers)
            bare_coordinates.update(
                (page_number, round(float(word["x0"]), 6), round(float(word["top"]), 6))
                for word in page_words
                if re.fullmatch(r"\d+", str(word["text"]))
            )
            candidates.extend(_source_marker_candidates_from_words(
                page_words,
                page_number=page_number,
                minimum_size=minimum_size,
                maximum_size=maximum_size,
            ))
    return _prefer_dotted_source_markers(candidates, bare_coordinates=bare_coordinates)


def _region_box(
    page: pdfplumber.page.Page,
    *,
    number: int,
    marker: tuple[int, float, float],
    markers: Mapping[int, tuple[int, float, float]],
    stop_at_answers: bool,
) -> tuple[float, float, float, float]:
    page_number, x0, top = marker
    half = float(page.width / 2)
    left_column = x0 < half
    same_row = sorted(
        (
            (later_number, marker_x0)
            for later_number, (marker_page, marker_x0, marker_top) in markers.items()
            if marker_page == page_number
            and (marker_x0 < half) == left_column
            and abs(marker_top - top) <= 1.5
        ),
        key=lambda item: item[1],
    )
    horizontal_grid = len(same_row) > 1
    if horizontal_grid:
        position = next(index for index, item in enumerate(same_row) if item[0] == number)
        left = (20.0 if left_column else half - 8) if position == 0 else max(
            same_row[position][1] - 4.0,
            same_row[position - 1][1] + 4.0,
        )
        right = (
            same_row[position + 1][1] - 4.0
            if position + 1 < len(same_row)
            else (half + 8 if left_column else float(page.width - 20))
        )
        following = [
            marker_top
            for later_number, (marker_page, marker_x0, marker_top) in markers.items()
            if later_number > number
            and marker_page == page_number
            and marker_top > top + 1.5
            and left <= marker_x0 < right
        ]
        bottom = min(following) - 0.5 if following else page.height - 20
        return (left, max(0.0, top - 2), right, min(float(page.height), bottom))
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
    left = 20.0 if left_column else half - 8
    right = half + 8 if left_column else float(page.width - 20)
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
    digest = _source_region_digest(
        pdf_sha256=pdf_sha256,
        role=role,
        number=number,
        page=page_number,
        box=rounded_box,
        text=text,
        words=words,
    )
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


def _answer_evidence_from_words(
    words: Iterable[Mapping[str, object]],
    *,
    page_number: int,
    pdf_sha256: str,
    total: int,
) -> dict[int, dict[str, object]]:
    word_list = list(words)
    found: dict[int, dict[str, object]] = {}
    for index, marker in enumerate(word_list):
        marker_match = re.fullmatch(r"(\d+)\.", str(marker.get("text", "")))
        if not marker_match:
            continue
        number = int(marker_match.group(1))
        if not 1 <= number <= total:
            continue
        try:
            marker_x1 = float(marker["x1"])
            marker_top = float(marker["top"])
        except (KeyError, TypeError, ValueError):
            continue
        answer_words: list[Mapping[str, object]] | None = None
        answer = ""
        nearby = word_list[index + 1:index + 5]
        for nearby_index, candidate in enumerate(nearby):
            label_match = re.fullmatch(r"\(\s*([a-eA-E])\s*\)", str(candidate.get("text", "")))
            candidate_words = [candidate]
            if not label_match and str(candidate.get("text", "")) == "(" and nearby_index + 2 < len(nearby):
                middle = nearby[nearby_index + 1]
                closing = nearby[nearby_index + 2]
                split_match = re.fullmatch(r"([a-eA-E])", str(middle.get("text", "")))
                if split_match and str(closing.get("text", "")) == ")":
                    try:
                        contiguous = (
                            abs(float(middle["x0"]) - float(candidate["x1"])) <= 1.0
                            and abs(float(closing["x0"]) - float(middle["x1"])) <= 1.0
                            and max(
                                abs(float(middle["top"]) - marker_top),
                                abs(float(closing["top"]) - marker_top),
                            ) <= 2.0
                        )
                    except (KeyError, TypeError, ValueError):
                        contiguous = False
                    if contiguous:
                        label_match = split_match
                        candidate_words = [candidate, middle, closing]
            if not label_match:
                continue
            try:
                candidate_x0 = float(candidate["x0"])
                candidate_top = float(candidate["top"])
            except (KeyError, TypeError, ValueError):
                continue
            if marker_x1 - 1.0 <= candidate_x0 <= marker_x1 + 30.0 and abs(candidate_top - marker_top) <= 2.0:
                answer_words = candidate_words
                answer = label_match.group(1).upper()
                break
        if answer_words is None:
            continue
        literal_words = [
            {
                "text": str(value.get("text", "")),
                "x0": round(float(value["x0"]), 3),
                "top": round(float(value["top"]), 3),
                "x1": round(float(value["x1"]), 3),
                "bottom": round(float(value["bottom"]), 3),
            }
            for value in (marker, *answer_words)
        ]
        evidence = {
            "answer": answer,
            "page": page_number,
            "sha256": _canonical_hash({
                "pdf_sha256": pdf_sha256,
                "role": "answer_key",
                "number": number,
                "page": page_number,
                "literal_words": literal_words,
            }),
        }
        previous = found.get(number)
        if previous is not None and previous != evidence:
            raise ValueError(f"ambiguous answer-key source records for {number} on page {page_number}.")
        found[number] = evidence
    return found


def _answer_key_source_evidence(
    source_pdf: Path,
    *,
    answer_pages: tuple[int, ...],
    total: int,
    pdf_sha256: str,
    chapter: int,
) -> dict[int, dict[str, object]]:
    found: dict[int, dict[str, object]] = {}
    with pdfplumber.open(source_pdf) as document:
        for page_number in answer_pages:
            words = document.pages[page_number - 1].extract_words(extra_attrs=["size"])
            answer_headings = [
                float(word["bottom"])
                for word in words
                if str(word["text"]).upper() == "ANSWERS" and float(word["size"]) >= 11.5
            ]
            solution_headings = [
                float(word["top"])
                for word in words
                if str(word["text"]).upper() == "SOLUTIONS" and float(word["size"]) >= 11.5
            ]
            start = max(answer_headings) if answer_headings else 0.0
            end = min(solution_headings) if solution_headings else float("inf")
            page_found = _answer_evidence_from_words(
                [word for word in words if start < float(word["top"]) < end],
                page_number=page_number,
                pdf_sha256=pdf_sha256,
                total=total,
            )
            for number, evidence in page_found.items():
                previous = found.get(number)
                if previous is not None and previous["answer"] != evidence["answer"]:
                    raise ValueError(f"ambiguous answer-key source records for {number}.")
                found.setdefault(number, evidence)
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
        question_candidates = _source_marker_candidates(
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
        solution_candidates = _source_marker_candidates(
            source_pdf,
            range(int(raw_config["solution_pages"][0]), int(raw_config["solution_pages"][1]) + 1),
            minimum_size=7.5,
            maximum_size=10.5,
            stop_at_answers=False,
        )
        missing_solution_numbers = set(range(1, total + 1)) - {candidate[0] for candidate in solution_candidates}
        solutions = legacy_build._select_markers(
            solution_candidates,
            total,
            allowed_missing=missing_solution_numbers,
        )
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
            solution_region = None
            if number in solutions:
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
            if solution_region is not None:
                solution_text = legacy_build.normalize_extracted_text(str(solution_region["text"]))
                solution_text = re.sub(rf"^\s*{number}\s*\.\s*", "", solution_text, count=1)
                solution_hash = str(solution_region["sha256"])
            else:
                previous_number = max((value for value in solutions if value < number), default=None)
                following_number = min((value for value in solutions if value > number), default=None)
                missing_evidence = _missing_solution_source_evidence(
                    pdf_sha256=source_pdf_hash,
                    number=number,
                    previous=solutions.get(previous_number) if previous_number is not None else None,
                    following=solutions.get(following_number) if following_number is not None else None,
                )
                solution_text = ""
                solution_hash = str(missing_evidence["sha256"])
                failures = raw_record.setdefault("baseline_failures", [])
                if isinstance(failures, list):
                    failures.append("missing_solution_source")
            raw_record.update({
                "correct_answer": answers[number]["answer"],
                "solution_steps": [line.strip() for line in solution_text.splitlines() if line.strip()],
                "source_association": {
                    "question": [question_region["sha256"]],
                    "answer": [answers[number]["sha256"]],
                    "solution": [solution_hash],
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
