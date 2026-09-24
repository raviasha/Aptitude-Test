"""Canonical browser-integrity event definitions and faculty-facing summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, Mapping, Sequence


EvidenceClass = Literal["blocked_action", "visibility_change", "monitoring_anomaly"]


@dataclass(frozen=True, slots=True)
class IntegrityDefinition:
    code: str
    title: str
    explanation: str
    evidence_class: EvidenceClass
    counts_as_violation: bool = True


def _definition(
    code: str,
    title: str,
    explanation: str,
    evidence_class: EvidenceClass,
    *,
    counts_as_violation: bool = True,
) -> IntegrityDefinition:
    return IntegrityDefinition(code, title, explanation, evidence_class, counts_as_violation)


DEFINITIONS = {
    item.code: item
    for item in (
        _definition("fullscreen_exited", "Exited full-screen mode", "The exam page observed that full-screen mode ended. This records the browser state and does not establish intent.", "visibility_change"),
        _definition("visibility_hidden", "Exam page became hidden", "The browser reported that the exam tab or window was hidden, including when minimized. This does not establish why it happened.", "visibility_change"),
        _definition("focus_lost", "Changed tab, window, or minimized the exam", "The exam window stopped receiving keyboard focus. Switching windows and operating-system prompts can both cause this event.", "visibility_change"),
        _definition("browser_page_hidden", "Exam page was left or closed", "The browser reported that the exam page was being hidden, closed, or moved out of the active page lifecycle.", "visibility_change"),
        _definition("browser_page_reloaded", "Exam page was reloaded", "The client found saved monitoring state when the exam page loaded again. A reload or browser restart may cause this event.", "monitoring_anomaly"),
        _definition("browser_frozen", "Exam page was suspended", "The browser reported that it suspended the exam page. Browser resource management or device load may cause this event.", "monitoring_anomaly"),
        _definition("browser_monitor_gap", "Browser monitoring interrupted (cause unverified)", "The page stopped sending monitoring heartbeats for longer than expected. Browser suspension, reload, device load, or connectivity may cause it; the cause is unverified.", "monitoring_anomaly"),
        _definition("browser_monitor_restarted", "Client monitoring restarted", "The client monitoring process restarted during the assessment. This is a technical event and does not establish why it restarted.", "monitoring_anomaly"),
        _definition("browser_storage_unavailable", "Browser integrity storage unavailable", "The browser could not read or write the local integrity-event queue, or found invalid saved data.", "monitoring_anomaly"),
        _definition("copy", "Attempted to copy exam content", "The client blocked a browser copy action while the assessment was active.", "blocked_action"),
        _definition("cut", "Attempted to cut exam content", "The client blocked a browser cut action while the assessment was active.", "blocked_action"),
        _definition("paste", "Attempted to paste into the exam", "The client blocked a browser paste action while the assessment was active.", "blocked_action"),
        _definition("dragstart", "Drag action blocked", "The client blocked the start of a browser drag action while the assessment was active.", "blocked_action"),
        _definition("drop", "Drop action blocked", "The client blocked a browser drop action while the assessment was active.", "blocked_action"),
        _definition("print_attempt", "Print action blocked", "The client blocked a browser print action while the assessment was active.", "blocked_action"),
        _definition("shortcut_c", "Copy shortcut blocked", "The client blocked the copy keyboard shortcut while the assessment was active.", "blocked_action"),
        _definition("shortcut_x", "Cut shortcut blocked", "The client blocked the cut keyboard shortcut while the assessment was active.", "blocked_action"),
        _definition("shortcut_v", "Paste shortcut blocked", "The client blocked the paste keyboard shortcut while the assessment was active.", "blocked_action"),
        _definition("shortcut_p", "Print shortcut blocked", "The client blocked the print keyboard shortcut while the assessment was active.", "blocked_action"),
        _definition("shortcut_s", "Save shortcut blocked", "The client blocked the save keyboard shortcut while the assessment was active.", "blocked_action"),
        _definition("context_menu", "Blocked browser menu request (informational)", "The client blocked a right-click or equivalent browser menu request. This historical event is informational and is not counted as a violation.", "blocked_action", counts_as_violation=False),
    )
}

ALIASES = {
    "contextmenu": "context_menu",
    "fullscreen_exit": "fullscreen_exited",
}

CLIENT_EMITTED_CODES = frozenset(
    code for code in DEFINITIONS if code != "context_menu"
)

_UNKNOWN = IntegrityDefinition(
    code="unknown",
    title="Unrecognized integrity event",
    explanation="The installed client reported an event that this coordinator version does not recognize. Review the technical details and client version before drawing a conclusion.",
    evidence_class="monitoring_anomaly",
    counts_as_violation=True,
)


def canonical_integrity_code(code: str) -> str:
    return ALIASES.get(code, code)


def integrity_definition(code: str) -> IntegrityDefinition:
    canonical = canonical_integrity_code(code)
    definition = DEFINITIONS.get(canonical)
    if definition is not None:
        return definition
    return IntegrityDefinition(
        code=canonical,
        title=_UNKNOWN.title,
        explanation=_UNKNOWN.explanation,
        evidence_class=_UNKNOWN.evidence_class,
        counts_as_violation=_UNKNOWN.counts_as_violation,
    )


def accepted_integrity_codes() -> frozenset[str]:
    return frozenset(DEFINITIONS) | frozenset(ALIASES)


def count_integrity_violations(events: Iterable[object]) -> int:
    count = 0
    for event in events:
        if isinstance(event, Mapping):
            code = str(event.get("violation_type") or event.get("event_type") or "")
        else:
            code = str(getattr(event, "event_type", getattr(event, "violation_type", "")))
        count += int(integrity_definition(code).counts_as_violation)
    return count


def _row_value(row: object, *names: str) -> str:
    if isinstance(row, Mapping) or hasattr(row, "keys"):
        keys = set(row.keys())
        for name in names:
            if name in keys:
                return str(row[name])
    for name in names:
        value = getattr(row, name, None)
        if value is not None:
            return str(value)
    return ""


def _parsed_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def summarize_integrity_events(rows: Sequence[object]) -> list[dict]:
    """Return readable incidents, grouping only adjacent matching visibility events."""

    incidents: list[dict] = []
    for row in rows:
        original = _row_value(row, "violation_type", "event_type", "code")
        occurred_at = _row_value(row, "occurred_at")
        definition = integrity_definition(original)
        detail = {"occurred_at": occurred_at}
        if original != definition.code:
            detail["original_code"] = original
        previous = incidents[-1] if incidents else None
        can_group = False
        if (
            previous
            and definition.evidence_class == "visibility_change"
            and previous["canonical_code"] == definition.code
        ):
            earlier = _parsed_timestamp(previous["details"][-1]["occurred_at"])
            current = _parsed_timestamp(occurred_at)
            can_group = earlier is not None and current is not None and 0 <= (current - earlier).total_seconds() <= 2
        if can_group:
            previous["details"].append(detail)
            continue
        incident = {
            "canonical_code": definition.code,
            "title": definition.title,
            "explanation": definition.explanation,
            "evidence_class": definition.evidence_class,
            "occurred_at": occurred_at,
            "counts_as_violation": definition.counts_as_violation,
            "details": [detail],
        }
        if original != definition.code or definition.title == _UNKNOWN.title:
            incident["original_code"] = original
        incidents.append(incident)
    return incidents
