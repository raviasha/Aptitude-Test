"""Validated immutable chapter configuration for the V2 pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .models import frozen_mapping


def _page_range(raw: Any, name: str) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"{name} must contain exactly a start and end page.")
    start, end = raw
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise ValueError(f"{name} must contain integer page numbers.")
    if start <= 0 or end <= 0 or start > end:
        raise ValueError(f"{name} must be an increasing positive page range.")
    return start, end


def _question_range(raw: Any) -> tuple[int, int]:
    return _page_range(raw, "question_numbers")


@dataclass(frozen=True)
class ChapterConfig:
    chapter: int
    bank_name: str
    question_pages: tuple[int, int]
    answer_pages: tuple[int, int]
    solution_pages: tuple[int, int]
    question_numbers: tuple[int, int]
    intentional_exclusions: tuple[int, ...] = ()
    marker_overrides: Mapping[str, Any] = field(default_factory=frozen_mapping)
    layout_boundaries: Mapping[str, Any] = field(default_factory=frozen_mapping)
    extras: Mapping[str, Any] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "intentional_exclusions", tuple(self.intentional_exclusions))
        object.__setattr__(self, "marker_overrides", frozen_mapping(self.marker_overrides))
        object.__setattr__(self, "layout_boundaries", frozen_mapping(self.layout_boundaries))
        object.__setattr__(self, "extras", frozen_mapping(self.extras))

    @property
    def answer_key_pages(self) -> tuple[int, int]:
        """Compatibility alias for early V2 callers."""
        return self.answer_pages

    @classmethod
    def load(cls, path: Path) -> "ChapterConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid chapter configuration JSON: {path}") from error
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChapterConfig":
        if not isinstance(raw, Mapping):
            raise ValueError("chapter configuration must be a JSON object.")
        chapter = raw.get("chapter")
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0:
            raise ValueError("chapter must be a positive integer.")

        question_pages = _page_range(raw.get("question_pages"), "question_pages")
        answer_pages = _page_range(raw.get("answer_pages", raw.get("answer_key_pages")), "answer_pages")
        solution_pages = _page_range(raw.get("solution_pages"), "solution_pages")
        question_numbers = _question_range(raw.get("question_numbers", raw.get("expected_question_numbers")))

        exclusions = raw.get("intentional_exclusions", ())
        if not isinstance(exclusions, (list, tuple)) or any(isinstance(number, bool) or not isinstance(number, int) for number in exclusions):
            raise ValueError("intentional_exclusions must be a list of question numbers.")
        if len(set(exclusions)) != len(exclusions) or any(number < question_numbers[0] or number > question_numbers[1] for number in exclusions):
            raise ValueError("intentional_exclusions must be unique configured question numbers.")

        known = {
            "chapter", "bank_name", "question_pages", "answer_key_pages", "answer_pages", "solution_pages",
            "question_numbers", "expected_question_numbers", "intentional_exclusions", "marker_overrides", "layout_boundaries",
        }
        return cls(
            chapter=chapter,
            bank_name=str(raw.get("bank_name") or f"chapter-{chapter:03d}"),
            question_pages=question_pages,
            answer_pages=answer_pages,
            solution_pages=solution_pages,
            question_numbers=question_numbers,
            intentional_exclusions=tuple(exclusions),
            marker_overrides=frozen_mapping(raw.get("marker_overrides", {})),
            layout_boundaries=frozen_mapping(raw.get("layout_boundaries", {})),
            extras=frozen_mapping({key: value for key, value in raw.items() if key not in known}),
        )
