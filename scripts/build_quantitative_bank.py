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
import unicodedata
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

SOLUTION_STEP_OVERRIDES = {
    "qa-5061": [
        "Groceries, Entertainment and Investments = (23% + 10% + 15%) × ₹45,800 = 48% × ₹45,800 = ₹21,984.",
        "Commuting = 22% × ₹45,800 = ₹10,076.",
        "Required percentage = (₹21,984 ÷ ₹10,076) × 100 = 218.18% ≈ 218%. Therefore, option E is correct.",
    ],
    "qa-5126": [
        "The graph shows imports are 125% of exports in 2008. So ₹250 crores = 125% of exports, and exports in 2008 = ₹250 ÷ 1.25 = ₹200 crores.",
        "Total exports in 2008 and 2009 are ₹500 crores. Therefore, exports in 2009 = ₹500 − ₹200 = ₹300 crores.",
        "Imports in 2009 are 140% of exports. So imports = 1.40 × ₹300 = ₹420 crores. Therefore, option D is correct.",
    ],
}


def taxonomy_for(number: int) -> tuple[str, str]:
    for start, end, category, chapter in CHAPTER_RANGES:
        if start <= number <= end:
            return category, chapter
    raise ValueError(f"Question number {number} is outside the mapped source range.")


def clean_math_text(value: str) -> str:
    """Remove private-use glyph fragments emitted by the legacy PDF extractor."""
    if any(marker in value for marker in ("Ã", "Â", "â")):
        try:
            value = value.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = re.sub(r"[\uE000-\uF8FF]", "", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def clean_record(value):
    if isinstance(value, str):
        return clean_math_text(value)
    if isinstance(value, list):
        return [clean_record(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_record(item) for key, item in value.items()}
    return value


def load_and_categorize(source: Path) -> dict:
    document = json.loads(source.read_text(encoding="utf-8"))
    questions = document.get("questions") if isinstance(document, dict) else None
    if not isinstance(questions, list) or len(questions) != 5151:
        raise ValueError("The quantitative source must contain exactly 5,151 questions.")
    for number, raw_question in enumerate(questions, start=1):
        expected_key = f"qa-{number:04d}"
        if not isinstance(raw_question, dict) or raw_question.get("key") != expected_key:
            raise ValueError(f"Expected {expected_key!r} at question position {number}.")
        question = clean_record(raw_question)
        questions[number - 1] = question
        category, chapter = taxonomy_for(number)
        question["category"] = category
        question["chapter"] = chapter
        if question["key"] in SOLUTION_STEP_OVERRIDES:
            question["solution_steps"] = SOLUTION_STEP_OVERRIDES[question["key"]]
    document["bank_name"] = "R. S. Aggarwal Quantitative Aptitude (2017) - Categorized (Cropped DI Visuals)"
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


def visual_regions(pdf_page) -> list[tuple[float, float, float, float]]:
    """Find table/chart regions from the vector rules in an exercise page."""
    table_boxes = []
    for table in pdf_page.find_tables():
        x0, top, x1, bottom = map(float, table.bbox)
        if x1 - x0 >= 75 and bottom - top >= 35:
            table_boxes.append((x0, top, x1, bottom))
    rules = []
    # Curves in this PDF include the outline of individual letters.  Including
    # them merges headings and question text into a page-sized region; charts
    # and tables are defined by straight rules and rectangles.
    for item in [*pdf_page.lines, *pdf_page.rects]:
        x0, x1 = float(item["x0"]), float(item["x1"])
        top, bottom = float(item["top"]), float(item["bottom"])
        if top < 90 or (x1 - x0 < 6 and bottom - top < 6):
            continue
        # Decorative chapter rules can be close enough to a graph axis to join
        # the chart component. They are wide, hairline rules near the heading.
        if x1 - x0 > 250 and bottom - top < 2 and top < 180:
            continue
        # The two-column textbook layout uses a long central divider.  It is
        # not part of the left-hand graph and would otherwise pull question
        # text into the crop.
        if x1 - x0 < 2 and bottom - top > 200 and x0 > pdf_page.width * 0.45:
            continue
        rules.append((x0, top, x1, bottom))
    components: list[list[float]] = []
    for x0, top, x1, bottom in rules:
        for box in components:
            if not (x1 + 28 < box[0] or box[2] + 28 < x0 or bottom + 28 < box[1] or box[3] + 28 < top):
                box[0], box[1], box[2], box[3] = min(box[0], x0), min(box[1], top), max(box[2], x1), max(box[3], bottom)
                break
        else:
            components.append([x0, top, x1, bottom])
    # A bar chart's axes and bar groups are sometimes emitted as two nearby
    # components. Join only boxes that substantially share a vertical span;
    # this avoids joining unrelated charts stacked on the same page.
    changed = True
    while changed:
        changed = False
        for index, first in enumerate(components):
            for second_index in range(index + 1, len(components)):
                second = components[second_index]
                overlap = max(0, min(first[3], second[3]) - max(first[1], second[1]))
                shorter = min(first[3] - first[1], second[3] - second[1])
                gap = max(first[0], second[0]) - min(first[2], second[2])
                if shorter and overlap / shorter >= 0.45 and gap <= 120:
                    first[0], first[1], first[2], first[3] = min(first[0], second[0]), min(first[1], second[1]), max(first[2], second[2]), max(first[3], second[3])
                    components.pop(second_index)
                    changed = True
                    break
            if changed:
                break
    regions = [
        (max(18, x0 - 10), max(90, top - 10), min(pdf_page.width - 18, x1 + 10), min(pdf_page.height - 20, bottom + 10))
        for x0, top, x1, bottom in table_boxes
    ]
    for x0, top, x1, bottom in components:
        width, height = x1 - x0, bottom - top
        if width < 75 or height < 35 or width * height < 5000:
            continue
        if any(
            max(0, min(x1, table[2]) - max(x0, table[0])) * max(0, min(bottom, table[3]) - max(top, table[1]))
            >= (table[2] - table[0]) * (table[3] - table[1]) * 0.9
            for table in table_boxes
        ):
            continue
        regions.append((max(18, x0 - 28), max(90, top - 34), min(pdf_page.width - 18, x1 + 28), min(pdf_page.height - 20, bottom + 5)))
    return sorted(regions, key=lambda box: (box[1], box[0]))


def render_visual_crop(rendered, regions: list[tuple[float, float, float, float]], scale: float):
    from PIL import Image

    crops = [rendered.crop(tuple(round(value * scale) for value in region)) for region in regions]
    if not crops:
        raise ValueError("No chart or table region could be detected on a mapped DI source page.")
    width = max(crop.width for crop in crops)
    padding, gap = 22, 18
    height = sum(crop.height for crop in crops) + gap * (len(crops) - 1) + padding * 2
    composite = Image.new("RGB", (width + padding * 2, height), "white")
    y = padding
    for crop in crops:
        composite.paste(crop, ((composite.width - crop.width) // 2, y))
        y += crop.height + gap
    return composite


def attach_di_pdf_stimuli(document: dict, source_pdf: Path) -> list[dict]:
    """Render PDF pages containing each DI question and link them as shared stimuli.

    The supplied R. S. Aggarwal PDF has vector charts/tables rather than image
    objects. Rendering the source exercise pages preserves the original visual
    data, labels, and legends without copying answer/solution pages.
    """
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        import pdfplumber
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

    pdf = pdfium.PdfDocument(str(source_pdf))
    stimuli: list[dict] = []
    with pdfplumber.open(source_pdf) as visual_pdf:
        source_pages = sorted(set(page_for_question.values()))
        regions_for_page = {page_number: visual_regions(visual_pdf.pages[page_number - 1]) for page_number in source_pages}
        pages_with_visuals = [page_number for page_number in source_pages if regions_for_page[page_number]]
        if not pages_with_visuals:
            raise ValueError("No chart or table regions were detected in the DI source pages.")
        for position, page_number in page_for_question.items():
            visual_page = max((candidate for candidate in pages_with_visuals if candidate <= page_number), default=None)
            if visual_page is None:
                visual_page = min(pages_with_visuals)
            questions[position - 1]["stimulus_id"] = f"di-source-page-{visual_page}"
        for page_number in pages_with_visuals:
            scale = 1.6
            rendered = pdf[page_number - 1].render(scale=scale).to_pil().convert("RGB")
            cropped = render_visual_crop(rendered, regions_for_page[page_number], scale)
            encoded = BytesIO()
            cropped.save(encoded, "JPEG", quality=84, optimize=True, progressive=True)
            stimulus_id = f"di-source-page-{page_number}"
            stimuli.append({
                "id": stimulus_id,
                "type": "image",
                "title": "Data Interpretation chart or table",
                "alt_text": "Chart or table required for this Data Interpretation question set.",
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
