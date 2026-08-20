from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_BANK = PROJECT_ROOT / "question-banks" / "quantitative_aptitude_complete_extended.json"
DEFAULT_REVIEW = Path(__file__).with_name("reviews") / "chapter-001.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "question-banks" / "ch01_number_system_complete.zip"

DIFFICULTY_RUBRIC_VERSION = "reasoning-complexity-v1"

SOLUTION_CRITICAL_RULES = {
    "replacement_character": re.compile(r"\uFFFD"),
    "empty_layout_braces": re.compile(r"\{\s*\}"),
    "repeated_operator_fragment": re.compile(r"(?:\u00D7=|=\u00D7|==|%%|%\s+%|\u00D7\s+\u00D7)"),
    "detached_digit_array": re.compile(r"(?:\b\d\s+){5,}\d\b"),
    "concatenated_formula_numbers": re.compile(r"\b\d{4,}\s+\d{4,}\b"),
}

QUESTION_CRITICAL_RULES = {
    "replacement_character": re.compile(r"\uFFFD"),
    "operator_run": re.compile(r"(?:[+\-*/=]\s*){4,}"),
    "broken_formula_word_order": re.compile(
        r"then\s+the\s+value\s+of\s+is|value\s+of\s+is\w",
        re.IGNORECASE,
    ),
    "detached_math_array": re.compile(r"(?:\b\d{1,3}\s+){5,}\d{1,3}\b"),
    "duplicated_variable_artifact": re.compile(r"\bxx\b|\bisxx\b", re.IGNORECASE),
}


