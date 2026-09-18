"""Categorize the legacy quantitative bank and build its v2 ZIP package.

The chapter boundaries come from the numbered chapter headings in the source
material. The script is deterministic so the categorized JSON and package can
be rebuilt whenever the source bank is corrected.
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import unicodedata
import zipfile

try:
    from .solution_quality import audit_questions, write_audit_reports
except ImportError:
    from solution_quality import audit_questions, write_audit_reports


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "question-banks" / "quantitative_aptitude_complete_extended.json"
DEFAULT_PACKAGE = ROOT / "question-banks" / "quantitative_aptitude_categorized_v2.zip"
DEFAULT_AUDIT_DIR = ROOT / "question-banks" / "solution-audit"

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
    (4848, 4932, "Data Interpretation", "Tabulation"),
    (4933, 5013, "Data Interpretation", "Bar Graphs"),
    (5014, 5084, "Data Interpretation", "Pie Charts"),
    (5085, 5151, "Data Interpretation", "Line Graphs"),
)

# The legacy extraction flattened the PDF's two-column reading order.  The
# question sequence is therefore not always the same as the printed local
# number sequence (notably in the first Pie Charts and Line Graphs exercises).
# These source exercise records are the authoritative bridge between the
# legacy keys and the exact "Directions (Questions x-y)" visual set.
DI_EXERCISES = (
    {"id": "tabulation-exercise-1", "chapter": "Tabulation", "global_start": 4848, "global_end": 4872, "page_start": 896, "page_end": 899, "first_page_top": 0.0},
    {"id": "tabulation-exercise-2", "chapter": "Tabulation", "global_start": 4873, "global_end": 4907, "page_start": 901, "page_end": 905, "first_page_top": 0.0},
    {"id": "tabulation-exercise-3", "chapter": "Tabulation", "global_start": 4908, "global_end": 4932, "page_start": 908, "page_end": 911, "first_page_top": 498.0},
    {"id": "bar-graphs-exercise-1", "chapter": "Bar Graphs", "global_start": 4933, "global_end": 4963, "page_start": 914, "page_end": 917, "first_page_top": 0.0},
    {"id": "bar-graphs-exercise-2", "chapter": "Bar Graphs", "global_start": 4964, "global_end": 4988, "page_start": 921, "page_end": 923, "first_page_top": 0.0},
    {"id": "bar-graphs-exercise-3", "chapter": "Bar Graphs", "global_start": 4989, "global_end": 5013, "page_start": 927, "page_end": 930, "first_page_top": 0.0},
    {
        "id": "pie-charts-exercise-1",
        "chapter": "Pie Charts",
        "global_start": 5014,
        "global_end": 5037,
        "page_start": 932,
        "page_end": 935,
        "first_page_top": 0.0,
        # Source questions 4, 8, 13, 28 and 29 were omitted by the legacy
        # two-column extractor.  The remaining records occur in this order.
        "local_numbers": (1, 2, 3, 5, 6, 7, 14, 15, 9, 10, 11, 12, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27),
    },
    {"id": "pie-charts-exercise-2", "chapter": "Pie Charts", "global_start": 5038, "global_end": 5061, "page_start": 937, "page_end": 939, "first_page_top": 0.0},
    {"id": "pie-charts-exercise-3", "chapter": "Pie Charts", "global_start": 5062, "global_end": 5084, "page_start": 941, "page_end": 943, "first_page_top": 489.0},
    {
        "id": "line-graphs-exercise-1",
        "chapter": "Line Graphs",
        "global_start": 5085,
        "global_end": 5111,
        "page_start": 946,
        "page_end": 949,
        "first_page_top": 0.0,
        # Source question 26 is absent from the legacy extraction; the last
        # page's right column was emitted before its left column.
        "local_numbers": tuple(range(1, 24)) + (27, 28, 24, 25),
    },
    {"id": "line-graphs-exercise-2", "chapter": "Line Graphs", "global_start": 5112, "global_end": 5126, "page_start": 952, "page_end": 954, "first_page_top": 190.0},
    {"id": "line-graphs-exercise-3", "chapter": "Line Graphs", "global_start": 5127, "global_end": 5151, "page_start": 956, "page_end": 959, "first_page_top": 327.0},
)

SOLUTION_STEP_OVERRIDES = {
    "qa-4693": [
        "A tells the truth with probability 0.60, so A tells a lie with probability 0.40.",
        "B tells the truth with probability 0.70, so B tells a lie with probability 0.30.",
        "They say the same thing when both tell the truth or both tell a lie: (0.60 x 0.70) + (0.40 x 0.30) = 0.42 + 0.12 = 0.54. Therefore, option A is correct.",
    ],
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

# Some source pages contain the last questions for one graph above the next
# question set's graph.  Position-aware mapping keeps those questions attached
# to the preceding page; these focused crops also avoid showing an unrelated
# table that happens to share the same two-column source page.
SOURCE_VISUAL_CROPS = (
    {
        "id": "qa-4063-adjoining-figure",
        "page_number": 729,
        "crop_box": (130.0, 232.0, 222.0, 310.0),
        "question_keys": ("qa-4063",),
        "title": "Adjoining geometry figure",
        "alt_text": (
            "Circle of radius a with a triangle inscribed in its upper semicircle; "
            "the region between the semicircle and triangle is shaded."
        ),
    },
    {
        "id": "di-company-profit-2004-2010",
        "page_number": 947,
        "crop_box": (322.0, 66.0, 565.0, 365.0),
        "question_keys": tuple(f"qa-{number:04d}" for number in range(5095, 5103)),
        "title": "Percent profit earned by Companies A and B (2004-2010)",
        "alt_text": (
            "Line graph of percent profit earned by Companies A and B from 2004 through 2010."
        ),
    },
)

# Focused source-set overrides are used only when a figure's vector paths do
# not form a detectable component.  Coordinates are PDF points (612 x 792),
# and each crop is still linked by exact exercise and local question range.
DI_GROUP_CROP_OVERRIDES = {
    ("bar-graphs-exercise-2", 1, 5): ((921, (45.0, 112.0, 567.0, 296.0)),),
    ("bar-graphs-exercise-2", 6, 10): ((921, (310.0, 384.0, 575.0, 505.0)),),
    ("bar-graphs-exercise-3", 6, 10): ((927, (45.0, 505.0, 567.0, 760.0)),),
    ("bar-graphs-exercise-3", 16, 20): ((929, (145.0, 88.0, 475.0, 338.0)),),
    ("pie-charts-exercise-3", 10, 14): ((942, (310.0, 216.0, 570.0, 590.0)),),
    ("pie-charts-exercise-3", 15, 19): ((943, (45.0, 310.0, 300.0, 590.0)),),
    ("line-graphs-exercise-1", 11, 18): ((947, (322.0, 66.0, 565.0, 365.0)),),
}


def taxonomy_for(number: int) -> tuple[str, str]:
    for start, end, category, chapter in CHAPTER_RANGES:
        if start <= number <= end:
            return category, chapter
    raise ValueError(f"Question number {number} is outside the mapped source range.")


def clean_math_text(value: str) -> str:
    """Remove private-use glyph fragments emitted by the legacy PDF extractor."""
    for _ in range(2):
        if not any(marker in value for marker in ("\u00c3", "\u00c2", "\u00e2")):
            break
        try:
            value = value.encode("latin-1").decode("utf-8")
        except UnicodeError:
            break
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


def normalized_word_index(pdf_page) -> tuple[str, list[tuple[int, int, float]]]:
    """Return normalized page text plus offsets back to each source word."""
    normalized_text = ""
    positions: list[tuple[int, int, float]] = []
    for word in pdf_page.extract_words():
        token = normalize_for_pdf_match(str(word.get("text", "")))
        if not token:
            continue
        start = len(normalized_text)
        normalized_text += token
        positions.append((start, len(normalized_text), float(word["top"])))
    return normalized_text, positions


def question_top_from_word_index(
    word_index: tuple[str, list[tuple[int, int, float]]], question_text: str
) -> float | None:
    """Locate a question's first word so visuals later on the page are ignored."""
    page_text, positions = word_index
    normalized_question = normalize_for_pdf_match(question_text)
    match_offset = -1
    for length in (80, 70, 60, 50, 40, 30, 25):
        probe = normalized_question[:length]
        if not probe:
            continue
        match_offset = page_text.find(probe)
        if match_offset >= 0:
            break
    if match_offset < 0:
        return None
    for start, end, top in positions:
        if start <= match_offset < end:
            return top
    return None


