"""Stable PDF rendering and reviewed, record-level source evidence."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from .config import ChapterConfig
from .models import CropBox, RecordEvidence, SourceCrop, SourceImage, frozen_mapping
from .store import ArtifactStore, dependency_fingerprint


DEFAULT_DPI = 180
PNG_COMPRESSION_LEVEL = 9
PERMISSION_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)


def sha256_path(path: Path) -> str:
    """Return the SHA-256 digest of a filesystem artifact."""
    for attempt in range(len(PERMISSION_RETRY_DELAYS) + 1):
        try:
            digest = hashlib.sha256()
            with Path(path).open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except PermissionError:
            if attempt == len(PERMISSION_RETRY_DELAYS):
                raise
            time.sleep(PERMISSION_RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def locate_pdftoppm() -> Path:
    """Locate Poppler using the established runtime discovery order."""
    configured = os.environ.get("PDFTOPPM")
    if configured and Path(configured).is_file():
        return Path(configured)
    discovered = shutil.which("pdftoppm")
    if discovered:
        return Path(discovered)
    candidates = [
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "native"
        / "poppler"
        / "Library"
        / "bin"
        / "pdftoppm.exe"
    ]
    candidates.extend(
        Path.home().glob(
            ".cache/codex-runtimes/*/dependencies/native/poppler/Library/bin/pdftoppm.exe"
        )
    )
    match = next((path for path in candidates if path.is_file()), None)
    if match is None:
        raise FileNotFoundError(
            "pdftoppm was not found. Install Poppler or set PDFTOPPM to its executable path."
        )
    return match


def _write_stable_png(image: Image.Image, output_path: Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_path.parent, prefix=f".{output_path.stem}.", suffix=".tmp", delete=False
        ) as temporary_file:
            temporary_name = temporary_file.name
            image.save(
                temporary_file,
                format="PNG",
                optimize=False,
                compress_level=PNG_COMPRESSION_LEVEL,
            )
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, output_path)
    except BaseException:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def render_page(pdf_path: Path, page_number: int, dpi: int, output_path: Path) -> SourceImage:
    """Render one PDF page with Poppler and normalize its PNG encoding."""
    pdf_path = Path(pdf_path)
    output_path = Path(output_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Source PDF does not exist: {pdf_path}")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number <= 0:
        raise ValueError("page_number must be a positive integer.")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer.")
    if output_path.suffix.lower() != ".png":
        raise ValueError("output_path must have a .png suffix.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = Path(tempfile.mkdtemp(dir=output_path.parent, prefix=f".{output_path.stem}.render-"))
    try:
        render_prefix = temporary_directory / "page"
        rendered_path = render_prefix.with_suffix(".png")
        subprocess.run(
            [
                str(locate_pdftoppm()),
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-r",
                str(dpi),
                "-png",
                "-singlefile",
                str(pdf_path),
                str(render_prefix),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if not rendered_path.is_file():
            raise RuntimeError(f"Poppler did not create a fresh render for {output_path}.")
        with Image.open(rendered_path) as rendered:
            rendered.load()
            normalized = rendered.copy()
        _write_stable_png(normalized, output_path)
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)
    return SourceImage(output_path, page_number, dpi, sha256_path(output_path))


def _validated_box(box: CropBox, width: int, height: int) -> None:
    values = (box.left, box.top, box.right, box.bottom)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("Crop coordinates must be integers.")
    if box.left < 0 or box.top < 0 or box.right > width or box.bottom > height:
        raise ValueError("Crop coordinates must stay within the source image.")
    if box.left >= box.right or box.top >= box.bottom:
        raise ValueError("Crop coordinates must define a non-empty region.")


def crop_region(page: SourceImage, box: CropBox, output_path: Path) -> SourceCrop:
    """Write a stable, hash-addressable crop from one rendered source page."""
    if not Path(page.path).is_file():
        raise FileNotFoundError(f"Rendered source page does not exist: {page.path}")
    if sha256_path(page.path) != page.sha256:
        raise ValueError(f"Rendered source page hash does not match provenance: {page.path}")
    with Image.open(page.path) as image:
        image.load()
        _validated_box(box, image.width, image.height)
        cropped = image.crop((box.left, box.top, box.right, box.bottom))
    _write_stable_png(cropped, Path(output_path))
    return SourceCrop(
        role="",
        question_number=0,
        page_number=page.page_number,
        box=box,
        path=Path(output_path),
        width=cropped.width,
        height=cropped.height,
        sha256=sha256_path(Path(output_path)),
        source_image_sha256=page.sha256,
        source_dpi=page.dpi,
    )


def _marker_mapping(config: ChapterConfig, role: str) -> Mapping[str, Any]:
    aliases = {
        "question": ("question", "questions", "question_markers"),
        "answer_key": ("answer_key", "answer", "answers", "answer_markers", "answer_key_markers"),
        "solution": ("solution", "solutions", "solution_markers"),
    }
    for key in aliases[role]:
        value = config.marker_overrides.get(key)
        if isinstance(value, Mapping):
            return value
    raise ValueError(f"Missing reviewed {role} markers in marker_overrides.")


def _marker(raw: Any, role: str, question_number: int) -> Mapping[str, int]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"Reviewed {role} marker for question {question_number} must be an object.")
    page = raw.get("page")
    top = raw.get("top")
    if (
        isinstance(page, bool)
        or isinstance(top, bool)
        or not isinstance(page, int)
        or not isinstance(top, int)
        or page <= 0
        or top < 0
    ):
        raise ValueError(f"Reviewed {role} marker for question {question_number} needs positive page and non-negative top.")
    result = {"page": page, "top": top}
    for coordinate in ("left", "right", "bottom"):
        value = raw.get(coordinate)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Reviewed {role} marker {coordinate} for question {question_number} must be an integer.")
            result[coordinate] = value
    return result


def _explicit_segments(raw: Any, role: str, question_number: int) -> tuple[Mapping[str, int], ...] | None:
    """Return reviewed self-bounded segments, or ``None`` for legacy adjacent markers."""
    if not isinstance(raw, Mapping):
        return None
    if raw.get("missing") is True:
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"Reviewed missing {role} evidence for question {question_number} requires a reason."
            )
        if any(key in raw for key in ("segments", "page", "top", "bottom", "left", "right")):
            raise ValueError(
                f"Reviewed missing {role} evidence for question {question_number} cannot also define crop coordinates."
            )
        return ()
    if "segments" not in raw:
        return None
    segments = raw.get("segments")
    if not isinstance(segments, (list, tuple)) or not segments:
        raise ValueError(
            f"Reviewed {role} segments for question {question_number} must be a non-empty list."
        )
    reviewed: list[Mapping[str, int]] = []
    for index, segment in enumerate(segments):
        parsed = _marker(segment, role, question_number)
        if not all(coordinate in parsed for coordinate in ("left", "right", "bottom")):
            raise ValueError(
                f"Reviewed {role} segment {index} for question {question_number} must define left, right, and bottom."
            )
        reviewed.append(parsed)
    return tuple(reviewed)


def _role_boundaries(config: ChapterConfig, role: str) -> Mapping[str, int]:
    raw = config.layout_boundaries.get(role, config.layout_boundaries.get(f"{role}_crops", {}))
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for coordinate in ("left", "right"):
        value = raw.get(coordinate)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{role} layout boundary {coordinate} must be an integer.")
            result[coordinate] = value
    return result


def _role_range(config: ChapterConfig, role: str) -> tuple[int, int]:
    if role == "question":
        return config.question_pages
    if role == "answer_key":
        return config.answer_pages
    return config.solution_pages


def _shared_context_groups(
    config: ChapterConfig, role: str
) -> tuple[tuple[str, tuple[int, ...], tuple[Mapping[str, int], ...]], ...]:
    raw_groups = config.shared_contexts.get(role, {})
    if not isinstance(raw_groups, Mapping):
        raise ValueError(f"shared_contexts.{role} must be an object keyed by stable context id.")
    groups: list[tuple[str, tuple[int, ...], tuple[Mapping[str, int], ...]]] = []
    claimed: set[int] = set()
    configured = set(range(config.question_numbers[0], config.question_numbers[1] + 1))
    for context_id, raw in raw_groups.items():
        if not isinstance(context_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", context_id) is None:
            raise ValueError(f"Shared {role} context ids must use lowercase letters, digits, and hyphens.")
        if not isinstance(raw, Mapping):
            raise ValueError(f"Shared {role} context {context_id} must be an object.")
        numbers = raw.get("question_numbers")
        if (
            not isinstance(numbers, (list, tuple))
            or not numbers
            or any(isinstance(number, bool) or not isinstance(number, int) for number in numbers)
        ):
            raise ValueError(f"Shared {role} context {context_id} requires question_numbers.")
        selected = tuple(numbers)
        if len(set(selected)) != len(selected) or not set(selected) <= configured:
            raise ValueError(f"Shared {role} context {context_id} has duplicate or unconfigured question numbers.")
        overlap = claimed.intersection(selected)
        if overlap:
            raise ValueError(f"Questions may belong to only one shared {role} context: {sorted(overlap)}.")
        segments = _explicit_segments(raw, role, selected[0])
        if not segments:
            raise ValueError(f"Shared {role} context {context_id} requires explicit segments.")
        claimed.update(selected)
        groups.append((context_id, selected, segments))
    return tuple(groups)


def _crop_shared_contexts(
    config: ChapterConfig,
    role: str,
    pages: Mapping[int, SourceImage],
    work_dir: Path,
    selected_numbers: frozenset[int] | None = None,
) -> Mapping[int, tuple[SourceCrop, ...]]:
    start_page, last_page = _role_range(config, role)
    result: dict[int, list[SourceCrop]] = {}
    for context_id, numbers, segments in _shared_context_groups(config, role):
        for number in numbers:
            if number in config.intentional_exclusions or (
                selected_numbers is not None and number not in selected_numbers
            ):
                continue
            for segment_index, segment in enumerate(segments):
                page_number = segment["page"]
                if page_number < start_page or page_number > last_page:
                    raise ValueError(
                        f"Shared {role} context {context_id} is outside the configured page range."
                    )
                page = pages[page_number]
                box = CropBox(segment["left"], segment["top"], segment["right"], segment["bottom"])
                output_path = (
                    work_dir
                    / "crops"
                    / f"ch{config.chapter:03d}-q{number:04d}-{role}-context-{context_id}-s{segment_index:02d}-p{page_number:03d}.png"
                )
                crop = crop_region(page, box, output_path)
                result.setdefault(number, []).append(
                    replace(crop, role=role, question_number=number, context_id=context_id)
                )
    return {number: tuple(crops) for number, crops in result.items()}


def _crop_role(
    config: ChapterConfig,
    role: str,
    pages: Mapping[int, SourceImage],
    work_dir: Path,
    selected_numbers: frozenset[int] | None = None,
) -> Mapping[int, tuple[SourceCrop, ...]]:
    markers = _marker_mapping(config, role)
    numbers = list(range(config.question_numbers[0], config.question_numbers[1] + 1))
    explicit = {
        number: _explicit_segments(markers.get(str(number)), role, number)
        for number in numbers
    }
    reviewed = {
        number: _marker(markers.get(str(number)), role, number)
        for number in numbers
        if explicit[number] is None
    }
    start_page, last_page = _role_range(config, role)
    boundaries = _role_boundaries(config, role)
    result: dict[int, tuple[SourceCrop, ...]] = {}

    for index, number in enumerate(numbers):
        if selected_numbers is not None and number not in selected_numbers:
            continue
        segments = explicit[number]
        if segments is not None:
            if number in config.intentional_exclusions:
                continue
            crops: list[SourceCrop] = []
            for segment_index, segment in enumerate(segments):
                page_number = segment["page"]
                if page_number < start_page or page_number > last_page:
                    raise ValueError(
                        f"Reviewed {role} segment for question {number} is outside the configured page range."
                    )
                page = pages[page_number]
                box = CropBox(segment["left"], segment["top"], segment["right"], segment["bottom"])
                output_path = (
                    work_dir
                    / "crops"
                    / f"ch{config.chapter:03d}-q{number:04d}-{role}-s{segment_index:02d}-p{page_number:03d}.png"
                )
                crop = crop_region(page, box, output_path)
                crops.append(replace(crop, role=role, question_number=number))
            result[number] = tuple(crops)
            continue

        current = reviewed[number]
        next_number = numbers[index + 1] if index + 1 < len(numbers) else None
        if next_number is not None and explicit[next_number] is not None:
            raise ValueError(
                f"Reviewed adjacent {role} marker for question {number} cannot be bounded by an explicit segment."
            )
        next_marker = reviewed[next_number] if next_number is not None else None
        if current["page"] < start_page or current["page"] > last_page:
            raise ValueError(f"Reviewed {role} marker for question {number} is outside the configured page range.")
        if next_marker is not None and (next_marker["page"], next_marker["top"]) <= (current["page"], current["top"]):
            raise ValueError(f"Reviewed {role} markers must increase for question {number}.")

        if number in config.intentional_exclusions:
            continue
        final_page = current["page"] if "bottom" in current else (next_marker["page"] if next_marker is not None else last_page)
        if final_page > last_page:
            final_page = last_page
        crops: list[SourceCrop] = []
        for page_number in range(current["page"], final_page + 1):
            page = pages[page_number]
            with Image.open(page.path) as image:
                width, height = image.size
            left = current.get("left", boundaries.get("left", 0))
            right = current.get("right", boundaries.get("right", width))
            top = current["top"] if page_number == current["page"] else 0
            bottom = height
            if page_number == final_page and next_marker is not None and next_marker["page"] == page_number:
                bottom = min(bottom, next_marker["top"])
            if page_number == current["page"] and "bottom" in current:
                bottom = min(bottom, current["bottom"])
            if top >= bottom:
                continue
            box = CropBox(left, top, right, bottom)
            output_path = work_dir / "crops" / f"ch{config.chapter:03d}-q{number:04d}-{role}-p{page_number:03d}.png"
            crop = crop_region(page, box, output_path)
            crops.append(replace(crop, role=role, question_number=number))
        if not crops:
            raise ValueError(f"Reviewed {role} markers produce no crop for question {number}.")
        result[number] = tuple(crops)
    return result


def _configured_dpi(config: ChapterConfig) -> int:
    value = config.extras.get("source_dpi", config.extras.get("render_dpi", DEFAULT_DPI))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("source_dpi must be a positive integer.")
    return value


def canonical_source_pdf_text(path: Path | str) -> str:
    """Serialize a source PDF using the writer's one canonical lexical spelling."""
    return str(Path(os.path.abspath(path)))


