"""Contents-defined chapter map for *A Modern Approach to Logical Reasoning*."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterEntry:
    """One contiguous PDF chapter with a stable V2 pipeline identity."""

    pipeline_id: int
    section_id: str
    section_title: str
    chapter_number: int
    slug: str
    title: str
    pdf_page_start: int
    pdf_page_end: int


def _section(
    pipeline_start: int,
    section_id: str,
    section_title: str,
    pdf_page_start: int,
    pdf_page_end: int,
    contents: tuple[tuple[str, str, int], ...],
) -> tuple[ChapterEntry, ...]:
    entries: list[ChapterEntry] = []
    for index, (slug, title, printed_page_start) in enumerate(contents):
        next_printed_start = contents[index + 1][2] if index + 1 < len(contents) else None
        chapter_start = pdf_page_start + printed_page_start - 1
        chapter_end = pdf_page_start + next_printed_start - 2 if next_printed_start else pdf_page_end
        entries.append(
            ChapterEntry(
                pipeline_id=pipeline_start + index,
                section_id=section_id,
                section_title=section_title,
                chapter_number=index + 1,
                slug=slug,
                title=title,
                pdf_page_start=chapter_start,
                pdf_page_end=chapter_end,
            )
        )
    return tuple(entries)


_GENERAL_MENTAL_ABILITY = _section(
    101,
    "s01",
    "General Mental Ability",
    17,
    576,
    (
        ("analogy", "Analogy", 1),
        ("classification", "Classification", 95),
        ("series_completion", "Series Completion", 139),
        ("coding_decoding", "Coding-Decoding", 169),
        ("blood_relations", "Blood Relations", 220),
        ("puzzle_test", "Puzzle Test", 242),
        ("sequential_output_tracing", "Sequential Output Tracing", 318),
        ("direction_sense_test", "Direction Sense Test", 324),
        ("logical_venn_diagrams", "Logical Venn Diagrams", 346),
        ("alphabet_test", "Alphabet Test", 384),
        ("number_ranking_time_sequence_test", "Number, Ranking & Time Sequence Test", 417),
        ("mathematical_operations", "Mathematical Operations", 433),
        ("logical_sequence_of_words", "Logical Sequence of Words", 455),
        ("arithmetical_reasoning", "Arithmetical Reasoning", 459),
        ("inserting_missing_character", "Inserting the Missing Character", 475),
        ("data_sufficiency", "Data Sufficiency", 495),
        ("decision_making", "Decision Making", 507),
        ("assertion_and_reason", "Assertion and Reason", 540),
        ("situation_reaction_test", "Situation Reaction Test", 551),
        ("verification_of_truth", "Verification of Truth of the Statement", 556),
    ),
)

_LOGICAL_DEDUCTION = _section(
    121,
    "s02",
    "Logical Deduction",
    577,
    735,
    (
        ("logic", "Logic", 1),
        ("statement_arguments", "Statement - Arguments", 30),
        ("statement_assumptions", "Statement - Assumptions", 48),
        ("statement_courses_of_action", "Statement - Courses of Action", 85),
        ("statement_conclusions", "Statement - Conclusions", 101),
        ("deriving_conclusions_from_passages", "Deriving Conclusions from Passages", 123),
        ("theme_detection", "Theme Detection", 146),
        ("question_statements", "Question - Statements", 152),
        ("miscellaneous_logical_puzzles", "Miscellaneous Logical Puzzles", 156),
    ),
)

_NON_VERBAL_REASONING = _section(
    130,
    "s03",
    "Non-Verbal Reasoning",
    736,
    1171,
    (
        ("series", "Series", 1),
        ("analogy", "Analogy", 136),
        ("classification", "Classification", 206),
        ("analytical_reasoning", "Analytical Reasoning", 241),
        ("mirror_images", "Mirror Images", 267),
        ("water_images", "Water Images", 278),
        ("embedded_figures", "Embedded Figures", 289),
        ("completion_of_incomplete_pattern", "Completion of Incomplete Pattern", 303),
        ("figure_matrix", "Figure Matrix", 313),
        ("paper_folding", "Paper Folding", 321),
        ("paper_cutting", "Paper Cutting", 327),
        ("rule_detection", "Rule Detection", 343),
        ("grouping_of_identical_figures", "Grouping of Identical Figures", 346),
        ("cubes_and_dice", "Cubes and Dice", 351),
        ("dot_situation", "Dot Situation", 395),
        ("construction_of_squares_and_triangles", "Construction of Squares & Triangles", 404),
        ("figure_formation_and_analysis", "Figure Formation & Analysis", 416),
        ("practice_question_set", "Practice Question Set", 422),
    ),
)

_CHAPTERS = _GENERAL_MENTAL_ABILITY + _LOGICAL_DEDUCTION + _NON_VERBAL_REASONING


def chapters() -> tuple[ChapterEntry, ...]:
    """Return every chapter in the book's physical source order."""

    return _CHAPTERS
