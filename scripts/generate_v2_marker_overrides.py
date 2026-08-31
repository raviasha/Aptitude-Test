"""Generate reviewed-crop candidates for two-column V2 textbook chapters.

The generator only proposes deterministic boundaries.  The resulting crops still
flow through the pipeline's source/render vision review before publication.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import re
from pathlib import Path
from typing import Iterable

import pdfplumber


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCALE_180_DPI = 2.5
PAGE_TOP = 170
LEFT_COLUMN = (95, 760)
RIGHT_COLUMN = (775, 1435)

Marker = tuple[int, float, float]


def answer_windows(
    values: list[str],
    *,
    default_minimum: float,
    default_maximum: float,
) -> defaultdict[int, tuple[float, float]]:
    result: defaultdict[int, tuple[float, float]] = defaultdict(
        lambda: (default_minimum, default_maximum)
    )
    for value in values:
        page, minimum, maximum = value.split(":", 2)
        result[int(page)] = (float(minimum), float(maximum))
    return result


def _pixel_top(points: float) -> int:
    return max(PAGE_TOP, round(points * SCALE_180_DPI) - 8)


def _column(x0: float) -> int:
    return 0 if x0 < 200 else 1


def _region(page: int, column: int) -> tuple[int, int]:
    return page, column


def _region_index(page: int, column: int) -> int:
    return page * 2 + column


def explicit_segments(
    markers: dict[int, Marker],
    *,
    page_bottoms: dict[int, int],
    final_bottom: int,
) -> dict[int, list[dict[str, int]]]:
    """Convert numbered PDF markers into explicit 180-DPI crop segments."""
    numbers = sorted(markers)
    result: dict[int, list[dict[str, int]]] = {}
    for position, number in enumerate(numbers):
        page, x0, top_points = markers[number]
        column = _column(x0)
        start_index = _region_index(page, column)
        next_marker = markers[numbers[position + 1]] if position + 1 < len(numbers) else None
        end_index = (
            _region_index(next_marker[0], _column(next_marker[1]))
            if next_marker is not None
            else start_index
        )
        if end_index < start_index:
            raise ValueError(f"Markers leave reading order at question {number}.")

        crops: list[dict[str, int]] = []
        for index in range(start_index, end_index + 1):
            region_page, region_column = divmod(index, 2)
            left, right = LEFT_COLUMN if region_column == 0 else RIGHT_COLUMN
            top = _pixel_top(top_points) if index == start_index else PAGE_TOP
            bottom = page_bottoms.get(region_page, final_bottom)
            if next_marker is not None and index == end_index:
                bottom = _pixel_top(next_marker[2])
            if top >= bottom:
                continue
            crops.append(
                {
                    "page": region_page,
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                }
            )
        if not crops:
            raise ValueError(f"Marker {number} produced no crop segments.")
        result[number] = crops
    return result


def _marker_candidates(
    pdf_path: Path,
    pages: Iterable[int],
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
                if str(word["text"]).upper() == "ANSWERS" and float(word["size"]) >= 11.5
            ]
            cutoff = min(answer_tops) if stop_at_answers and answer_tops else float("inf")
            found: list[tuple[int, int, float, float]] = []
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
                found.append((int(match.group(1)), page_number, x0, top))
            found.sort(key=lambda item: (0 if item[2] < 200 else 1, item[3]))
            candidates.extend(found)
    return candidates


def _select_markers(candidates: list[tuple[int, int, float, float]], total: int) -> dict[int, Marker]:
    selected: dict[int, Marker] = {}
    cursor = 0
    for number in range(1, total + 1):
        found = next((index for index in range(cursor, len(candidates)) if candidates[index][0] == number), None)
        if found is None:
            raise ValueError(f"Could not locate printed marker {number}.")
        _, page, x0, top = candidates[found]
        selected[number] = (page, x0, top)
        cursor = found + 1
    return selected


def _answer_segments(
    pdf_path: Path,
    pages: range,
    total: int,
    *,
    minimum_top: float,
    maximum_top: float,
    page_windows: dict[int, tuple[float, float]] | None = None,
) -> dict[int, list[dict[str, int]]]:
    located: dict[int, tuple[int, float]] = {}
    with pdfplumber.open(pdf_path) as document:
        for page_number in pages:
            page_minimum, page_maximum = (
                page_windows[page_number]
                if page_windows is not None
                else (minimum_top, maximum_top)
            )
            words = document.pages[page_number - 1].extract_words()
            for word in words:
                match = re.fullmatch(r"(\d+)\.", str(word["text"]))
                if not match:
                    continue
                number = int(match.group(1))
                top = float(word["top"])
                if 1 <= number <= total and page_minimum <= top <= page_maximum:
                    located[number] = (page_number, top)
    expected = set(range(1, total + 1))
    if set(located) != expected:
        raise ValueError(f"Answer marker coverage mismatch: missing {sorted(expected - set(located))}.")
    return {
        number: [
            {
                "page": page,
                "left": 95,
                "top": round(top * SCALE_180_DPI) - 8,
                "right": 1435,
                "bottom": round(top * SCALE_180_DPI) + 25,
            }
        ]
        for number, (page, top) in located.items()
    }


def _page_bottoms(values: list[str]) -> dict[int, int]:
    result: dict[int, int] = {}
    for value in values:
        page, bottom = value.split(":", 1)
        result[int(page)] = int(bottom)
    return result


def generate(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    total = int(config["printed_question_count"])
    source_pdf = PROJECT_ROOT / config["source_pdf"]

    question_candidates = _marker_candidates(
        source_pdf,
        range(config["question_pages"][0], config["question_pages"][1] + 1),
        minimum_size=8.5,
        maximum_size=11.5,
        stop_at_answers=True,
    )
    question_markers = _select_markers(question_candidates, total)

    solution_start = int(config["solution_pages"][0])
    solution_candidates = _marker_candidates(
        source_pdf,
        range(solution_start, config["solution_pages"][1] + 1),
        minimum_size=7.5,
        maximum_size=10.5,
        stop_at_answers=False,
    )
    solution_candidates = [
        candidate
        for candidate in solution_candidates
        if candidate[1] > solution_start or candidate[3] >= args.solution_first_page_min_top
    ]
    solution_markers = _select_markers(solution_candidates, total)

    question_segments = explicit_segments(
        question_markers,
        page_bottoms=_page_bottoms(args.question_page_bottom),
        final_bottom=args.page_bottom,
    )
    solution_segments = explicit_segments(
        solution_markers,
        page_bottoms={},
        final_bottom=args.page_bottom,
    )
    answer_segments = _answer_segments(
        source_pdf,
        range(config["answer_pages"][0], config["answer_pages"][1] + 1),
        total,
        minimum_top=args.answer_top_min,
        maximum_top=args.answer_top_max,
        page_windows=answer_windows(
            args.answer_page_window,
            default_minimum=args.answer_top_min,
            default_maximum=args.answer_top_max,
        ),
    )

    config["marker_overrides"] = {
        "question": {str(number): {"segments": segments} for number, segments in question_segments.items()},
        "answer_key": {str(number): {"segments": segments} for number, segments in answer_segments.items()},
        "solution": {str(number): {"segments": segments} for number, segments in solution_segments.items()},
    }
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "generated", "records": total, "config": str(config_path)}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--solution-first-page-min-top", type=float, default=0.0)
    parser.add_argument("--answer-top-min", type=float, required=True)
    parser.add_argument("--answer-top-max", type=float, required=True)
    parser.add_argument(
        "--answer-page-window",
        action="append",
        default=[],
        metavar="PAGE:MIN:MAX",
    )
    parser.add_argument("--question-page-bottom", action="append", default=[], metavar="PAGE:PIXEL")
    parser.add_argument("--page-bottom", type=int, default=1870)
    generate(parser.parse_args())


if __name__ == "__main__":
    main()