def _evidence_payload(evidence: RecordEvidence) -> dict[str, Any]:
    def crop_value(crop: SourceCrop) -> dict[str, Any]:
        value = {
            "role": crop.role,
            "question_number": crop.question_number,
            "page_number": crop.page_number,
            "box": {"left": crop.box.left, "top": crop.box.top, "right": crop.box.right, "bottom": crop.box.bottom},
            "path": str(crop.path),
            "width": crop.width,
            "height": crop.height,
            "sha256": crop.sha256,
            "source_image_sha256": crop.source_image_sha256,
            "source_dpi": crop.source_dpi,
        }
        if crop.context_id:
            value["context_id"] = crop.context_id
        return value

    return {
        "chapter": evidence.chapter,
        "question_number": evidence.question_number,
        "source_pdf": canonical_source_pdf_text(evidence.source_pdf),
        "source_pdf_sha256": evidence.source_pdf_sha256,
        "question_crops": [crop_value(crop) for crop in evidence.question_crops],
        "answer_key_crops": [crop_value(crop) for crop in evidence.answer_key_crops],
        "solution_crops": [crop_value(crop) for crop in evidence.solution_crops],
        "source_status": evidence.source_status,
        "source_reasons": list(evidence.source_reasons),
        "requires_reviewed_rejection": evidence.requires_reviewed_rejection,
        "boundary_review": dict(evidence.boundary_review),
        "dependency_fingerprint": evidence.dependency_fingerprint,
    }