def select_visual_page(
    page_number: int,
    question_top: float | None,
    pages_with_visuals: list[int],
    regions_for_page: dict[int, list[tuple[float, float, float, float]]],
) -> int:
    """Choose the closest graph that appears before the question in reading order."""
    current_regions = regions_for_page.get(page_number, [])
    current_visual_precedes_question = bool(current_regions) and (
        question_top is None or min(region[1] for region in current_regions) <= question_top
    )
    if current_visual_precedes_question:
        return page_number
    previous_page = max((candidate for candidate in pages_with_visuals if candidate < page_number), default=None)
    if previous_page is not None:
        return previous_page
    if current_regions:
        return page_number
    return min(pages_with_visuals)


def visual_regions(pdf_page) -> list[tuple[float, float, float, float]]:
    """Find table/chart regions from the vector rules in an exercise page."""
    table_boxes = []
    for table in pdf_page.find_tables():
        x0, top, x1, bottom = map(float, table.bbox)
        if x1 - x0 >= 75 and bottom - top >= 35:
            table_boxes.append((x0, top, x1, bottom))
    rules = []
    column_dividers: list[tuple[float, float, float]] = []
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
        if x1 - x0 < 2 and bottom - top > 150 and pdf_page.width * 0.45 < x0 < pdf_page.width * 0.55:
            column_dividers.append(((x0 + x1) / 2, top, bottom))
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
    regions: list[tuple[float, float, float, float]] = [
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

    # Pie charts are Bezier curves in this PDF.  Most curves are letter
    # outlines, so keep only figure-sized, roughly square curve bounds.  The
    # largest outer curve absorbs its internal sectors during de-duplication.
    curve_regions: list[tuple[float, float, float, float]] = []
    for curve in pdf_page.curves:
        x0, x1 = float(curve["x0"]), float(curve["x1"])
        top, bottom = float(curve["top"]), float(curve["bottom"])
        width, height = x1 - x0, bottom - top
        if width < 80 or height < 80 or width * height < 6500:
            continue
        if not 0.45 <= width / height <= 2.2:
            continue
        curve_regions.append((max(18, x0 - 22), max(90, top - 30), min(pdf_page.width - 18, x1 + 22), min(pdf_page.height - 20, bottom + 8)))

    def area(box: tuple[float, float, float, float]) -> float:
        return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])

    def intersection(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
        return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(0.0, min(first[3], second[3]) - max(first[1], second[1]))

    for curve_box in sorted(curve_regions, key=area, reverse=True):
        if any(intersection(curve_box, box) >= min(area(curve_box), area(box)) * 0.72 for box in regions):
            continue
        if any(intersection(curve_box, box) >= min(area(curve_box), area(box)) * 0.72 for box in curve_regions if box in regions):
            continue
        regions.append(curve_box)

    # A source page can contain unrelated left- and right-column charts at the
    # same vertical position.  If detection joined them across the printed
    # column divider, split the crop back into its original columns.
    split_regions: list[tuple[float, float, float, float]] = []
    for region in regions:
        pieces = [region]
        for divider_x, divider_top, divider_bottom in column_dividers:
            next_pieces: list[tuple[float, float, float, float]] = []
            for piece in pieces:
                vertical_overlap = max(0.0, min(piece[3], divider_bottom) - max(piece[1], divider_top))
                if piece[0] + 70 < divider_x < piece[2] - 70 and vertical_overlap >= 28:
                    next_pieces.extend(((piece[0], piece[1], divider_x - 3, piece[3]), (divider_x + 3, piece[1], piece[2], piece[3])))
                else:
                    next_pieces.append(piece)
            pieces = next_pieces
        split_regions.extend(pieces)

    # Drop nested duplicates (for example, a detected table plus the same
    # table's line component) while retaining separate charts in one set.
    deduplicated: list[tuple[float, float, float, float]] = []
    for box in sorted(split_regions, key=area, reverse=True):
        if any(intersection(box, kept) >= min(area(box), area(kept)) * 0.78 for kept in deduplicated):
            continue
        deduplicated.append(box)
    return sorted(deduplicated, key=lambda box: (box[1], box[0]))


def render_visual_crop(rendered, regions: list[tuple[float, float, float, float]], scale: float):
    crops = [rendered.crop(tuple(round(value * scale) for value in region)) for region in regions]
    return compose_visual_crops(crops)


def compose_visual_crops(crops):
    from PIL import Image

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


def render_source_visual_crops(document: dict, pdf) -> list[dict]:
    """Render exact textbook figures that need a dedicated question stimulus."""
    from PIL import ImageOps

    questions_by_key = {question["key"]: question for question in document["questions"]}
    stimuli: list[dict] = []
    scale = 2.2
    padding = round(10 * scale)
    for specification in SOURCE_VISUAL_CROPS:
        missing_keys = [key for key in specification["question_keys"] if key not in questions_by_key]
        if missing_keys:
            raise ValueError(f"Source visual references unknown question(s): {', '.join(missing_keys)}")
        rendered = pdf[specification["page_number"] - 1].render(scale=scale).to_pil().convert("RGB")
        crop = rendered.crop(tuple(round(value * scale) for value in specification["crop_box"]))
        crop = ImageOps.expand(crop, border=padding, fill="white")
        encoded = BytesIO()
        crop.save(encoded, "PNG", optimize=True)
        for key in specification["question_keys"]:
            questions_by_key[key]["stimulus_id"] = specification["id"]
        stimuli.append({
            "id": specification["id"],
            "type": "image",
            "title": specification["title"],
            "alt_text": specification["alt_text"],
            "file": f"assets/{specification['id']}.png",
            "asset_bytes": encoded.getvalue(),
        })
    return stimuli


def source_order(page_number: int, x: float, top: float, page_width: float = 612.0) -> tuple[int, int, float]:
    """Return the textbook's page/column/top reading order."""
    return page_number, 1 if x >= page_width / 2 else 0, top


def direction_group_events(pdf_page, page_number: int, minimum_top: float = 0.0) -> list[dict]:
    """Locate exact source set headings such as Directions (Questions 6-10)."""
    words = [
        word for word in pdf_page.extract_words()
        if minimum_top <= float(word["top"]) <= pdf_page.height - 30
    ]
    lines: list[dict] = []
    for word in sorted(words, key=lambda item: (round(float(item["top"]), 1), float(item["x0"]))):
        line = next(
            (candidate for candidate in reversed(lines[-8:]) if abs(candidate["top"] - float(word["top"])) <= 1.4),
            None,
        )
        if line is None:
            line = {"top": float(word["top"]), "words": []}
            lines.append(line)
        line["words"].append(word)

    events: list[dict] = []
    for line in lines:
        line_words = sorted(line["words"], key=lambda item: float(item["x0"]))
        text = " ".join(str(word["text"]) for word in line_words)
        match = re.search(r"Questions?\D{0,6}(\d+)\D{1,8}(\d+)", text, re.IGNORECASE)
        if not match or not re.search(r"Direction|\bEx\.?", text[: match.start()], re.IGNORECASE):
            continue
        question_word_index = next(
            (index for index, word in enumerate(line_words) if "question" in str(word["text"]).lower()),
            len(line_words),
        )
        marker = next(
            (
                word for word in reversed(line_words[:question_word_index])
                if re.search(r"Direction|^Ex\.?$", str(word["text"]), re.IGNORECASE)
            ),
            line_words[0],
        )
        start, end = int(match.group(1)), int(match.group(2))
        events.append({
            "start": start,
            "end": end,
            "page_number": page_number,
            "x": float(marker["x0"]),
            "top": float(line["top"]),
            "order": source_order(page_number, float(marker["x0"]), float(line["top"]), float(pdf_page.width)),
        })
    return events


def numbered_question_events(pdf_page, page_number: int, minimum_top: float = 0.0) -> list[dict]:
    """Locate printed local question numbers while ignoring option labels."""
    words = [
        word for word in pdf_page.extract_words()
        if minimum_top <= float(word["top"]) <= pdf_page.height - 30
    ]
    events: list[dict] = []
    for index, word in enumerate(words):
        match = re.fullmatch(r"(\d+)\.", str(word["text"]))
        if not match:
            continue
        previous = words[index - 1] if index else None
        if previous and abs(float(previous["top"]) - float(word["top"])) <= 2 and str(previous["text"]).lower().startswith("ex"):
            continue
        x, top = float(word["x0"]), float(word["top"])
        events.append({
            "number": int(match.group(1)),
            "page_number": page_number,
            "x": x,
            "top": top,
            "order": source_order(page_number, x, top, float(pdf_page.width)),
        })
    return events


def source_question_stem(pdf_page, local_number: int) -> str:
    """Extract one printed DI question stem, excluding its answer options."""
    words = pdf_page.extract_words()
    numbered = [
        (index, word) for index, word in enumerate(words)
        if str(word["text"]) == f"{local_number}."
    ]
    if not numbered:
        return ""
    _, marker = numbered[0]
    right_column = float(marker["x0"]) >= float(pdf_page.width) / 2
    marker_top = float(marker["top"])
    marker_x = float(marker["x0"])
    column_words = [
        word for word in words
        if (float(word["x0"]) >= float(pdf_page.width) / 2) == right_column
        and (
            float(word["top"]) > marker_top + 1.5
            or (abs(float(word["top"]) - marker_top) <= 1.5 and float(word["x0"]) >= marker_x)
        )
    ]
    column_words.sort(key=lambda word: (round(float(word["top"]), 1), float(word["x0"])))
    collected: list[str] = []
    for word in column_words:
        token = str(word["text"])
        if token == f"{local_number}." and not collected:
            continue
        if re.fullmatch(r"\(\s*a\s*\)", token, re.IGNORECASE):
            break
        collected.append(token)
    return clean_math_text(" ".join(collected))


def exercise_local_numbers(exercise: dict) -> tuple[int, ...]:
    count = exercise["global_end"] - exercise["global_start"] + 1
    local_numbers = tuple(exercise.get("local_numbers", range(1, count + 1)))
    if len(local_numbers) != count:
        raise ValueError(f"{exercise['id']} has {count} questions but {len(local_numbers)} source numbers.")
    return local_numbers


def attach_di_pdf_stimuli(document: dict, source_pdf: Path) -> list[dict]:
    """Attach figures by exact source question set, never by a nearby page."""
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        import pdfplumber
    except ImportError as error:
        raise RuntimeError(
            "DI PDF rendering needs pypdf, pdfplumber and pypdfium2. Use the bundled Codex Python runtime."
        ) from error
    if not source_pdf.is_file():
        raise FileNotFoundError(f"DI source PDF was not found: {source_pdf}")

    questions = document["questions"]
    reader = PdfReader(str(source_pdf))
    pdf = pdfium.PdfDocument(str(source_pdf))
    stimuli: list[dict] = []
    scale = 1.75

    with pdfplumber.open(source_pdf) as visual_pdf:
        rendered_pages: dict[int, object] = {}
        for exercise in DI_EXERCISES:
            pages = range(exercise["page_start"], exercise["page_end"] + 1)
            page_text = {
                page_number: normalize_for_pdf_match(reader.pages[page_number - 1].extract_text() or "")
                for page_number in pages
            }
            events: list[dict] = []
            for page_number in pages:
                minimum_top = exercise["first_page_top"] if page_number == exercise["page_start"] else 0.0
                events.extend(direction_group_events(visual_pdf.pages[page_number - 1], page_number, minimum_top))
            events.sort(key=lambda event: event["order"])
            event_ranges = {(event["start"], event["end"]): event for event in events}
            if len(event_ranges) != len(events):
                raise ValueError(f"Duplicate source set headings were detected in {exercise['id']}.")

            local_numbers = exercise_local_numbers(exercise)
            questions_for_group: dict[tuple[int, int], list[int]] = {key: [] for key in event_ranges}
            last_source_page = exercise["page_start"]
            for offset, local_number in enumerate(local_numbers):
                position = exercise["global_start"] + offset
                question = questions[position - 1]
                group_key = next(
                    (key for key in event_ranges if key[0] <= local_number <= key[1]),
                    None,
                )
                if group_key is None:
                    raise ValueError(f"{question['key']} (source question {local_number}) has no Directions set in {exercise['id']}.")

                normalized = normalize_for_pdf_match(question["question_text"])
                matched_page = None
                for length in (80, 70, 60, 50, 40, 30, 25, 20, 15):
                    probe = normalized[:length]
                    if not probe:
                        continue
                    matched_page = next((page for page, text in page_text.items() if probe in text), None)
                    if matched_page is not None:
                        break
                if matched_page is None:
                    raise ValueError(f"Could not trace {question['key']} to a source exercise page.")
                if matched_page < last_source_page:
                    raise ValueError(
                        f"Source mapping moved backwards for {question['key']}: page {last_source_page} to {matched_page}."
                    )
                last_source_page = matched_page
                source_set_id = f"di-{exercise['id']}-q{group_key[0]}-{group_key[1]}"
                question["stimulus_id"] = source_set_id
                question["source_page"] = matched_page
                question["source_question_number"] = local_number
                question["source_set_id"] = source_set_id
                source_stem = source_question_stem(visual_pdf.pages[matched_page - 1], local_number)
                normalized_source_stem = normalize_for_pdf_match(source_stem)
                question["source_fidelity"] = round(
                    SequenceMatcher(None, normalized, normalized_source_stem, autojunk=False).ratio()
                    if normalized_source_stem else 0.0,
                    4,
                )
                question["source_length_ratio"] = round(
                    min(len(normalized), len(normalized_source_stem)) / max(len(normalized), len(normalized_source_stem))
                    if normalized and normalized_source_stem else 0.0,
                    4,
                )
                questions_for_group[group_key].append(position)

            regions_for_group: dict[tuple[int, int], list[tuple[int, tuple[float, float, float, float]]]] = {
                key: [] for key in event_ranges
            }
            for page_number in pages:
                minimum_top = exercise["first_page_top"] if page_number == exercise["page_start"] else 0.0
                for region in visual_regions(visual_pdf.pages[page_number - 1]):
                    if region[3] < minimum_top:
                        continue
                    page_width = float(visual_pdf.pages[page_number - 1].width)
                    page_center = page_width / 2
                    # A table or chart that substantially crosses the centre
                    # is a full-width, single-column figure.  A right-column
                    # chart may extend slightly left for labels, so require a
                    # meaningful reach on both sides before classing it left.
                    order_x = (
                        region[0]
                        if region[0] < page_center - 40 and region[2] > page_center + 40
                        else (region[0] + region[2]) / 2
                    )
                    region_order = source_order(page_number, order_x, region[1] + 28, page_width)
                    preceding = [event for event in events if event["order"] <= region_order]
                    if not preceding:
                        continue
                    event = preceding[-1]
                    regions_for_group[(event["start"], event["end"])].append((page_number, region))

            for group_key in regions_for_group:
                override = DI_GROUP_CROP_OVERRIDES.get((exercise["id"], group_key[0], group_key[1]))
                if override:
                    regions_for_group[group_key] = list(override)

            missing_regions = [key for key, positions in questions_for_group.items() if positions and not regions_for_group[key]]
            if missing_regions:
                ranges = ", ".join(f"{start}-{end}" for start, end in missing_regions)
                raise ValueError(f"No exact visual region was found for {exercise['id']} set(s): {ranges}.")

            for group_key, positions in questions_for_group.items():
                if not positions:
                    continue
                crops = []
                for page_number, region in regions_for_group[group_key]:
                    if page_number not in rendered_pages:
                        rendered_pages[page_number] = pdf[page_number - 1].render(scale=scale).to_pil().convert("RGB")
                    crops.append(
                        rendered_pages[page_number].crop(tuple(round(value * scale) for value in region))
                    )
                cropped = compose_visual_crops(crops)
                encoded = BytesIO()
                cropped.save(encoded, "JPEG", quality=86, optimize=True, progressive=True)
                stimulus_id = f"di-{exercise['id']}-q{group_key[0]}-{group_key[1]}"
                stimuli.append({
                    "id": stimulus_id,
                    "type": "image",
                    "title": f"{exercise['chapter']} - source questions {group_key[0]}-{group_key[1]}",
                    "alt_text": f"Exact textbook visual for {exercise['chapter']} questions {group_key[0]} through {group_key[1]}.",
                    "file": f"assets/{stimulus_id}.jpg",
                    "asset_bytes": encoded.getvalue(),
                })

        stimuli.extend(render_source_visual_crops(document, pdf))
    return stimuli


def build_package(destination: Path, document: dict, stimuli: list[dict], audit_summary: dict | None = None, chunk_size: int = 500) -> None:
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
        "solution_audit": audit_summary or {},
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
    parser.add_argument("--di-pdf", type=Path, help="R. S. Aggarwal PDF used to render source tables, graphs, and figures.")
    parser.add_argument("--audit-report-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--strict-solutions", action="store_true", help="Refuse to build while critical solution issues remain.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    document = load_and_categorize(source)
    stimuli = attach_di_pdf_stimuli(document, args.di_pdf.resolve()) if args.di_pdf else []
    audit_records, audit_summary = audit_questions(document["questions"])
    write_audit_reports(args.audit_report_dir.resolve(), audit_records, audit_summary)
    if args.strict_solutions and audit_summary["critical_questions"]:
        raise ValueError(f"Solution audit found {audit_summary['critical_questions']:,} critical question(s).")
    if args.write_source and args.output_json:
        raise ValueError("Choose either --write-source or --output-json, not both.")
    if args.write_source:
        write_json_atomic(source, document)
    elif args.output_json:
        write_json_atomic(args.output_json.resolve(), document)
    build_package(args.package.resolve(), document, stimuli, audit_summary)
    print(f"Categorized {len(document['questions']):,} questions across {len(CHAPTER_RANGES)} chapters.")
    print(f"Attached {len(stimuli)} shared question visual(s).")
    print(f"Solution audit: {audit_summary['critical_questions']:,} critical, {audit_summary['review_questions']:,} review.")
    print(f"Built {args.package.resolve()}")


if __name__ == "__main__":
    main()
