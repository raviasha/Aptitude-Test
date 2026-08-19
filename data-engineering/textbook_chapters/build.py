from __future__ import annotations

import argparse
import hashlib
import json
import re
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
            answer_tops = [float(word["top"]) for word in words if word["text"].upper() == "ANSWERS"]
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
    return _select_markers(candidates, int(review["printed_question_count"]))


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


def parse_answer_key(pdf_path: Path, answer_pages: list[int], total: int) -> dict[int, str]:
    reader = PdfReader(str(pdf_path))
    text = "\n".join(reader.pages[page - 1].extract_text() or "" for page in answer_pages)
    answers = {
        int(number): answer.upper()
        for number, answer in re.findall(r"(?<!\d)(\d+)\.\s*\(\s*([a-eA-E])\s*\)", text)
    }
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
    answers = parse_answer_key(pdf_path, [int(page) for page in review["answer_pages"]], total)
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
        record.update({
            "key": key,
            "chapter": chapter_name,
            "correct_answer": answer,
            "explanation": steps[-1],
            "solution_steps": steps,
            "option_explanations": {},
            "page_number": question_page,
            "source_page": question_page,
            "source_page_id": f"textbook-page-{question_page}",
            "source_question_number": number,
            "answer_source": {
                "policy": "textbook-answer-key-only",
                "source_pages": [int(page) for page in review["answer_pages"]],
                "source_question_number": number,
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
