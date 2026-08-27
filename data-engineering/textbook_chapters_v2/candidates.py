"""Assembly of verified extraction data into package-ready candidates."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import CandidateRecord, PipelineBlocked, RecordEvidence, SourceCrop
from .rules import Finding, choose_representation, normalize_candidate_text
from .store import dependency_fingerprint
from .vision import extraction_job_fingerprint


_OPTION_LABELS = ("A", "B", "C", "D", "E")


def _non_empty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not (text := normalize_candidate_text(value)).strip():
        raise PipelineBlocked(f"Accepted extraction needs non-empty {label}.")
    return text.strip()


def _options(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PipelineBlocked("Accepted extraction needs A-D or A-E options.")
    labels = tuple(sorted(value))
    if labels not in (_OPTION_LABELS[:4], _OPTION_LABELS):
        raise PipelineBlocked("Accepted extraction needs exactly contiguous A-D or A-E options.")
    return {label: _non_empty_text(value[label], f"option {label}") for label in labels}


def _accepted_payload(extraction: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Return record data and attached evidence without accepting ambiguous shapes."""
    if isinstance(extraction, CandidateRecord):
        return {
            "question_text": extraction.question_text,
            "options": extraction.options,
            "correct_answer": extraction.correct_answer,
            "answer_key_answer": extraction.answer_key_answer,
            "answer_key_crop_sha256": extraction.answer_key_crop_sha256,
            "answer_key_job_fingerprint": extraction.answer_key_job_fingerprint,
            "solution_steps": extraction.solution_steps,
            "representation": extraction.representation,
            "source_fingerprint": extraction.source_fingerprint,
        }, {}
    if not isinstance(extraction, Mapping):
        raise TypeError("extraction must be an accepted mapping or CandidateRecord.")
    embedded = extraction.get("candidate")
    if isinstance(embedded, CandidateRecord):
        return {
            "question_text": embedded.question_text,
            "options": embedded.options,
            "correct_answer": embedded.correct_answer,
            "answer_key_answer": embedded.answer_key_answer,
            "answer_key_crop_sha256": embedded.answer_key_crop_sha256,
            "answer_key_job_fingerprint": embedded.answer_key_job_fingerprint,
            "solution_steps": embedded.solution_steps,
            "representation": embedded.representation,
            "source_fingerprint": embedded.source_fingerprint,
        }, extraction
    return extraction, extraction


def _answer_key_evidence(record: Mapping[str, Any], evidence: RecordEvidence) -> str:
    answer_key = record.get("answer_key")
    if isinstance(answer_key, Mapping):
        answer = answer_key.get("correct_answer")
        crop_sha256 = answer_key.get("crop_sha256")
        job_fingerprint = answer_key.get("job_fingerprint")
    else:
        answer = record.get("answer_key_answer")
        crop_sha256 = record.get("answer_key_crop_sha256")
        job_fingerprint = record.get("answer_key_job_fingerprint")
    if not isinstance(answer, str) or answer not in _OPTION_LABELS:
        raise PipelineBlocked("Accepted extraction needs an independently extracted answer-key answer.")
    if not isinstance(crop_sha256, str) or len(crop_sha256) != 64:
        raise PipelineBlocked("Accepted extraction needs an independently extracted answer-key crop hash.")
    source_fingerprint = record.get("source_fingerprint")
    if not isinstance(job_fingerprint, str) or job_fingerprint != source_fingerprint:
        raise PipelineBlocked("Accepted extraction answer-key evidence is not bound to its extraction job.")
    if not any(
        crop.role == "answer_key"
        and crop.question_number == evidence.question_number
        and crop.sha256 == crop_sha256
        for crop in evidence.answer_key_crops
    ):
        raise PipelineBlocked("Accepted extraction answer-key crop is not authorized by this record evidence.")
    return answer


def _field_findings(findings: Sequence[Finding], field: str) -> list[Finding]:
    if field == "solution":
        return [item for item in findings if item.field_path == "solution" or item.field_path.startswith("solution_steps[")]
    return [item for item in findings if item.field_path in {field, field.replace("question", "question_text")}]


def _crop_payload(crop: Any, field: str, alt_text: Any, evidence: RecordEvidence, allowed_crops: Sequence[SourceCrop], expected_role: str) -> dict[str, Any]:
    if not isinstance(crop, SourceCrop):
        raise PipelineBlocked(f"Image representation for {field} needs an approved SourceCrop.")
    if not crop.path.is_file():
        raise PipelineBlocked(f"Approved source crop for {field} does not exist.")
    content = crop.path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != crop.sha256:
        raise PipelineBlocked(f"Approved source crop hash does not match for {field}.")
    if crop.role != expected_role or crop.question_number != evidence.question_number:
        raise PipelineBlocked(f"Approved source crop for {field} is not authorized by this record evidence.")
    if not any(
        approved.role == expected_role
        and approved.question_number == evidence.question_number
        and approved.sha256 == crop.sha256
        for approved in allowed_crops
    ):
        raise PipelineBlocked(f"Approved source crop for {field} is not authorized by this record evidence.")
    text = _non_empty_text(alt_text, f"verified alt text for {field}")
    return {
        "source_path": str(Path(crop.path)),
        "source_sha256": digest,
        "alt_text": text,
        "page_number": crop.page_number,
        "box": {"left": crop.box.left, "top": crop.box.top, "right": crop.box.right, "bottom": crop.box.bottom},
        "source_image_sha256": crop.source_image_sha256,
        "source_dpi": crop.source_dpi,
    }


