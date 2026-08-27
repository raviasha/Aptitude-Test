"""Deterministic, versioned checks for unsafe textbook text candidates.

These checks identify likely visual-notation corruption.  They deliberately do
not repair text: a finding sends the affected field to image review or
quarantine according to the representation policy.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterator


POLICY_VERSION = 1


@dataclass(frozen=True)
class Finding:
    rule_id: str
    field_path: str
    severity: str
    evidence: str


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    pattern: re.Pattern[str]
    severity: str = "block"


# Each pattern is evaluated on a single field, never on concatenated record
# text.  The numeric-power pattern intentionally requires the surrounding
# division wording found in the source regression, avoiding place-value text.
RULES = (
    _Rule(
        "math.detached_numeric_power",
        re.compile(
            r"(?<![\dA-Za-z])\d{1,3}[ \t]+[2-9]\d?\b(?=[ \t]+is[ \t]+divided\b)",
            re.IGNORECASE,
        ),
    ),
    _Rule(
        "math.flattened_fraction_power",
        re.compile(r"^\s*(?:[2-9]\s+)?1\s+[A-Za-z]\s*$", re.MULTILINE),
    ),
    _Rule(
        "math.detached_parenthesized_power",
        re.compile(
            r"\((?=[^()\r\n]*(?:\d|\?))[^()\r\n]{1,48}\)[ \t]*[2-9]\b"
            r"(?=[ \t]*(?:[+\-\u2212\u2013*/=\u00D7\u00F7?]|$))"
        ),
    ),
    _Rule(
        "layout.option_spill",
        re.compile(r"^\s*\d{4,}\s+[A-Za-z]{1,3}\s+\d{2,}", re.MULTILINE),
    ),
)


def normalize_candidate_text(value: str) -> str:
    """Apply only stable Unicode and whitespace normalization to a candidate."""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(" \t") for line in normalized.split("\n"))


def _text_fields(candidate: dict) -> Iterator[tuple[str, str]]:
    question_text = candidate.get("question_text")
    if isinstance(question_text, str):
        yield "question_text", question_text

    options = candidate.get("options")
    if isinstance(options, dict):
        for label in sorted(options, key=str):
            value = options[label]
            if isinstance(value, str):
                yield f"options.{label}", value

    solution_steps = candidate.get("solution_steps")
    if isinstance(solution_steps, (list, tuple)):
        for index, value in enumerate(solution_steps):
            if isinstance(value, str):
                yield f"solution_steps[{index}]", value


def validate_record(candidate: dict) -> list[Finding]:
    """Return deterministic blocking findings for text fields in *candidate*."""
    findings: list[Finding] = []
    for field_path, value in _text_fields(candidate):
        normalized = normalize_candidate_text(value)
        for rule in RULES:
            match = rule.pattern.search(normalized)
            if match is not None:
                findings.append(
                    Finding(
                        rule_id=rule.rule_id,
                        field_path=field_path,
                        severity=rule.severity,
                        evidence=match.group(0),
                    )
                )
    return findings


def choose_representation(
    field_role: str, recommendation: str, findings: list[Finding]
) -> str:
    """Choose a field representation, failing closed for unsafe inputs."""
    if any(item.severity == "block" for item in findings):
        return "quarantine"
    if recommendation == "image" and field_role in {"question", "option", "solution"}:
        return "image"
    if recommendation == "text":
        return "text"
    return "quarantine"
