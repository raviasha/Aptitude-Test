"""Deterministic source-marker proposals awaiting vision review.

These helpers do not approve source boundaries. They only convert numbered,
exercise-local markers into stable internal record IDs and explicit V2 crops.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


SCALE_180_DPI = 2.5
CONTENT_TOP = 170
CONTENT_BOTTOM_MARGIN = 60
LEFT_COLUMN = (95, 760)
RIGHT_COLUMN = (775, 1435)


@dataclass(frozen=True)
class NumberedMarker:
    printed_number: int
    page_number: int
    x0: float
    top: float


@dataclass(frozen=True)
class ProposedQuestion:
    internal_number: int
    exercise_id: str
    printed_number: int
    marker: NumberedMarker


@dataclass(frozen=True)
class ExerciseMarkerGroup:
    exercise_id: str
    question_markers: tuple[NumberedMarker, ...]
    answer_markers: tuple[NumberedMarker, ...]


def exercise_starts_from_words(
    page_number: int,
    words: tuple[Mapping[str, object], ...],
) -> tuple[tuple[str, int, float], ...]:
    """Return printed exercise labels such as ``1A`` from one source page."""

    result: list[tuple[str, int, float]] = []
    for index, word in enumerate(words[:-1]):
        if str(word.get("text", "")).upper() != "EXERCISE":
            continue
        following = words[index + 1]
        label = str(following.get("text", "")).upper()
        if re.fullmatch(r"\d+[A-Z0-9]*", label) is None:
            continue
        top = float(word["top"])
        if abs(float(following["top"]) - top) <= 3:
            result.append((label, page_number, top))
    return tuple(result)


def numbered_markers_from_words(
    page_number: int,
    words: tuple[Mapping[str, object], ...],
    *,
    minimum_top: float,
    maximum_top: float,
) -> tuple[NumberedMarker, ...]:
    """Return question-like number markers, excluding numbered illustrations."""

    result: list[NumberedMarker] = []
    for index, word in enumerate(words):
        match = re.fullmatch(r"(\d+)\.", str(word.get("text", "")))
        if match is None:
            continue
        top = float(word["top"])
        if top < minimum_top or top > maximum_top:
            continue
        previous = words[index - 1] if index else {}
        if (
            str(previous.get("text", "")).lower().rstrip(".") in {"ex", "example"}
            and abs(float(previous.get("top", top)) - top) <= 3
        ):
            continue
        x0 = float(word["x0"])
        if not (25 <= x0 <= 120 or 290 <= x0 <= 350):
            continue
        result.append(NumberedMarker(int(match.group(1)), page_number, x0, top))
    return tuple(result)


def exercise_windows(
    starts: tuple[tuple[str, int, float], ...],
    *,
    chapter_end_page: int,
) -> tuple[tuple[str, int, float, int, float], ...]:
    """Return inclusive/exclusive source bounds for each ordered exercise."""

    result: list[tuple[str, int, float, int, float]] = []
    for index, (exercise_id, page_number, top) in enumerate(starts):
        if index + 1 < len(starts):
            _, end_page, end_top = starts[index + 1]
        else:
            end_page, end_top = chapter_end_page, float("inf")
        result.append((exercise_id, page_number, top, end_page, end_top))
    return tuple(result)


def _inside_window(
    page_number: int,
    top: float,
    start_page: int,
    start_top: float,
    end_page: int,
    end_top: float,
) -> bool:
    return (page_number, top) >= (start_page, start_top) and (page_number, top) < (end_page, end_top)


def _answer_heading_events(
    pages: Mapping[int, tuple[Mapping[str, object], ...]],
) -> tuple[tuple[int, float], ...]:
    return tuple(
        (page_number, float(word["top"]))
        for page_number, words in pages.items()
        for word in words
        if str(word.get("text", "")).upper() == "ANSWERS"
    )


def exercise_marker_groups(
    pages: Mapping[int, tuple[Mapping[str, object], ...]],
    *,
    chapter_end_page: int,
) -> tuple[ExerciseMarkerGroup, ...]:
    """Separate numbered question and answer markers for every exercise window."""

    starts = tuple(
        event
        for page_number in sorted(pages)
        for event in exercise_starts_from_words(page_number, pages[page_number])
    )
    answers = _answer_heading_events(pages)
    groups: list[ExerciseMarkerGroup] = []
    for exercise_id, start_page, start_top, end_page, end_top in exercise_windows(
        starts, chapter_end_page=chapter_end_page
    ):
        answer_start = next(
            (
                (page_number, top)
                for page_number, top in answers
                if _inside_window(page_number, top, start_page, start_top, end_page, end_top)
            ),
            None,
        )
        question_end_page, question_end_top = answer_start or (end_page, end_top)
        question_markers: list[NumberedMarker] = []
        answer_markers: list[NumberedMarker] = []
        for page_number in sorted(pages):
            words = pages[page_number]
            if page_number < start_page or page_number > end_page:
                continue
            candidates = numbered_markers_from_words(
                page_number,
                words,
                minimum_top=0.0,
                maximum_top=float("inf"),
            )
            for marker in candidates:
                if _inside_window(
                    marker.page_number,
                    marker.top,
                    start_page,
                    start_top,
                    question_end_page,
                    question_end_top,
                ):
                    question_markers.append(marker)
                elif answer_start is not None and _inside_window(
                    marker.page_number,
                    marker.top,
                    answer_start[0],
                    answer_start[1],
                    end_page,
                    end_top,
                ):
                    answer_markers.append(marker)
        question_markers.sort(key=lambda marker: (_region_index(marker), marker.top))
        answer_markers.sort(key=lambda marker: (_region_index(marker), marker.top))
        groups.append(
            ExerciseMarkerGroup(exercise_id, tuple(question_markers), tuple(answer_markers))
        )
    return tuple(groups)


def _column(marker: NumberedMarker) -> int:
    return 0 if marker.x0 < 200 else 1


def _region_index(marker: NumberedMarker) -> int:
    return marker.page_number * 2 + _column(marker)


def _pixel_top(points: float) -> int:
    return max(CONTENT_TOP, round(points * SCALE_180_DPI) - 8)


def number_exercises(
    exercises: tuple[tuple[str, tuple[NumberedMarker, ...]], ...],
) -> tuple[ProposedQuestion, ...]:
    """Assign stable record numbers while retaining each exercise-local number."""

    result: list[ProposedQuestion] = []
    for exercise_id, markers in exercises:
        for marker in markers:
            result.append(
                ProposedQuestion(
                    internal_number=len(result) + 1,
                    exercise_id=exercise_id,
                    printed_number=marker.printed_number,
                    marker=marker,
                )
            )
    return tuple(result)


def explicit_column_segments(
    markers: tuple[NumberedMarker, ...],
    *,
    page_heights: Mapping[int, int],
) -> tuple[tuple[dict[str, int], ...], ...]:
    """Bound each source marker at the next marker in two-column reading order."""

    ordered = tuple(sorted(markers, key=lambda marker: (_region_index(marker), marker.top)))
    result: list[tuple[dict[str, int], ...]] = []
    for position, current in enumerate(ordered):
        following = ordered[position + 1] if position + 1 < len(ordered) else None
        start_region = _region_index(current)
        following_region = _region_index(following) if following is not None else None
        stop_region = (
            start_region + 1
            if following_region is None or following_region == start_region
            else following_region
        )
        if stop_region <= start_region:
            raise ValueError("Source markers must increase in two-column reading order.")
        segments: list[dict[str, int]] = []
        for region in range(start_region, stop_region):
            page_number, column = divmod(region, 2)
            try:
                page_height = page_heights[page_number]
            except KeyError as error:
                raise ValueError(f"Missing rendered height for source page {page_number}.") from error
            left, right = LEFT_COLUMN if column == 0 else RIGHT_COLUMN
            top = _pixel_top(current.top) if region == start_region else CONTENT_TOP
            bottom = page_height - CONTENT_BOTTOM_MARGIN
            if following is not None and region == stop_region - 1 and following_region == region:
                bottom = _pixel_top(following.top)
            if top >= bottom:
                raise ValueError(f"Marker {current.printed_number} creates an empty source crop.")
            segments.append(
                {
                    "page": page_number,
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                }
            )
        result.append(tuple(segments))
    return tuple(result)


def explicit_single_column_segments(
    markers: tuple[NumberedMarker, ...],
    *,
    page_heights: Mapping[int, int],
) -> tuple[tuple[dict[str, int], ...], ...]:
    """Bound full-width verbal question rows by the next numbered source row."""

    ordered = tuple(sorted(markers, key=lambda marker: (marker.page_number, marker.top)))
    result: list[tuple[dict[str, int], ...]] = []
    for position, current in enumerate(ordered):
        following = ordered[position + 1] if position + 1 < len(ordered) else None
        final_page = following.page_number if following is not None else current.page_number
        segments: list[dict[str, int]] = []
        for page_number in range(current.page_number, final_page + 1):
            try:
                page_height = page_heights[page_number]
            except KeyError as error:
                raise ValueError(f"Missing rendered height for source page {page_number}.") from error
            top = _pixel_top(current.top) if page_number == current.page_number else CONTENT_TOP
            bottom = _pixel_top(following.top) if following is not None and page_number == following.page_number else page_height - CONTENT_BOTTOM_MARGIN
            if top >= bottom:
                raise ValueError(f"Marker {current.printed_number} creates an empty source crop.")
            segments.append(
                {"page": page_number, "left": 95, "top": top, "right": 1435, "bottom": bottom}
            )
        result.append(tuple(segments))
    return tuple(result)