def _media_payload(
    attached: Mapping[str, Any], modes: Mapping[str, Any], options: Mapping[str, str], evidence: RecordEvidence
) -> dict[str, Any]:
    crops = attached.get("media_crops")
    alt_text = attached.get("alt_text")
    if not isinstance(crops, Mapping):
        crops = {}
    if not isinstance(alt_text, Mapping):
        alt_text = {}
    media: dict[str, Any] = {}
    if modes["question"] == "image":
        media["question"] = _crop_payload(
            crops.get("question"), "question", alt_text.get("question"), evidence, evidence.question_crops, "question"
        )
    option_media: dict[str, Any] = {}
    raw_option_crops = crops.get("options")
    raw_option_alt_text = alt_text.get("options")
    option_crops = raw_option_crops if isinstance(raw_option_crops, Mapping) else {}
    option_alt_text = raw_option_alt_text if isinstance(raw_option_alt_text, Mapping) else {}
    for label in options:
        if modes["options"][label] == "image":
            option_media[label] = _crop_payload(
                option_crops.get(label), f"option {label}", option_alt_text.get(label), evidence, evidence.question_crops, "question"
            )
    if option_media:
        media["options"] = option_media
    if modes["solution"] == "image":
        raw_crops = crops.get("solution")
        raw_alt_text = alt_text.get("solution")
        solution_crops = raw_crops if isinstance(raw_crops, (list, tuple)) else (raw_crops,)
        solution_alt_text = raw_alt_text if isinstance(raw_alt_text, (list, tuple)) else (raw_alt_text,)
        if len(solution_crops) != len(solution_alt_text) or not solution_crops:
            raise PipelineBlocked("Image representation for solution needs approved crops and verified alt text.")
        media["solution"] = [
            _crop_payload(crop, "solution", item_alt_text, evidence, evidence.solution_crops, "solution")
            for crop, item_alt_text in zip(solution_crops, solution_alt_text)
        ]
    return media


def assemble_candidate(evidence: RecordEvidence, extraction: Any, findings: Sequence[Finding]) -> CandidateRecord:
    """Fail closed while turning one accepted extraction into an immutable candidate."""
    if not isinstance(evidence, RecordEvidence):
        raise TypeError("evidence must be a RecordEvidence value.")
    record, attached = _accepted_payload(extraction)
    expected_extraction_fingerprint = extraction_job_fingerprint(evidence)
    if record.get("source_fingerprint") != expected_extraction_fingerprint:
        raise PipelineBlocked("Accepted extraction fingerprint does not match the supplied record evidence.")
    question_text = _non_empty_text(record.get("question_text"), "question_text")
    options = _options(record.get("options"))
    correct_answer = record.get("correct_answer")
    if not isinstance(correct_answer, str) or correct_answer not in options:
        raise PipelineBlocked("Accepted extraction correct_answer must identify an existing option.")
    answer_key_answer = _answer_key_evidence(record, evidence)
    if correct_answer != answer_key_answer:
        raise PipelineBlocked("Vision answer disagrees with independently extracted answer-key evidence.")
    raw_steps = record.get("solution_steps")
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise PipelineBlocked("Accepted extraction needs non-empty solution_steps.")
    solution_steps = tuple(_non_empty_text(item, "solution step") for item in raw_steps)
    representation = record.get("representation")
    if not isinstance(representation, Mapping) or set(representation) < {"question", "options", "solution"}:
        raise PipelineBlocked("Accepted extraction needs representation decisions for question, options, and solution.")
    raw_option_modes = representation.get("options")
    if not isinstance(raw_option_modes, Mapping):
        raise PipelineBlocked("Accepted extraction needs a representation decision for every option.")
    modes: dict[str, Any] = {
        "question": choose_representation("question", str(representation.get("question", "")), _field_findings(findings, "question")),
        "options": {},
        "solution": choose_representation("solution", str(representation.get("solution", "")), _field_findings(findings, "solution")),
    }
    for label in options:
        if label not in raw_option_modes:
            raise PipelineBlocked(f"Accepted extraction needs a representation decision for option {label}.")
        modes["options"][label] = choose_representation(
            "option", str(raw_option_modes[label]), _field_findings(findings, f"options.{label}")
        )
    if modes["question"] == "quarantine" or modes["solution"] == "quarantine" or any(
        mode == "quarantine" for mode in modes["options"].values()
    ):
        raise PipelineBlocked("A quarantined field cannot be assembled into a candidate.")
    media = _media_payload(attached, modes, options, evidence)
    source_fingerprint = record.get("source_fingerprint") or evidence.dependency_fingerprint
    if not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64:
        raise PipelineBlocked("Candidate needs a source dependency fingerprint.")
    candidate_data = {
        "chapter": evidence.chapter,
        "question_number": evidence.question_number,
        "question_text": question_text,
        "options": options,
        "correct_answer": correct_answer,
        "answer_key_answer": answer_key_answer,
        "answer_key_crop_sha256": (
            record["answer_key"]["crop_sha256"]
            if isinstance(record.get("answer_key"), Mapping)
            else record["answer_key_crop_sha256"]
        ),
        "answer_key_job_fingerprint": (
            record["answer_key"]["job_fingerprint"]
            if isinstance(record.get("answer_key"), Mapping)
            else record["answer_key_job_fingerprint"]
        ),
        "solution_steps": solution_steps,
        "representation": {**modes, "media": media},
        "source_fingerprint": source_fingerprint,
    }
    return CandidateRecord(**candidate_data, sha256=dependency_fingerprint(candidate_data))