def _source_issue(config: ChapterConfig, number: int) -> tuple[str, tuple[str, ...], bool]:
    issues = config.extras.get("known_source_issues", {})
    if not isinstance(issues, Mapping):
        raise ValueError("known_source_issues must be an object keyed by question number.")
    raw = issues.get(str(number), issues.get(number))
    if raw is None or not isinstance(raw, Mapping) or "status" not in raw:
        return "complete", (), False
    status = raw.get("status")
    if status not in {"missing_solution", "incomplete_solution"}:
        raise ValueError(f"Unsupported source status for question {number}: {status!r}.")
    reason = raw.get("reason")
    detail = raw.get("detail")
    if not isinstance(reason, str) or not reason.strip() or not isinstance(detail, str) or not detail.strip():
        raise ValueError(f"Source issue for question {number} requires reason and detail.")
    requires_rejection = raw.get("requires_reviewed_rejection")
    if requires_rejection is not True:
        raise ValueError(f"Source issue for question {number} must require reviewed rejection.")
    return status, (f"{reason.strip()}: {detail.strip()}",), True


def _boundary_review(config: ChapterConfig, number: int) -> Mapping[str, Any]:
    reviews = config.extras.get("boundary_reviews", {})
    if not isinstance(reviews, Mapping):
        raise ValueError("boundary_reviews must be an object keyed by role and question number.")
    selected = {
        key: value
        for key, value in reviews.items()
        if isinstance(key, str) and key.rsplit(":", 1)[-1] == str(number)
    }
    return frozen_mapping(selected)


