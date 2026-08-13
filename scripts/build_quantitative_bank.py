"""Categorize the legacy quantitative bank and build its v2 ZIP package.

The chapter boundaries come from the numbered chapter headings in the source
material. The script is deterministic so the categorized JSON and package can
be rebuilt whenever the source bank is corrected.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "question-banks" / "quantitative_aptitude_complete_extended.json"
DEFAULT_PACKAGE = ROOT / "question-banks" / "quantitative_aptitude_categorized_v2.zip"

CHAPTER_RANGES = (
    (1, 378, "Arithmetical Ability", "Number System"),
    (379, 508, "Arithmetical Ability", "HCF and LCM"),
    (509, 714, "Arithmetical Ability", "Decimal Fractions"),
    (715, 1256, "Arithmetical Ability", "Simplification"),
    (1257, 1443, "Arithmetical Ability", "Square Roots and Cube Roots"),
    (1444, 1514, "Arithmetical Ability", "Average"),
    (1515, 1652, "Arithmetical Ability", "Problems on Numbers"),
    (1653, 1717, "Arithmetical Ability", "Problems on Ages"),
    (1718, 1839, "Arithmetical Ability", "Surds and Indices"),
    (1840, 1930, "Arithmetical Ability", "Logarithms"),
    (1931, 2327, "Arithmetical Ability", "Percentage"),
    (2328, 2629, "Arithmetical Ability", "Profit and Loss"),
    (2630, 2881, "Arithmetical Ability", "Ratio and Proportion"),
    (2882, 2953, "Arithmetical Ability", "Partnership"),
    (2954, 3035, "Arithmetical Ability", "Chain Rule"),
    (3036, 3088, "Arithmetical Ability", "Pipes and Cisterns"),
    (3089, 3230, "Arithmetical Ability", "Time and Work"),
    (3231, 3414, "Arithmetical Ability", "Time and Distance"),
    (3415, 3454, "Arithmetical Ability", "Boats and Streams"),
    (3455, 3533, "Arithmetical Ability", "Problems on Trains"),
    (3534, 3559, "Arithmetical Ability", "Alligation or Mixture"),
    (3560, 3666, "Arithmetical Ability", "Simple Interest"),
    (3667, 3747, "Arithmetical Ability", "Compound Interest"),
    (3748, 4171, "Arithmetical Ability", "Area"),
    (4172, 4478, "Arithmetical Ability", "Volume and Surface Area"),
    (4479, 4502, "Arithmetical Ability", "Races and Games"),
    (4503, 4520, "Arithmetical Ability", "Calendar"),
    (4521, 4574, "Arithmetical Ability", "Clocks"),
    (4575, 4602, "Arithmetical Ability", "Stocks and Shares"),
    (4603, 4650, "Arithmetical Ability", "Permutation and Combination"),
    (4651, 4700, "Arithmetical Ability", "Probability"),
    (4701, 4720, "Arithmetical Ability", "True Discount"),
    (4721, 4733, "Arithmetical Ability", "Banker's Discount"),
    (4734, 4751, "Arithmetical Ability", "Heights and Distances"),
    (4752, 4847, "Arithmetical Ability", "Odd Man Out and Series"),
    (4848, 4952, "Data Interpretation", "Tabulation"),
    (4953, 5013, "Data Interpretation", "Bar Graphs"),
    (5014, 5084, "Data Interpretation", "Pie Charts"),
    (5085, 5151, "Data Interpretation", "Line Graphs"),
)


def taxonomy_for(number: int) -> tuple[str, str]:
    for start, end, category, chapter in CHAPTER_RANGES:
        if start <= number <= end:
            return category, chapter
    raise ValueError(f"Question number {number} is outside the mapped source range.")


def load_and_categorize(source: Path) -> dict:
    document = json.loads(source.read_text(encoding="utf-8"))
    questions = document.get("questions") if isinstance(document, dict) else None
    if not isinstance(questions, list) or len(questions) != 5151:
        raise ValueError("The quantitative source must contain exactly 5,151 questions.")
    for number, question in enumerate(questions, start=1):
        expected_key = f"qa-{number:04d}"
        if not isinstance(question, dict) or question.get("key") != expected_key:
            raise ValueError(f"Expected {expected_key!r} at question position {number}.")
        category, chapter = taxonomy_for(number)
        question["category"] = category
        question["chapter"] = chapter
    document["bank_name"] = "R. S. Aggarwal Quantitative Aptitude (2017) - Categorized"
    return document


def write_json_atomic(destination: Path, document: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=destination.parent, suffix=".tmp"
    ) as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(destination)


def normalize_for_pdf_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def attach_di_pdf_stimuli(document: dict, source_pdf: Path) -> list[dict]:
    """Render PDF pages containing each DI question and link them as shared stimuli.

    The supplied R. S. Aggarwal PDF has vector charts/tables rather than image
    objects. Rendering the source exercise pages preserves the original visual
    data, labels, and legends without copying answer/solution pages.
    """
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError(
            "DI PDF rendering needs pypdf and pypdfium2. Use the bundled Codex Python runtime."
        ) from error
    if not source_pdf.is_file():
        raise FileNotFoundError(f"DI source PDF was not found: {source_pdf}")

    reader = PdfReader(str(source_pdf))
    page_text = {
        page_number: normalize_for_pdf_match(reader.pages[page_number - 1].extract_text() or "")
        for page_number in range(896, 962)
    }
    questions = document["questions"]
    page_for_question: dict[int, int] = {}
    for position in range(4848, 5152):
        question = questions[position - 1]
        normalized = normalize_for_pdf_match(question["question_text"])
        matched_page = None
        for length in (60, 50, 40, 30, 25):
            probe = normalized[:length]
            if not probe:
                continue
            matched_page = next((page for page, text in page_text.items() if probe in text), None)
            if matched_page:
                break
        if not matched_page:
            raise ValueError(f"Could not map DI question {question['key']} to a PDF exercise page.")
        page_for_question[position] = matched_page
        question["stimulus_id"] = f"di-source-page-{matched_page}"

    pdf = pdfium.PdfDocument(str(source_pdf))
    stimuli: list[dict] = []
    for page_number in sorted(set(page_for_question.values())):
        rendered = pdf[page_number - 1].render(scale=1.6).to_pil().convert("RGB")
        encoded = BytesIO()
        rendered.save(encoded, "JPEG", quality=84, optimize=True, progressive=True)
        stimulus_id = f"di-source-page-{page_number}"
        stimuli.append({
            "id": stimulus_id,
            "type": "image",
            "title": f"Data Interpretation source visual (PDF page {page_number})",
            "alt_text": "Source table, bar graph, pie chart, or line graph for this Data Interpretation question set.",
            "file": f"assets/{stimulus_id}.jpg",
            "asset_bytes": encoded.getvalue(),
        })
    return stimuli


def build_package(destination: Path, document: dict, stimuli: list[dict], chunk_size: int = 500) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    questions = document["questions"]
    question_files = [
        f"questions/questions-{start + 1:04d}-{min(start + chunk_size, len(questions)):04d}.jsonl"
        for start in range(0, len(questions), chunk_size)
    ]
    manifest = {
        "format_version": 2,
        "bank_name": document["bank_name"],
        "question_files": question_files,
        "stimuli": [{key: value for key, value in stimulus.items() if key != "asset_bytes"} for stimulus in stimuli],
        "notes": "Taxonomy migrated from the source chapter headings.",
    }
    with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, suffix=".tmp") as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            for filename, start in zip(question_files, range(0, len(questions), chunk_size)):
                lines = (
                    json.dumps(question, ensure_ascii=False, separators=(",", ":"))
                    for question in questions[start : start + chunk_size]
                )
                archive.writestr(filename, "\n".join(lines) + "\n")
            for stimulus in stimuli:
                archive.writestr(stimulus["file"], stimulus["asset_bytes"])
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--write-source", action="store_true")
    parser.add_argument("--di-pdf", type=Path, help="R. S. Aggarwal PDF used to render DI tables and graphs.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    document = load_and_categorize(source)
    stimuli = attach_di_pdf_stimuli(document, args.di_pdf.resolve()) if args.di_pdf else []
    if args.write_source and args.output_json:
        raise ValueError("Choose either --write-source or --output-json, not both.")
    if args.write_source:
        write_json_atomic(source, document)
    elif args.output_json:
        write_json_atomic(args.output_json.resolve(), document)
    build_package(args.package.resolve(), document, stimuli)
    print(f"Categorized {len(document['questions']):,} questions across {len(CHAPTER_RANGES)} chapters.")
    print(f"Attached {len(stimuli)} shared Data Interpretation visual(s).")
    print(f"Built {args.package.resolve()}")


if __name__ == "__main__":
    main()
