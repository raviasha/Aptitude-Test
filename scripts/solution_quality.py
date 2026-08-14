"""Audit PDF-derived solution text for formula/layout corruption."""

from __future__ import annotations

import csv
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any, Iterable


CRITICAL_RULES = {
    "private_use_glyph": re.compile(r"[\uE000-\uF8FF]"),
    "mojibake": re.compile(r"[\u00c3\u00c2]|\u00e2(?:\u201a|\u20ac|\u02c6|\u2030)"),
    "cyrillic_multiplication_artifact": re.compile(r"(?<![\u0400-\u04FF])[\u0432\u0412](?![\u0400-\u04FF])"),
    "empty_layout_braces": re.compile(r"\{\s*\}"),
    "repeated_operator_fragment": re.compile(r"(?:×=|=×|==|%%|%\s+%|×\s+×)"),
    "detached_digit_array": re.compile(r"(?:\b\d\s+){5,}\d\b"),
    "concatenated_formula_numbers": re.compile(r"\b\d{4,}\s+\d{4,}\b"),
}

REVIEW_RULES = {
    "raw_backslash_separator": re.compile(r"(?:^|\s)\\(?:\s|$)"),
    "dense_number_sequence": re.compile(r"(?:\b\d{2,}\s+){4,}\d{2,}\b"),
}


def solution_text(question: dict[str, Any]) -> str:
    steps = question.get("solution_steps") or []
    return "\n".join(str(step) for step in steps)


def audit_question(question: dict[str, Any]) -> dict[str, Any] | None:
    text = solution_text(question)
    issues = [name for name, pattern in CRITICAL_RULES.items() if pattern.search(text)]
    review_issues = [name for name, pattern in REVIEW_RULES.items() if pattern.search(text)]
    if not text.strip():
        review_issues.append("missing_solution_steps")
    if not issues and not review_issues:
        return None
    return {
        "key": question.get("key", ""),
        "category": question.get("category", ""),
        "chapter": question.get("chapter", ""),
        "severity": "critical" if issues else "review",
        "issues": issues + review_issues,
        "question_text": question.get("question_text", ""),
        "correct_answer": question.get("correct_answer", ""),
        "solution_steps": question.get("solution_steps") or [],
    }


def audit_questions(questions: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    questions = list(questions)
    records = [record for question in questions if (record := audit_question(question))]
    severity_counts = Counter(record["severity"] for record in records)
    issue_counts = Counter(issue for record in records for issue in record["issues"])
    chapter_counts = Counter((record["category"], record["chapter"], record["severity"]) for record in records)
    summary = {
        "questions_audited": len(questions),
        "clean_questions": len(questions) - len(records),
        "flagged_questions": len(records),
        "critical_questions": severity_counts["critical"],
        "review_questions": severity_counts["review"],
        "issue_counts": dict(sorted(issue_counts.items())),
        "chapter_counts": [
            {"category": category, "chapter": chapter, "severity": severity, "count": count}
            for (category, chapter, severity), count in sorted(chapter_counts.items())
        ],
    }
    return records, summary


def write_audit_reports(destination: Path, records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (destination / "flagged-solutions.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("key", "category", "chapter", "severity", "issues", "question_text", "correct_answer", "solution_steps"))
        writer.writeheader()
        for record in records:
            writer.writerow({**record, "issues": "; ".join(record["issues"]), "solution_steps": " | ".join(record["solution_steps"])})
    lines = [
        "# Quantitative Aptitude Solution Audit",
        "",
        f"- Questions audited: {summary['questions_audited']:,}",
        f"- Clean: {summary['clean_questions']:,}",
        f"- Critical (hidden from students until verified): {summary['critical_questions']:,}",
        f"- Review recommended: {summary['review_questions']:,}",
        "",
        "## Chapter queue",
        "",
        "| Category | Chapter | Severity | Count |",
        "|---|---|---:|---:|",
    ]
    lines.extend(
        f"| {item['category']} | {item['chapter']} | {item['severity']} | {item['count']} |"
        for item in summary["chapter_counts"]
    )
    (destination / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def has_critical_solution_artifacts(steps: list[str]) -> bool:
    text = "\n".join(steps)
    return any(pattern.search(text) for pattern in CRITICAL_RULES.values())