def _required_role_pages(
    config: ChapterConfig, role: str, selected_numbers: frozenset[int]
) -> set[int]:
    markers = _marker_mapping(config, role)
    numbers = list(range(config.question_numbers[0], config.question_numbers[1] + 1))
    explicit = {
        number: _explicit_segments(markers.get(str(number)), role, number)
        for number in numbers
    }
    reviewed = {
        number: _marker(markers.get(str(number)), role, number)
        for number in numbers
        if explicit[number] is None
    }
    _, last_page = _role_range(config, role)
    required: set[int] = set()
    for index, number in enumerate(numbers):
        if number not in selected_numbers or number in config.intentional_exclusions:
            continue
        segments = explicit[number]
        if segments is not None:
            required.update(segment["page"] for segment in segments)
            continue
        current = reviewed[number]
        next_number = numbers[index + 1] if index + 1 < len(numbers) else None
        next_marker = reviewed[next_number] if next_number is not None and explicit[next_number] is None else None
        final_page = current["page"] if "bottom" in current else (
            next_marker["page"] if next_marker is not None else last_page
        )
        required.update(range(current["page"], min(final_page, last_page) + 1))
    for _context_id, numbers_in_context, segments in _shared_context_groups(config, role):
        if selected_numbers.intersection(numbers_in_context):
            required.update(segment["page"] for segment in segments)
    return required


