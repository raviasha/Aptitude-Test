from __future__ import annotations

import argparse
import io
import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "question-banks" / "extraction-ch36-session"
DEFAULT_QUESTIONS = DEFAULT_SOURCE_DIR / "questions" / "chapter-036.jsonl"
DEFAULT_ANALYSIS = Path(__file__).with_name("page-analysis.json")
DEFAULT_TEXTBOOK_SOLUTIONS = Path(__file__).with_name("textbook-solutions.json")
DEFAULT_DIFFICULTY_REVIEW = Path(__file__).with_name("difficulty-review.json")
DEFAULT_OUTPUT = PROJECT_ROOT / "question-banks" / "ch36_tabulation_complete.zip"
EXPECTED_SOURCE_SIZE = (1700, 2200)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected an object on line {line_number} of {path}.")
            records.append(value)
    return records


def apply_difficulty_review(raw_questions: list[dict[str, Any]], review: dict[str, Any]) -> None:
    if review.get("schema_version") != 1 or review.get("policy") != "reasoning-load-reviewed-v1":
        raise ValueError("Unsupported Chapter 36 difficulty-review policy.")
    overrides = review.get("overrides")
    if not isinstance(overrides, dict):
        raise ValueError("Chapter 36 difficulty review needs an overrides object.")
    by_key = {str(question.get("key", "")): question for question in raw_questions}
    unknown = sorted(set(overrides) - set(by_key))
    if unknown:
        raise ValueError(f"Difficulty review references unknown question keys: {unknown}")
    for key, difficulty in overrides.items():
        if difficulty not in {"Easy", "Medium", "Hard"}:
            raise ValueError(f"Difficulty review has invalid value {difficulty!r} for {key}.")
        by_key[key]["difficulty"] = difficulty
    for question in raw_questions:
        if question.get("difficulty") not in {"Easy", "Medium", "Hard"}:
            raise ValueError(f"Question {question.get('key')!r} has no valid difficulty.")
        question["difficulty_source"] = review["policy"]


def source_identity(input_key: str, hallucinated_keys: set[str]) -> tuple[str, int] | None:
    if input_key in hallucinated_keys:
        return None

    first_exercise = re.fullmatch(r"ch36-q(\d{4})", input_key)
    if first_exercise:
        return "I", int(first_exercise.group(1))

    match = re.fullmatch(r"ch36-ex(\d)-q(\d{4})", input_key)
    if not match:
        raise ValueError(f"Unrecognized Chapter 36 key: {input_key}")
    source_group, number = int(match.group(1)), int(match.group(2))
    if source_group == 2 and 1 <= number <= 5:
        return "II", number
    if source_group == 2 and 11 <= number <= 15:
        # The earlier extraction numbered the real printed questions 6-10 as
        # 11-15 after hallucinating an extra graph-only set.
        return "II", number - 5
    if source_group in {3, 4, 5, 6, 7}:
        return "II", number
    raise ValueError(f"Unexpected Chapter 36 source key: {input_key}")


def question_page(exercise: str, number: int) -> int:
    if exercise == "I":
        if 1 <= number <= 5:
            return 896
        if 6 <= number <= 14:
            return 897
        if 15 <= number <= 21:
            return 898
        if 22 <= number <= 25:
            return 899
    elif exercise == "II":
        if 1 <= number <= 5:
            return 901
        if 6 <= number <= 14:
            return 902
        if 15 <= number <= 21:
            return 903
        if 22 <= number <= 31:
            return 904
        if 32 <= number <= 35:
            return 905
    raise ValueError(f"No source page for Exercise {exercise}, question {number}.")


def output_key(exercise: str, number: int) -> str:
    exercise_number = 1 if exercise == "I" else 2
    return f"ch36-ex{exercise_number}-q{number:04d}"


