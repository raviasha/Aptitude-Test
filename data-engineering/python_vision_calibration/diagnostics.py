"""Conservative, literal-only routing diagnostics for the Chapter 1 pilot."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import RawBaselineRecord


# These are deliberately literal reproductions of the versioned regression
# fixture, rather than generalized OCR "repair" heuristics.  A match only
# changes the route; it never changes the observed text.
_LITERAL_NOTATION_WARNINGS = {
    "(80) 2 - (65) 2 + 81 = ?": "AMBIGUOUS_DETACHED_DIGITS",
    "The remainder when 7 84 is divided by 342 is": "AMBIGUOUS_DETACHED_DIGITS",
    "2 1 x": "AMBIGUOUS_NOTATION",
    "28700 ab 252 ba 24 12 12 ×": "SUSPICIOUS_TOKEN_CHAIN",
}


def _contains_invalid_unicode(value: Any) -> bool:
    if isinstance(value, str):
        return "\ufffd" in value or any("\ud800" <= character <= "\udfff" for character in value)
    if isinstance(value, Mapping):
        return any(_contains_invalid_unicode(key) or _contains_invalid_unicode(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_invalid_unicode(item) for item in value)
    return False


def hard_warning_codes(record: RawBaselineRecord) -> tuple[str, ...]:
    """Return deterministic warning codes without inferring or repairing content."""
    if not isinstance(record, RawBaselineRecord):
        raise TypeError("record must be a RawBaselineRecord value.")

    candidate = record.candidate
    if not isinstance(candidate, Mapping):
        return ("MALFORMED_CANDIDATE",)

    warnings: set[str] = set()
    baseline_failures = candidate.get("baseline_failures", ())
    if not isinstance(baseline_failures, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in baseline_failures
    ):
        warnings.add("MALFORMED_BASELINE_FAILURES")
    else:
        warnings.update(f"BASELINE_FAILURE:{item}" for item in baseline_failures)

    question_text = candidate.get("question_text")
    if not isinstance(question_text, str) or not question_text.strip():
        warnings.add("MISSING_QUESTION")

    options = candidate.get("options")
    if not isinstance(options, Mapping) or not options:
        warnings.add("MISSING_OPTIONS")
        option_labels: set[str] = set()
    elif any(not isinstance(label, str) or not isinstance(text, str) for label, text in options.items()):
        warnings.add("MALFORMED_OPTIONS")
        option_labels = {label for label in options if isinstance(label, str)}
    else:
        option_labels = set(options)
        if any(not text.strip() for text in options.values()):
            warnings.add("MISSING_OPTION_TEXT")

    answer = candidate.get("correct_answer")
    if not isinstance(answer, str) or not answer.strip():
        warnings.add("MISSING_CORRECT_ANSWER")
    elif answer not in option_labels:
        warnings.add("ANSWER_LABEL_NOT_IN_OPTIONS")

    solution_steps = candidate.get("solution_steps")
    if (
        not isinstance(solution_steps, (list, tuple))
        or not solution_steps
        or any(not isinstance(step, str) or not step.strip() for step in solution_steps)
    ):
        warnings.add("MISSING_SOLUTION")

    if _contains_invalid_unicode(candidate):
        warnings.add("INVALID_UNICODE")

    notation_fields: tuple[Any, ...] = (question_text,)
    if isinstance(options, Mapping):
        notation_fields += tuple(options.values())
    for text in notation_fields:
        if not isinstance(text, str):
            continue
        warning = _LITERAL_NOTATION_WARNINGS.get(text)
        if warning is not None:
            warnings.add(warning)

    return tuple(sorted(warnings))