def prepare_source_evidence(
    config: ChapterConfig,
    pdf_path: Path,
    work_dir: Path,
    *,
    question_numbers: Iterable[int] | None = None,
) -> list[RecordEvidence]:
    """Render configured source pages and crop each reviewed record boundary."""
    pdf_path = Path(pdf_path)
    work_dir = Path(work_dir)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Source PDF does not exist: {pdf_path}")
    source_pdf_sha256 = sha256_path(pdf_path)
    configured_sha256 = config.extras.get("source_pdf_sha256")
    if configured_sha256 is not None:
        if (
            not isinstance(configured_sha256, str)
            or len(configured_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in configured_sha256)
        ):
            raise ValueError("source_pdf_sha256 must be a 64-character hexadecimal digest.")
        if source_pdf_sha256 != configured_sha256.lower():
            raise ValueError(
                f"Source PDF does not match configured SHA-256: expected {configured_sha256.lower()}, "
                f"got {source_pdf_sha256}."
            )
    dpi = _configured_dpi(config)
    configured_numbers = tuple(
        number for number in range(config.question_numbers[0], config.question_numbers[1] + 1)
        if number not in config.intentional_exclusions
    )
    selected = configured_numbers if question_numbers is None else tuple(question_numbers)
    if (
        any(isinstance(number, bool) or not isinstance(number, int) for number in selected)
        or len(set(selected)) != len(selected)
        or not set(selected) <= set(configured_numbers)
    ):
        raise ValueError("question_numbers must be unique configured non-excluded question numbers.")
    selected_set = frozenset(selected)
    all_pages = sorted(set().union(*(
        _required_role_pages(config, role, selected_set)
        for role in ("question", "answer_key", "solution")
    )))
    pages = {
        page_number: render_page(
            pdf_path,
            page_number,
            dpi,
            work_dir / "rendered" / f"page-{page_number:03d}-{dpi}dpi.png",
        )
        for page_number in all_pages
    }
    role_crops = {}
    for role in ("question", "answer_key", "solution"):
        primary = _crop_role(config, role, pages, work_dir, selected_set)
        contexts = _crop_shared_contexts(config, role, pages, work_dir, selected_set)
        role_crops[role] = {
            number: contexts.get(number, ()) + primary[number]
            for number in primary
        }
    prepared: list[RecordEvidence] = []
    store = ArtifactStore(work_dir)
    for number in sorted(selected_set):
        crop_provenance = tuple(
            {
                "role": crop.role,
                "page_number": crop.page_number,
                "box": [crop.box.left, crop.box.top, crop.box.right, crop.box.bottom],
                "source_image_sha256": crop.source_image_sha256,
                "source_dpi": crop.source_dpi,
                "crop_sha256": crop.sha256,
                **({"context_id": crop.context_id} if crop.context_id else {}),
            }
            for role in ("question", "answer_key", "solution")
            for crop in role_crops[role][number]
        )
        source_status, source_reasons, requires_reviewed_rejection = _source_issue(config, number)
        boundary_review = _boundary_review(config, number)
        fingerprint = dependency_fingerprint(
            {
                "chapter": config.chapter,
                "question_number": number,
                "source_pdf_sha256": source_pdf_sha256,
                "dpi": dpi,
                "crop_provenance": crop_provenance,
                "source_status": source_status,
                "source_reasons": source_reasons,
                "requires_reviewed_rejection": requires_reviewed_rejection,
                "boundary_review": boundary_review,
            }
        )
        evidence = RecordEvidence(
            chapter=config.chapter,
            question_number=number,
            source_pdf=pdf_path,
            source_pdf_sha256=source_pdf_sha256,
            question_crops=role_crops["question"][number],
            answer_key_crops=role_crops["answer_key"][number],
            solution_crops=role_crops["solution"][number],
            source_status=source_status,
            source_reasons=source_reasons,
            requires_reviewed_rejection=requires_reviewed_rejection,
            boundary_review=boundary_review,
            dependency_fingerprint=fingerprint,
        )
        store.write_json(
            "source-evidence",
            f"ch{config.chapter:03d}-q{number:04d}",
            _evidence_payload(evidence),
            {"fingerprint": fingerprint, "source_pdf_sha256": source_pdf_sha256},
        )
        prepared.append(evidence)
    return prepared