def expand_cross_page_groups(analysis: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    expanded: dict[tuple[str, int], dict[str, Any]] = {}
    for group in analysis.get("cross_page_groups", []):
        for number in group["question_numbers"]:
            key = (str(group["exercise"]), int(number))
            if key in expanded:
                raise ValueError(f"Duplicate cross-page declaration for Exercise {key[0]} question {key[1]}.")
            expanded[key] = group
    return expanded


def source_regions(visual: dict[str, Any]) -> list[dict[str, Any]]:
    if "source_regions" in visual:
        regions = visual["source_regions"]
        if not isinstance(regions, list) or len(regions) < 2:
            raise ValueError(f"Multi-page visual {visual['id']} needs at least two source regions.")
        return regions
    return [{
        "source_page": visual["source_page"],
        "source_file": visual["source_file"],
        "crop_box": visual["crop_box"],
        "question_content_starts_at_y": visual["question_content_starts_at_y"],
    }]


def validate_analysis(analysis: dict[str, Any], source_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    if analysis.get("schema_version") != 1:
        raise ValueError("Unsupported page-analysis schema version.")
    if tuple(analysis.get("source_image_size", [])) != EXPECTED_SOURCE_SIZE:
        raise ValueError(f"Chapter 36 source images must be {EXPECTED_SOURCE_SIZE[0]}x{EXPECTED_SOURCE_SIZE[1]} pixels.")
    if analysis.get("policy", {}).get("association") != "question-first-page-local":
        raise ValueError("The page-local question-first association policy is required.")

    visual_for_question: dict[tuple[str, int], dict[str, Any]] = {}
    visual_ids: set[str] = set()
    for visual in analysis.get("visuals", []):
        visual_id = str(visual["id"])
        if visual_id in visual_ids:
            raise ValueError(f"Duplicate visual id: {visual_id}")
        visual_ids.add(visual_id)

        regions = source_regions(visual)
        source_pages: list[int] = []
        for region in regions:
            source_page = int(region["source_page"])
            source_pages.append(source_page)
            source_file = source_dir / str(region["source_file"])
            if not source_file.is_file():
                raise FileNotFoundError(f"Missing source page image: {source_file}")
            with Image.open(source_file) as page_image:
                if page_image.size != EXPECTED_SOURCE_SIZE:
                    raise ValueError(f"Unexpected dimensions for {source_file}: {page_image.size}")

            left, top, right, bottom = (int(value) for value in region["crop_box"])
            if not (0 <= left < right <= EXPECTED_SOURCE_SIZE[0] and 0 <= top < bottom <= EXPECTED_SOURCE_SIZE[1]):
                raise ValueError(f"Invalid crop box for {visual_id}: {region['crop_box']}")
            if bottom > int(region["question_content_starts_at_y"]):
                raise ValueError(f"Crop {visual_id} overlaps the configured question-text boundary.")

        exercise = str(visual["exercise"])
        for number in visual["question_numbers"]:
            question_key = (exercise, int(number))
            if question_key in visual_for_question:
                raise ValueError(f"Exercise {exercise} question {number} is assigned to multiple visuals.")
            visual_for_question[question_key] = visual

    cross_page = expand_cross_page_groups(analysis)
    for question_key, visual in visual_for_question.items():
        page = question_page(*question_key)
        pages = [int(region["source_page"]) for region in source_regions(visual)]
        cross_page_group = cross_page.get(question_key)
        if cross_page_group is None and pages != [page]:
            raise ValueError(
                f"Exercise {question_key[0]} question {question_key[1]} must use only its page {page}, not {pages}."
            )
        if cross_page_group is not None and pages != [int(value) for value in cross_page_group["visual_pages"]]:
            raise ValueError(
                f"Cross-page visual pages for Exercise {question_key[0]} question {question_key[1]} "
                f"must be {cross_page_group['visual_pages']}, not {pages}."
            )
    return visual_for_question


def normalized_questions(
    raw_questions: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    hallucinated_keys = {str(key) for key in analysis.get("hallucinated_input_keys", [])}
    normalized: dict[tuple[str, int], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []

    for raw in raw_questions:
        input_key = str(raw.get("key", ""))
        identity = source_identity(input_key, hallucinated_keys)
        if identity is None:
            rejected.append({
                "input_key": input_key,
                "reason": "source_question_not_found",
                "detail": "The record is not printed on any audited Chapter 36 source page.",
            })
            continue
        if identity in normalized:
            raise ValueError(f"Multiple input records map to Exercise {identity[0]} question {identity[1]}.")
        normalized[identity] = deepcopy(raw)

    expected = {("I", number) for number in range(1, 26)} | {("II", number) for number in range(1, 36)}
    if set(normalized) != expected:
        missing = sorted(expected - set(normalized))
        extra = sorted(set(normalized) - expected)
        raise ValueError(f"Source question coverage mismatch. Missing={missing}; extra={extra}")
    return normalized, rejected


def textbook_solution_records(
    document: dict[str, Any],
    normalized: dict[tuple[str, int], dict[str, Any]],
    analysis: dict[str, Any],
    source_dir: Path,
) -> dict[tuple[str, int], dict[str, Any]]:
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported textbook-solution schema version.")
    if document.get("policy") != "textbook-answer-and-solution-only":
        raise ValueError("The textbook-only answer and solution policy is required.")

    source_files = {int(page): str(filename) for page, filename in analysis["source_files"].items()}
    records: dict[tuple[str, int], dict[str, Any]] = {}
    for source in document.get("records", []):
        identity = (str(source["exercise"]), int(source["question_number"]))
        if identity in records:
            raise ValueError(f"Duplicate textbook solution for Exercise {identity[0]} question {identity[1]}.")
        if identity not in normalized:
            raise ValueError(f"Unexpected textbook solution for Exercise {identity[0]} question {identity[1]}.")

        answer = str(source.get("correct_answer", "")).upper()
        if answer not in normalized[identity]["options"]:
            raise ValueError(f"Textbook answer {answer!r} is not an option for Exercise {identity[0]} question {identity[1]}.")
        expected_answer_page = 899 if identity[0] == "I" else 905
        answer_page = int(source["answer_key_page"])
        if answer_page != expected_answer_page:
            raise ValueError(
                f"Exercise {identity[0]} question {identity[1]} must use textbook answer page {expected_answer_page}."
            )

        solution_pages = [int(page) for page in source.get("solution_pages", [])]
        steps = source.get("solution_steps", [])
        if not solution_pages or not isinstance(steps, list) or not steps or not all(
            isinstance(step, str) and step.strip() for step in steps
        ):
            raise ValueError(f"Exercise {identity[0]} question {identity[1]} needs textbook solution pages and steps.")
        for page in {answer_page, *solution_pages}:
            source_filename = source_files.get(page)
            if source_filename is None or not (source_dir / source_filename).is_file():
                raise FileNotFoundError(
                    f"Textbook provenance page {page} is missing for Exercise {identity[0]} question {identity[1]}."
                )
        records[identity] = {
            "correct_answer": answer,
            "answer_key_page": answer_page,
            "solution_pages": solution_pages,
            "solution_steps": [step.strip() for step in steps],
        }

    if set(records) != set(normalized):
        missing = sorted(set(normalized) - set(records))
        extra = sorted(set(records) - set(normalized))
        raise ValueError(f"Textbook solution coverage mismatch. Missing={missing}; extra={extra}")
    return records


def build_records(
    normalized: dict[tuple[str, int], dict[str, Any]],
    analysis: dict[str, Any],
    visual_for_question: dict[tuple[str, int], dict[str, Any]],
    textbook_solutions: dict[tuple[str, int], dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cross_page = expand_cross_page_groups(analysis)
    published: list[dict[str, Any]] = []

    for identity in sorted(normalized, key=lambda item: (item[0], item[1])):
        exercise, number = identity
        raw = normalized[identity]
        source_page = question_page(exercise, number)
        visual = visual_for_question.get(identity)
        if visual is None:
            raise ValueError(f"Exercise {exercise} question {number} has no verified visual association.")

        group = cross_page.get(identity)
        visual_pages = [int(region["source_page"]) for region in source_regions(visual)]
        association_status = "cross_page_verified" if group is not None else "same_page_verified"
        textbook = textbook_solutions[identity]

        record = deepcopy(raw)
        record["key"] = output_key(exercise, number)
        record["page_number"] = source_page
        record["source_page"] = source_page
        record["source_page_id"] = f"ch36-page-{source_page}"
        record["source_exercise"] = exercise
        record["source_question_number"] = number
        record["stimulus_id"] = visual["id"]
        record["correct_answer"] = textbook["correct_answer"]
        record["explanation"] = textbook["solution_steps"][-1]
        record["solution_steps"] = textbook["solution_steps"]
        record["option_explanations"] = {}
        record["answer_source"] = {
            "policy": "textbook-answer-key-only",
            "source_page": textbook["answer_key_page"],
            "source_page_id": f"ch36-page-{textbook['answer_key_page']}",
            "source_exercise": exercise,
            "source_question_number": number,
        }
        record["solution_source"] = {
            "policy": "textbook-solution-only",
            "source_pages": textbook["solution_pages"],
            "source_page_ids": [f"ch36-page-{page}" for page in textbook["solution_pages"]],
            "source_exercise": exercise,
            "source_question_number": number,
        }
        record["image_association"] = {
            "policy": "question-first-page-aware",
            "question_page": source_page,
            "visual_source_pages": visual_pages,
            "stimulus_id": visual["id"],
            "status": association_status,
        }
        published.append(record)

    return published, rejected


def png_bytes(image: Image.Image) -> bytes:
    encoded = io.BytesIO()
    image.save(encoded, format="PNG", optimize=True)
    return encoded.getvalue()


def create_stimuli(
    analysis: dict[str, Any],
    source_dir: Path,
    published: list[dict[str, Any]],
    qa_dir: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    questions_by_stimulus: dict[str, list[str]] = {}
    for question in published:
        questions_by_stimulus.setdefault(question["stimulus_id"], []).append(question["key"])

    if qa_dir is not None:
        qa_dir.mkdir(parents=True, exist_ok=True)

    stimuli: list[dict[str, Any]] = []
    assets: dict[str, bytes] = {}
    for visual in analysis["visuals"]:
        visual_id = str(visual["id"])
        question_keys = questions_by_stimulus.get(visual_id, [])
        if not question_keys:
            continue
        asset_path = f"assets/{visual_id}.png"
        crops: list[Image.Image] = []
        regions = source_regions(visual)
        for region in regions:
            with Image.open(source_dir / region["source_file"]) as source_image:
                crops.append(
                    source_image.convert("RGB").crop(tuple(int(value) for value in region["crop_box"]))
                )
        if len(crops) == 1:
            composed = crops[0]
        else:
            gap = 24
            width = max(crop.width for crop in crops)
            height = sum(crop.height for crop in crops) + gap * (len(crops) - 1)
            composed = Image.new("RGB", (width, height), "white")
            offset_y = 0
            for crop in crops:
                composed.paste(crop, ((width - crop.width) // 2, offset_y))
                offset_y += crop.height + gap
        asset = png_bytes(composed)
        if qa_dir is not None:
            (qa_dir / f"{visual_id}.png").write_bytes(asset)
        assets[asset_path] = asset
        source_pages = [int(region["source_page"]) for region in regions]
        stimuli.append({
            "id": visual_id,
            "type": "image",
            "title": visual["title"],
            "alt_text": visual["alt_text"],
            "file": asset_path,
            "source_pages": source_pages,
            "source_page_ids": [f"ch36-page-{page}" for page in source_pages],
            "question_keys": question_keys,
            "crop_regions": [
                {
                    "source_page": int(region["source_page"]),
                    "crop_box_pixels": [int(value) for value in region["crop_box"]],
                }
                for region in regions
            ],
            "association_policy": "question-first-page-aware",
        })
    return stimuli, assets


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
    source_dir: Path,
    questions_path: Path,
    analysis_path: Path,
    solutions_path: Path,
    output_path: Path,
    qa_dir: Path | None = None,
    difficulty_review_path: Path = DEFAULT_DIFFICULTY_REVIEW,
) -> dict[str, int]:
    analysis = load_json(analysis_path)
    raw_questions = load_jsonl(questions_path)
    difficulty_review = load_json(difficulty_review_path)
    apply_difficulty_review(raw_questions, difficulty_review)
    solution_document = load_json(solutions_path)
    visual_for_question = validate_analysis(analysis, source_dir)
    normalized, rejected = normalized_questions(raw_questions, analysis)
    solutions = textbook_solution_records(solution_document, normalized, analysis, source_dir)
    published, rejected = build_records(normalized, analysis, visual_for_question, solutions, rejected)
    stimuli, assets = create_stimuli(analysis, source_dir, published, qa_dir)

    if len(raw_questions) != 65 or len(published) != 60 or len(rejected) != 5 or len(stimuli) != 12:
        raise ValueError(
            "Chapter 36 audit totals changed unexpectedly: "
            f"raw={len(raw_questions)}, published={len(published)}, rejected={len(rejected)}, stimuli={len(stimuli)}"
        )

    lineage = {
        "schema_version": 1,
        "pipeline": "data-engineering/chapter36/build.py",
        "policy": analysis["policy"],
        "source_pages_audited": sorted(int(page) for page in analysis["source_files"]),
        "published_questions": [
            {
                "question_key": question["key"],
                "source_page": question["source_page"],
                "source_exercise": question["source_exercise"],
                "source_question_number": question["source_question_number"],
                "stimulus_id": question["stimulus_id"],
                "association_status": question["image_association"]["status"],
                "visual_source_pages": question["image_association"]["visual_source_pages"],
                "answer_key_page": question["answer_source"]["source_page"],
                "solution_pages": question["solution_source"]["source_pages"],
            }
            for question in published
        ],
        "stimuli": [
            {
                "id": stimulus["id"],
                "source_pages": stimulus["source_pages"],
                "question_keys": stimulus["question_keys"],
                "crop_regions": stimulus["crop_regions"],
            }
            for stimulus in stimuli
        ],
        "rejected_questions": rejected,
    }
    manifest = {
        "format_version": 2,
        "pipeline_version": 2,
        "bank_name": "R. S. Aggarwal - Chapter 36: Tabulation (textbook-verified v4)",
        "chapter": 36,
        "chapter_name": "Tabulation",
        "section": "Data Interpretation",
        "association_policy": "question-first-page-aware",
        "answer_solution_policy": "textbook-answer-and-solution-only",
        "difficulty_policy": difficulty_review["policy"],
        "question_files": ["questions/chapter-036.jsonl"],
        "stimuli": stimuli,
        "source_pages_audited": lineage["source_pages_audited"],
        "total_source_records": len(raw_questions),
        "total_questions": len(published),
        "total_rejected_questions": len(rejected),
        "lineage_file": "metadata/lineage.json",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as archive:
        write_zip_member(archive, "manifest.json", json_bytes(manifest))
        write_zip_member(archive, "questions/chapter-036.jsonl", jsonl_bytes(published))
        write_zip_member(archive, "metadata/lineage.json", json_bytes(lineage))
        write_zip_member(archive, "metadata/rejected-questions.jsonl", jsonl_bytes(rejected))
        for asset_path, content in sorted(assets.items()):
            write_zip_member(archive, asset_path, content)

    return {
        "source_records": len(raw_questions),
        "published_questions": len(published),
        "rejected_questions": len(rejected),
        "stimuli": len(stimuli),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the page-aware Chapter 36 question bank.")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--analysis", type=Path, default=DEFAULT_ANALYSIS)
    parser.add_argument("--solutions", type=Path, default=DEFAULT_TEXTBOOK_SOLUTIONS)
    parser.add_argument("--difficulty-review", type=Path, default=DEFAULT_DIFFICULTY_REVIEW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--qa-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    arguments = parse_args()
    summary = build_package(
        source_dir=arguments.source_dir.resolve(),
        questions_path=arguments.questions.resolve(),
        analysis_path=arguments.analysis.resolve(),
        solutions_path=arguments.solutions.resolve(),
        output_path=arguments.output.resolve(),
        qa_dir=arguments.qa_dir.resolve() if arguments.qa_dir else None,
        difficulty_review_path=arguments.difficulty_review.resolve(),
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
