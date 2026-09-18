"""Build image-only, chapter-wise ZIP packages from R.S. Aggarwal logical reasoning PDF.

The package deliberately contains rendered source pages only; no OCR text, answers,
or solutions are copied into the ZIP. Chapter boundaries are taken from the book's
printed contents pages, whose page numbering restarts at each section.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
import zipfile


SECTIONS = [
    ("s01_general_mental_ability", "General Mental Ability", 17, [
        (1, "analogy", "Analogy", 1),
        (2, "classification", "Classification", 95),
        (3, "series_completion", "Series Completion", 139),
        (4, "coding_decoding", "Coding-Decoding", 169),
        (5, "blood_relations", "Blood Relations", 220),
        (6, "puzzle_test", "Puzzle Test", 242),
        (7, "sequential_output_tracing", "Sequential Output Tracing", 318),
        (8, "direction_sense_test", "Direction Sense Test", 324),
        (9, "logical_venn_diagrams", "Logical Venn Diagrams", 346),
        (10, "alphabet_test", "Alphabet Test", 384),
        (11, "number_ranking_time_sequence_test", "Number, Ranking & Time Sequence Test", 417),
        (12, "mathematical_operations", "Mathematical Operations", 433),
        (13, "logical_sequence_of_words", "Logical Sequence of Words", 455),
        (14, "arithmetical_reasoning", "Arithmetical Reasoning", 459),
        (15, "inserting_missing_character", "Inserting the Missing Character", 475),
        (16, "data_sufficiency", "Data Sufficiency", 495),
        (17, "decision_making", "Decision Making", 507),
        (18, "assertion_and_reason", "Assertion and Reason", 540),
        (19, "situation_reaction_test", "Situation Reaction Test", 551),
        (20, "verification_of_truth", "Verification of Truth of the Statement", 556),
    ]),
    ("s02_logical_deduction", "Logical Deduction", 577, [
        (1, "logic", "Logic", 1),
        (2, "statement_arguments", "Statement - Arguments", 30),
        (3, "statement_assumptions", "Statement - Assumptions", 48),
        (4, "statement_courses_of_action", "Statement - Courses of Action", 85),
        (5, "statement_conclusions", "Statement - Conclusions", 101),
        (6, "deriving_conclusions_from_passages", "Deriving Conclusions from Passages", 123),
        (7, "theme_detection", "Theme Detection", 146),
        (8, "question_statements", "Question - Statements", 152),
        (9, "miscellaneous_logical_puzzles", "Miscellaneous Logical Puzzles", 156),
    ]),
    ("s03_non_verbal_reasoning", "Non-Verbal Reasoning", 736, [
        (1, "series", "Series", 1),
        (2, "analogy", "Analogy", 136),
        (3, "classification", "Classification", 206),
        (4, "analytical_reasoning", "Analytical Reasoning", 241),
        (5, "mirror_images", "Mirror Images", 267),
        (6, "water_images", "Water Images", 278),
        (7, "embedded_figures", "Embedded Figures", 289),
        (8, "completion_of_incomplete_pattern", "Completion of Incomplete Pattern", 303),
        (9, "figure_matrix", "Figure Matrix", 313),
        (10, "paper_folding", "Paper Folding", 321),
        (11, "paper_cutting", "Paper Cutting", 327),
        (12, "rule_detection", "Rule Detection", 343),
        (13, "grouping_of_identical_figures", "Grouping of Identical Figures", 346),
        (14, "cubes_and_dice", "Cubes and Dice", 351),
        (15, "dot_situation", "Dot Situation", 395),
        (16, "construction_of_squares_and_triangles", "Construction of Squares & Triangles", 404),
        (17, "figure_formation_and_analysis", "Figure Formation & Analysis", 416),
        (18, "practice_question_set", "Practice Question Set", 422),
    ]),
]


@dataclass(frozen=True)
class ChapterSpec:
    section_id: str
    section_title: str
    chapter: int
    slug: str
    title: str
    printed_page_start: int
    printed_page_end: int | None
    pdf_page_start: int
    pdf_page_end: int
    output_prefix: str
    book_name: str


@dataclass(frozen=True)
class BookProfile:
    key: str
    book_name: str
    sections: tuple
    page_count_hint: int
    output_prefix: str
    section_end_hints: tuple[int | None, ...] = ()


NEW_REASONING_SECTIONS = [
    ("new_reasoning", "Reasoning for Competitions", 5, [
        (1, "coding_decoding", "Coding-Decoding", 1),
        (2, "alphabet_test", "Alphabet Test", 35),
    ]),
]


def get_book_profile(name: str) -> BookProfile:
    """Return a renderer profile without changing the legacy profile."""
    if name == "modern_approach":
        return BookProfile(
            key=name,
            book_name="A Modern Approach to Logical Reasoning",
            sections=tuple(SECTIONS),
            page_count_hint=1171,
            output_prefix="logical_reasoning",
            section_end_hints=(),
        )
    if name == "new_reasoning":
        return BookProfile(
            key=name,
            book_name="A New Approach to Reasoning: Reasoning for Competitions",
            sections=tuple(NEW_REASONING_SECTIONS),
            page_count_hint=576,
            output_prefix="logical_reasoning_new",
            section_end_hints=(58,),
        )
    raise ValueError(f"Unknown book profile: {name}")


def _expand_sections(profile: BookProfile, page_count: int) -> list[ChapterSpec]:
    chapters: list[ChapterSpec] = []
    for section_index, (section_id, section_title, section_pdf_start, section_chapters) in enumerate(profile.sections):
        section_end = (
            profile.sections[section_index + 1][2] - 1
            if section_index + 1 < len(profile.sections)
            else (profile.section_end_hints[section_index] if section_index < len(profile.section_end_hints) and profile.section_end_hints[section_index] else page_count)
        )
        for position, (chapter_no, slug, title, printed_start) in enumerate(section_chapters):
            printed_end = section_chapters[position + 1][3] - 1 if position + 1 < len(section_chapters) else None
            pdf_start = section_pdf_start + printed_start - 1
            pdf_end = section_pdf_start + printed_end - 1 if printed_end is not None else section_end
            chapters.append(ChapterSpec(
                section_id=section_id,
                section_title=section_title,
                chapter=chapter_no,
                slug=slug,
                title=title,
                printed_page_start=printed_start,
                printed_page_end=printed_end,
                pdf_page_start=pdf_start,
                pdf_page_end=pdf_end,
                output_prefix=profile.output_prefix,
                book_name=profile.book_name,
            ))
    return chapters


def select_chapters(profile: BookProfile, chapter_filter: str, page_count: int | None = None) -> list[ChapterSpec]:
    """Select chapters from a profile using a comma-separated list/range."""
    available = _expand_sections(profile, page_count or profile.page_count_hint)
    wanted: set[int] = set()
    for item in chapter_filter.split(","):
        token = item.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError("Chapter range must be ascending.")
            wanted.update(range(start, end + 1))
        else:
            wanted.add(int(token))
    selected = [chapter for chapter in available if chapter.chapter in wanted]
    if not selected or {chapter.chapter for chapter in selected} != wanted:
        available_numbers = sorted({chapter.chapter for chapter in available})
        raise ValueError(f"Requested chapters are unavailable; available chapters: {available_numbers}")
    return selected


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(
    source_pdf: Path,
    output_dir: Path,
    scale: float,
    jpeg_quality: int,
    profile_name: str = "modern_approach",
    chapter_filter: str | None = None,
) -> None:
    import pypdfium2 as pdfium

    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = sha256(source_pdf)
    pdf = pdfium.PdfDocument(str(source_pdf))
    page_count = len(pdf)
    profile = get_book_profile(profile_name)
    chapters = select_chapters(profile, chapter_filter or "1-999", page_count) if chapter_filter else _expand_sections(profile, page_count)
    index = []
    temp_dir = Path(tempfile.mkdtemp(prefix="logical-reasoning-vision-", dir=output_dir))
    try:
        for chapter in chapters:
                section_id = chapter.section_id
                section_title = chapter.section_title
                chapter_no = chapter.chapter
                slug = chapter.slug
                title = chapter.title
                printed_start = chapter.printed_page_start
                printed_end = chapter.printed_page_end
                pdf_start = chapter.pdf_page_start
                pdf_end = chapter.pdf_page_end
                if not (1 <= pdf_start <= pdf_end <= page_count):
                    raise ValueError(f"Invalid page range for {section_id} chapter {chapter_no}: {pdf_start}-{pdf_end}")
                filename = f"{chapter.output_prefix}_ch{chapter_no:02d}_{slug}_all_vision.zip"
                destination = output_dir / filename
                staging = temp_dir / f"{chapter.output_prefix}_{section_id}_ch{chapter_no:02d}"
                staging.mkdir()
                pages = []
                for pdf_page_number in range(pdf_start, pdf_end + 1):
                    image_name = f"pages/page-{pdf_page_number:04d}.jpg"
                    image_path = staging / f"page-{pdf_page_number:04d}.jpg"
                    page = pdf[pdf_page_number - 1]
                    image = page.render(scale=scale).to_pil().convert("RGB")
                    image.save(image_path, "JPEG", quality=jpeg_quality, optimize=True, progressive=True)
                    pages.append({"pdf_page": pdf_page_number, "file": image_name})
                manifest = {
                    "format_version": 1,
                    "package_type": "all_vision_image_only",
                    "book": chapter.book_name,
                    "section": section_title,
                    "section_id": section_id,
                    "chapter": chapter_no,
                    "chapter_title": title,
                    "printed_page_start": printed_start,
                    "printed_page_end": printed_end,
                    "pdf_page_start": pdf_start,
                    "pdf_page_end": pdf_end,
                    "source_pdf_sha256": source_hash,
                    "notes": "Source pages are rendered as images. No OCR text, answers, or solutions are included.",
                    "pages": pages,
                }
                (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                    archive.write(staging / "manifest.json", "manifest.json")
                    for item in pages:
                        archive.write(staging / Path(item["file"]).name, item["file"])
                temporary.replace(destination)
                index.append({"file": filename, "section": section_title, "chapter": chapter_no, "chapter_title": title, "pdf_pages": [pdf_start, pdf_end], "page_count": len(pages)})
                print(f"Built {filename} ({len(pages)} pages)")
        (output_dir / "logical_reasoning_vision_index.json").write_text(json.dumps({"book_profile": profile.key, "book": profile.book_name, "source_pdf": source_pdf.name, "source_pdf_sha256": source_hash, "packages": index}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        for path in sorted(temp_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        temp_dir.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("question-banks/logical-reasoning-vision"))
    parser.add_argument("--scale", type=float, default=1.5)
    parser.add_argument("--jpeg-quality", type=int, default=86)
    parser.add_argument("--profile", choices=("modern_approach", "new_reasoning"), default="modern_approach")
    parser.add_argument("--chapters", help="Comma-separated chapter numbers and/or ascending ranges, e.g. 1-2")
    args = parser.parse_args()
    build(args.source_pdf.resolve(), args.output_dir.resolve(), args.scale, args.jpeg_quality, args.profile, args.chapters)


if __name__ == "__main__":
    main()