def normalize_extracted_text(value: str) -> str:
    """Repair common UTF-8-as-Windows-1252 extraction artifacts."""
    for _ in range(2):
        if not any(marker in value for marker in ("\u00c3", "\u00c2", "\u00e2", "\u00cf")):
            break
        repaired = None
        for encoding in ("cp1252", "latin-1"):
            try:
                repaired = value.encode(encoding).decode("utf-8")
                break
            except UnicodeError:
                continue
        if repaired is None:
            break
        value = repaired
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = re.sub(r"[\uE000-\uF8FF]", "", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def normalize_record_text(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_extracted_text(value)
    if isinstance(value, list):
        return [normalize_record_text(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_record_text(item) for key, item in value.items()}
    return value


def unresolved_layout_issues(record: dict[str, Any]) -> list[str]:
    """Return deterministic reasons a PDF-derived record is unsafe to publish."""
    question_material = "\n".join(
        [str(record.get("question_text", ""))]
        + [str(value) for value in (record.get("options") or {}).values()]
    )
    solution_material = "\n".join(str(step) for step in (record.get("solution_steps") or []))
    issues = [
        f"question:{name}"
        for name, pattern in QUESTION_CRITICAL_RULES.items()
        if pattern.search(question_material)
    ]
    issues.extend(
        f"solution:{name}"
        for name, pattern in SOLUTION_CRITICAL_RULES.items()
        if pattern.search(solution_material)
    )
    return issues


def derived_difficulty(record: dict[str, Any]) -> str:
    """Grade reasoning load consistently when the textbook supplies no label."""
    question = str(record.get("question_text", ""))
    steps = [str(step) for step in (record.get("solution_steps") or [])]
    solution = " ".join(steps)
    word_count = len(re.findall(r"[A-Za-z0-9]+", solution))
    operator_count = len(re.findall(r"[+\-*/=\u00D7\u00F7%]", solution))
    score = 0
    score += 3 if len(steps) >= 5 else 2 if len(steps) >= 3 else 1 if len(steps) == 2 else 0
    score += 3 if word_count >= 120 else 2 if word_count >= 70 else 1 if word_count >= 35 else 0
    score += 3 if operator_count >= 18 else 2 if operator_count >= 10 else 1 if operator_count >= 5 else 0
    if re.search(r"statement\s+[iv]|data sufficien|which.*statements|all three|neither.*nor", question, re.I):
        score += 2
    conditional_count = len(
        re.findall(r"\b(if|then|case|condition|possible|least|greatest|maximum|minimum)\b", question + " " + solution, re.I)
    )
    if conditional_count >= 3:
        score += 1
    if score <= 2:
        return "Easy"
    if score <= 6:
        return "Medium"
    return "Hard"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def source_questions(path: Path, chapter_name: str) -> list[dict[str, Any]]:
    document = load_json(path)
    records = document.get("questions")
    if not isinstance(records, list):
        raise ValueError(f"{path} does not contain a questions list.")
    return [deepcopy(record) for record in records if record.get("chapter") == chapter_name]


def _marker_candidates(
    pdf_path: Path,
    pages: range,
    *,
    minimum_size: float,
    maximum_size: float,
    stop_at_answers: bool,
) -> list[tuple[int, int, float, float]]:
    candidates: list[tuple[int, int, float, float]] = []
    with pdfplumber.open(pdf_path) as document:
        for page_number in pages:
            words = document.pages[page_number - 1].extract_words(extra_attrs=["size"])
            answer_tops = [
                float(word["top"])
                for word in words
                if word["text"].upper() == "ANSWERS" and float(word["size"]) >= 11.5
            ]
            cutoff = min(answer_tops) if stop_at_answers and answer_tops else float("inf")
            page_candidates: list[tuple[int, int, float, float]] = []
            for word in words:
                match = re.fullmatch(r"(\d+)\.", str(word["text"]))
                if not match:
                    continue
                x0 = float(word["x0"])
                top = float(word["top"])
                size = float(word["size"])
                if top >= cutoff or not (minimum_size <= size <= maximum_size):
                    continue
                if not (35 <= x0 <= 75 or 300 <= x0 <= 340):
                    continue
                page_candidates.append((int(match.group(1)), page_number, x0, top))
            page_candidates.sort(key=lambda item: (0 if item[2] < 200 else 1, item[3]))
            candidates.extend(page_candidates)
    return candidates


def _select_markers(
    candidates: list[tuple[int, int, float, float]],
    total: int,
    overrides: dict[int, tuple[int, float, float]] | None = None,
    allowed_missing: set[int] | None = None,
) -> dict[int, tuple[int, float, float]]:
    overrides = overrides or {}
    allowed_missing = allowed_missing or set()
    selected: dict[int, tuple[int, float, float]] = {}
    cursor = 0
    for number in range(1, total + 1):
        if number in overrides:
            selected[number] = overrides[number]
            continue
        found = next((index for index in range(cursor, len(candidates)) if candidates[index][0] == number), None)
        if found is None:
            if number in allowed_missing:
                continue
            raise ValueError(f"Could not locate printed question/solution marker {number}.")
        _, page, x0, top = candidates[found]
        selected[number] = (page, x0, top)
        cursor = found + 1
    return selected


def question_markers(pdf_path: Path, review: dict[str, Any]) -> dict[int, tuple[int, float, float]]:
    pages = review["question_pages"]
    candidates = _marker_candidates(
        pdf_path,
        range(int(pages[0]), int(pages[1]) + 1),
        minimum_size=8.5,
        maximum_size=11.5,
        stop_at_answers=True,
    )
    overrides = {
        int(number): (int(value["page"]), float(value["x0"]), float(value["top"]))
        for number, value in review.get("question_marker_overrides", {}).items()
    }
    return _select_markers(candidates, int(review["printed_question_count"]), overrides=overrides)


def solution_markers(pdf_path: Path, review: dict[str, Any]) -> dict[int, tuple[int, float, float]]:
    pages = review["solution_pages"]
    candidates = _marker_candidates(
        pdf_path,
        range(int(pages[0]), int(pages[1]) + 1),
        minimum_size=7.5,
        maximum_size=10.5,
        stop_at_answers=False,
    )
    overrides = {
        int(number): (int(value["page"]), float(value["x0"]), float(value["top"]))
        for number, value in review.get("solution_marker_overrides", {}).items()
    }
    allowed_missing = {int(number) for number in review.get("allowed_missing_solution_markers", [])}
    return _select_markers(
        candidates,
        int(review["printed_question_count"]),
        overrides=overrides,
        allowed_missing=allowed_missing,
    )


def parse_answer_key(
    pdf_path: Path,
    answer_pages: list[int],
    total: int,
    overrides: dict[int, str] | None = None,
) -> dict[int, str]:
    reader = PdfReader(str(pdf_path))
    text = "\n".join(reader.pages[page - 1].extract_text() or "" for page in answer_pages)
    answers = {
        int(number): answer.upper()
        for number, answer in re.findall(r"(?<!\d)(\d+)\.\s*\(\s*([a-eA-E])\s*\)", text)
    }
    answers.update({int(number): str(answer).upper() for number, answer in (overrides or {}).items()})
    expected = set(range(1, total + 1))
    if set(answers) != expected:
        raise ValueError(
            f"Answer-key coverage mismatch. Missing={sorted(expected - set(answers))}; "
            f"extra={sorted(set(answers) - expected)}"
        )
    return answers


def align_raw_records(
    raw_records: list[dict[str, Any]],
    total: int,
    source_only_numbers: set[int],
) -> dict[int, dict[str, Any]]:
    available_numbers = [number for number in range(1, total + 1) if number not in source_only_numbers]
    if len(raw_records) != len(available_numbers):
        raise ValueError(
            f"Raw/source alignment mismatch: {len(raw_records)} raw records for "
            f"{len(available_numbers)} expected source positions."
        )
    return dict(zip(available_numbers, raw_records, strict=True))


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def token_coverage(value: str, source_text: str) -> float:
    tokens = _tokens(value)
    if not tokens:
        return 0.0
    source_tokens = set(_tokens(source_text))
    return sum(token in source_tokens for token in tokens) / len(tokens)


def inferred_solution_pages(
    number: int,
    markers: dict[int, tuple[int, float, float]],
    total: int,
) -> list[int]:
    page, _, _ = markers[number]
    next_marker = next((markers[candidate] for candidate in range(number + 1, total + 1) if candidate in markers), None)
    pages = [page]
    if next_marker and next_marker[0] > page and next_marker[2] > 100:
        pages.extend(range(page + 1, next_marker[0] + 1))
    return pages


def _source_page_text(reader: PdfReader, pages: list[int]) -> str:
    return "\n".join(reader.pages[page - 1].extract_text() or "" for page in pages)


def apply_review(record: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    reviewed = deepcopy(record)
    for field in ("question_text", "options", "difficulty", "solution_steps"):
        if field in entry:
            reviewed[field] = deepcopy(entry[field])
    return reviewed


def source_only_record(number: int, entry: dict[str, Any], chapter_name: str) -> dict[str, Any]:
    required = {"question_text", "options", "solution_steps"}
    missing = required - set(entry)
    if missing:
        raise ValueError(f"Source-only question {number} is missing reviewed fields: {sorted(missing)}")
    return {
        "key": f"source-only-{number}",
        "question_text": entry["question_text"],
        "category": "Arithmetical Ability",
        "difficulty": entry.get("difficulty", "Medium"),
        "options": deepcopy(entry["options"]),
        "correct_answer": "",
        "explanation": "",
        "solution_steps": deepcopy(entry["solution_steps"]),
        "option_explanations": {},
        "chapter": chapter_name,
    }


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for record in records
    )


def write_zip_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


def build_package(
    *,
    pdf_path: Path,
    source_bank_path: Path,
    review_path: Path,
    output_path: Path,
) -> dict[str, int]:
    review = load_json(review_path)
    if review.get("schema_version") != 1:
        raise ValueError("Unsupported chapter-review schema version.")
    if review.get("policy") != "question-first-textbook-verified":
        raise ValueError("The question-first textbook-verification policy is required.")

    chapter_number = int(review["chapter"])
    chapter_name = str(review["chapter_name"])
    source_chapter_name = str(review.get("source_chapter_name", chapter_name))
    total = int(review["printed_question_count"])
    raw_records = source_questions(source_bank_path, source_chapter_name)
    source_only_numbers = {int(number) for number in review.get("source_only_question_numbers", [])}
    aligned = align_raw_records(raw_records, total, source_only_numbers)
    questions = question_markers(pdf_path, review)
    solutions = solution_markers(pdf_path, review)
    answers = parse_answer_key(
        pdf_path,
        [int(page) for page in review["answer_pages"]],
        total,
        overrides={int(number): str(answer) for number, answer in review.get("answer_key_overrides", {}).items()},
    )
    rejected_config = {int(number): value for number, value in review.get("rejections", {}).items()}
    reviewed_entries = {int(number): value for number, value in review.get("questions", {}).items()}
    reader = PdfReader(str(pdf_path))

    published: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    lineage_questions: list[dict[str, Any]] = []
    for number in range(1, total + 1):
        question_page = questions[number][0]
        raw = aligned.get(number)
        if number in rejected_config:
            rejection = rejected_config[number]
            rejected.append({
                "input_key": raw.get("key") if raw else None,
                "source_question_number": number,
                "source_page": question_page,
                "reason": rejection["reason"],
                "detail": rejection["detail"],
            })
            continue

        entry = reviewed_entries.get(number, {})
        record = source_only_record(number, entry, chapter_name) if raw is None else apply_review(raw, entry)
        record = normalize_record_text(record)
        quality_issues = unresolved_layout_issues(record)
        if quality_issues:
            rejected.append({
                "input_key": raw.get("key") if raw else record.get("key"),
                "source_question_number": number,
                "source_page": question_page,
                "reason": "unresolved_pdf_layout_artifact",
                "detail": "The question or solution still contains unsafe flattened PDF formula layout: "
                + ", ".join(quality_issues),
            })
            continue
        steps = record.get("solution_steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(step, str) and step.strip() for step in steps):
            raise ValueError(f"Question {number} has no complete textbook solution steps.")

        solution_pages = [int(page) for page in entry.get("solution_pages", [])]
        if not solution_pages:
            if number not in solutions:
                raise ValueError(f"Question {number} has no solution marker or reviewed solution pages.")
            solution_pages = inferred_solution_pages(number, solutions, total)

        if "solution_steps" not in entry:
            source_solution_text = _source_page_text(reader, solution_pages)
            coverage = token_coverage(" ".join(steps), source_solution_text)
            if coverage < float(review.get("minimum_solution_page_token_coverage", 0.7)):
                raise ValueError(
                    f"Question {number} solution does not trace to pages {solution_pages} "
                    f"(token coverage {coverage:.3f}). Add a reviewed correction or rejection."
                )

        answer = answers[number]
        options = record.get("options")
        if not isinstance(options, dict) or answer not in options:
            raise ValueError(f"Textbook answer {answer} is not present in the options for question {number}.")

        key = f"ch{chapter_number:02d}-q{number:04d}"
        steps = [step.strip() for step in steps]
        answer_review = entry.get("answer_review")
        reviewed_difficulty = entry.get("difficulty")
        difficulty = str(reviewed_difficulty) if reviewed_difficulty else derived_difficulty(record)
        if difficulty not in {"Easy", "Medium", "Hard"}:
            raise ValueError(f"Question {number} has invalid difficulty {difficulty!r}.")
        record.update({
            "key": key,
            "chapter": chapter_name,
            "difficulty": difficulty,
            "difficulty_source": "reviewed" if reviewed_difficulty else DIFFICULTY_RUBRIC_VERSION,
            "correct_answer": answer,
            "explanation": steps[-1],
            "solution_steps": steps,
            "option_explanations": {},
            "page_number": question_page,
            "source_page": question_page,
            "source_page_id": f"textbook-page-{question_page}",
            "source_question_number": number,
            "answer_source": {
                "policy": (
                    "textbook-numbered-solution-reviewed-override"
                    if answer_review
                    else "textbook-answer-key-only"
                ),
                "source_pages": [
                    int(page)
                    for page in entry.get("answer_source_pages", review["answer_pages"])
                ],
                "source_question_number": number,
                **({"review": answer_review} if answer_review else {}),
            },
            "solution_source": {
                "policy": "textbook-numbered-solution-only",
                "source_pages": solution_pages,
                "source_question_number": number,
            },
            "image_association": {
                "policy": "question-first-page-aware",
                "question_page": question_page,
                "visual_source_pages": [],
                "stimulus_id": None,
                "status": "no_standalone_visual",
            },
        })
        record.pop("stimulus_id", None)
        published.append(record)
        lineage_questions.append({
            "question_key": key,
            "source_question_number": number,
            "source_page": question_page,
            "answer_key_pages": [int(page) for page in review["answer_pages"]],
            "solution_pages": solution_pages,
            "question_review": entry.get("question_review", "page-text-validated"),
            "solution_review": entry.get("solution_review", "numbered-source-validated"),
            "image_association_status": "no_standalone_visual",
        })

    expected_published = int(review["expected_published_questions"])
    expected_rejected = int(review["expected_rejected_questions"])
    if len(published) != expected_published or len(rejected) != expected_rejected:
        raise ValueError(
            f"Reviewed totals changed: published={len(published)} (expected {expected_published}), "
            f"rejected={len(rejected)} (expected {expected_rejected})."
        )

    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    question_file = f"questions/chapter-{chapter_number:03d}.jsonl"
    lineage = {
        "schema_version": 1,
        "pipeline": "data-engineering/textbook_chapters/build.py",
        "review_file": f"reviews/chapter-{chapter_number:03d}.json",
        "policy": review["policy"],
        "source_pdf_sha256": source_hash,
        "source_pages_audited": list(range(int(review["question_pages"][0]), int(review["solution_pages"][1]) + 1)),
        "vision_reviewed_question_pages": review["vision_reviewed_question_pages"],
        "published_questions": lineage_questions,
        "rejected_questions": rejected,
    }
    manifest = {
        "format_version": 2,
        "pipeline_version": 1,
        "bank_name": f"R. S. Aggarwal - Chapter {chapter_number}: {chapter_name} (textbook-verified)",
        "chapter": chapter_number,
        "chapter_name": chapter_name,
        "section": review["section"],
        "association_policy": "question-first-page-aware",
        "answer_solution_policy": "textbook-answer-and-solution-only",
        "question_text_policy": "page-text-validated-with-vision-reviewed-corrections",
        "difficulty_policy": DIFFICULTY_RUBRIC_VERSION,
        "question_files": [question_file],
        "stimuli": [],
        "source_pages_audited": lineage["source_pages_audited"],
        "total_source_records": total,
        "total_questions": len(published),
        "total_rejected_questions": len(rejected),
        "lineage_file": "metadata/lineage.json",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as archive:
        write_zip_member(archive, "manifest.json", json_bytes(manifest))
        write_zip_member(archive, question_file, jsonl_bytes(published))
        write_zip_member(archive, "metadata/lineage.json", json_bytes(lineage))
        write_zip_member(archive, "metadata/rejected-questions.jsonl", jsonl_bytes(rejected))
    return {
        "source_records": total,
        "published_questions": len(published),
        "rejected_questions": len(rejected),
        "stimuli": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a textbook-verified chapter question bank.")
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--source-bank", type=Path, default=DEFAULT_SOURCE_BANK)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    arguments = parse_args()
    summary = build_package(
        pdf_path=arguments.source_pdf.resolve(),
        source_bank_path=arguments.source_bank.resolve(),
        review_path=arguments.review.resolve(),
        output_path=arguments.output.resolve(),
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
