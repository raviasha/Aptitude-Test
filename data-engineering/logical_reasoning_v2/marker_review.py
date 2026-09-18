"""Utilities for vision review of proposed source crop boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
import re
import copy
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw


FULL_WIDTH_LEFT = 95
FULL_WIDTH_RIGHT = 1435


@dataclass(frozen=True)
class ReviewedExerciseMarkers:
    """Reviewed V2 source segments for one printed exercise."""

    exercise_id: str
    internal_numbers: tuple[int, ...]
    printed_numbers: tuple[int, ...]
    marker_overrides: dict[str, dict[str, dict[str, list[dict[str, int]]]]]
    review_provenance: tuple[dict[str, Any], ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_coordinate_grid(source: Path, output: Path, *, interval: int = 50) -> str:
    """Overlay a pixel-coordinate grid without changing source dimensions."""

    if interval <= 0:
        raise ValueError("interval must be positive.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    drawing = ImageDraw.Draw(image)
    for x in range(0, image.width, interval):
        drawing.line((x, 0, x, image.height), fill=(180, 0, 0), width=1)
        drawing.text((x + 2, 2), str(x), fill=(120, 0, 0))
    for y in range(0, image.height, interval):
        drawing.line((0, y, image.width, y), fill=(0, 0, 180), width=1)
        drawing.text((2, y + 2), str(y), fill=(0, 0, 120))
    image.save(output, "PNG", optimize=False, compress_level=9)
    return _sha256(output)


def _review_blocks(value: Any, *, role: str, page: int, height: int) -> tuple[dict[str, int], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{role} blocks on page {page} must be a list.")
    result: list[dict[str, int]] = []
    seen: set[int] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{role} block on page {page} must be an object.")
        number = raw.get("printed_number")
        top = raw.get("top")
        bottom = raw.get("bottom")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in (number, top, bottom)):
            raise ValueError(f"{role} block on page {page} needs integer number and bounds.")
        if number <= 0 or top < 0 or top >= bottom or bottom > height:
            raise ValueError(f"{role} block {number} has invalid bounds on page {page}.")
        if number in seen:
            raise ValueError(f"Duplicate {role} block {number} on page {page}.")
        seen.add(number)
        continuation = raw.get("continuation", False)
        if not isinstance(continuation, bool):
            raise ValueError(f"{role} block {number} needs a boolean continuation flag.")
        result.append({"printed_number": number, "page": page, "top": top, "bottom": bottom,
                       "continuation": continuation})
    return tuple(result)


def _canonical_exercise_id(value: str) -> str:
    """Normalize only the textbook's optional ``Exercise`` prefix."""

    normalized = " ".join(value.strip().split())
    match = re.fullmatch(r"(?:exercise\s+)?(\d+[a-z0-9]*)", normalized, flags=re.IGNORECASE)
    if match is None:
        return normalized
    return f"Exercise {match.group(1).upper()}"


def _load_review(review_path: Path, grid_path: Path) -> tuple[str, int, tuple[dict[str, int], ...], tuple[dict[str, int], ...], dict[str, str | int]]:
    """Load one review and bind its coordinates to its exact grid image."""

    try:
        raw = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read page review {review_path}.") from error
    if not isinstance(raw, Mapping):
        raise ValueError(f"Page review {review_path} must be a JSON object.")
    exercise_id = raw.get("exercise_id")
    page = raw.get("page")
    if not isinstance(exercise_id, str) or not exercise_id.strip() or isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise ValueError(f"Page review {review_path} needs an exercise_id and positive page.")
    if not grid_path.is_file():
        raise ValueError(f"Missing reviewed grid image: {grid_path}")
    with Image.open(grid_path) as image:
        width, height = image.size
    if width < FULL_WIDTH_RIGHT:
        raise ValueError(f"Grid image is too narrow for full-width source markers: {grid_path}")
    questions = _review_blocks(raw.get("question_blocks"), role="question", page=page, height=height)
    answers = _review_blocks(raw.get("answer_solution_blocks"), role="answer/solution", page=page, height=height)
    notes = raw.get("reviewer_notes")
    if not isinstance(notes, str):
        raise ValueError(f"Page review {review_path} needs reviewer_notes.")
    provenance: dict[str, str | int] = {
        "page": page,
        "review_path": review_path.as_posix(),
        "review_sha256": _sha256(review_path),
        "grid_path": grid_path.as_posix(),
        "grid_sha256": _sha256(grid_path),
    }
    return _canonical_exercise_id(exercise_id), page, questions, answers, provenance


def _indexed_blocks(blocks: Sequence[dict[str, int]], *, role: str) -> dict[int, list[dict[str, int]]]:
    indexed: dict[int, list[dict[str, int]]] = {}
    for block in sorted(blocks, key=lambda item: (item["page"], item["top"])):
        number = block["printed_number"]
        if block.get("continuation", False):
            if number not in indexed or block["page"] != indexed[number][-1]["page"] + 1:
                raise ValueError(f"Orphan or non-adjacent {role} continuation {number}.")
            indexed[number].append(block)
        elif number in indexed:
            raise ValueError(f"Duplicate {role} block {number} across the reviewed exercise.")
        else:
            indexed[number] = [block]
    return indexed


def _segment(block: Mapping[str, int]) -> dict[str, int]:
    return {
        "page": block["page"],
        "left": FULL_WIDTH_LEFT,
        "top": block["top"],
        "right": FULL_WIDTH_RIGHT,
        "bottom": block["bottom"],
    }


def reviewed_exercise_markers(
    review_grid_pairs: Sequence[tuple[Path, Path]],
    *,
    internal_number_start: int = 1,
    source_order: Sequence[int] | None = None,
) -> ReviewedExerciseMarkers:
    """Turn complete page reviews for one exercise into guarded V2 markers.

    The answer-and-solution source blocks are deliberately the same book evidence
    for now: the logical-reasoning text prints each answer together with its
    explanatory solution.  A complete, number-for-number match is mandatory.
    """

    if internal_number_start <= 0:
        raise ValueError("internal_number_start must be positive.")
    if not review_grid_pairs:
        raise ValueError("At least one reviewed page is required.")
    raw_reviews = [json.loads(Path(review).read_text(encoding="utf-8")) for review, _ in review_grid_pairs]
    if source_order is not None or any(
        "regions" in block for raw in raw_reviews if isinstance(raw, Mapping)
        for role in ("question_blocks", "answer_solution_blocks")
        for block in raw.get(role, []) if isinstance(block, Mapping)
    ):
        return _explicit_file_markers(review_grid_pairs, raw_reviews,
                                      internal_number_start=internal_number_start, source_order=source_order)
    loaded = tuple(_load_review(Path(review), Path(grid)) for review, grid in review_grid_pairs)
    exercise_ids = {item[0] for item in loaded}
    if len(exercise_ids) != 1:
        raise ValueError("All reviewed pages must belong to the same exercise.")
    exercise_id = loaded[0][0]
    question_blocks = _indexed_blocks(
        tuple(block for _, _, questions, _, _ in loaded for block in questions), role="question"
    )
    answer_blocks = _indexed_blocks(
        tuple(block for _, _, _, answers, _ in loaded for block in answers), role="answer/solution"
    )
    if not question_blocks:
        raise ValueError("The reviewed exercise has no question blocks.")
    if set(question_blocks) != set(answer_blocks):
        raise ValueError("Reviewed question and answer/solution number sets do not match.")

    printed_numbers = tuple(sorted(question_blocks))
    marker_overrides = {"question": {}, "answer_key": {}, "solution": {}}
    for offset, printed_number in enumerate(printed_numbers):
        internal_number = internal_number_start + offset
        key = str(internal_number)
        marker_overrides["question"][key] = {"segments": [_segment(block) for block in question_blocks[printed_number]]}
        answer_marker = {"segments": [_segment(block) for block in answer_blocks[printed_number]]}
        marker_overrides["answer_key"][key] = answer_marker
        marker_overrides["solution"][key] = {"segments": [_segment(block) for block in answer_blocks[printed_number]]}
    return ReviewedExerciseMarkers(
        exercise_id=exercise_id,
        internal_numbers=tuple(range(internal_number_start, internal_number_start + len(printed_numbers))),
        printed_numbers=printed_numbers,
        marker_overrides=marker_overrides,
        review_provenance=tuple(item[4] for item in loaded),
    )


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _regions(raw: Mapping[str, Any], page: int, sizes: Mapping[int, tuple[int, int]]) -> list[dict[str, int]]:
    """Copy exact rectangles; the legacy band form alone supplies full-width defaults."""
    regions = raw.get("regions") if "regions" in raw else [
        dict(page=page, left=FULL_WIDTH_LEFT, right=FULL_WIDTH_RIGHT,
             top=raw.get("top"), bottom=raw.get("bottom"))]
    if not isinstance(regions, list) or not regions:
        raise ValueError("Regions must be a nonempty list.")
    result = []
    for region in regions:
        keys = ("page", "left", "top", "right", "bottom")
        if isinstance(region, Mapping):
            region = {"page": page, **region}
        if not isinstance(region, Mapping) or any(not _integer(region.get(k)) for k in keys):
            raise ValueError("Regions require exact integer page,left,top,right,bottom.")
        segment = {key: region[key] for key in keys}
        if segment["page"] not in sizes:
            raise ValueError("Region references an unknown source page.")
        width, height = sizes[segment["page"]]
        if not (0 <= segment["left"] < segment["right"] <= width and
                0 <= segment["top"] < segment["bottom"] <= height):
            raise ValueError("Invalid region bounds.")
        result.append(segment)
    return result


def _validate_segments(segments: Sequence[Mapping[str, int]], order: Mapping[int, int] | None) -> None:
    for index, current in enumerate(segments):
        for previous in segments[:index]:
            if (current["page"] == previous["page"] and
                max(current["left"], previous["left"]) < min(current["right"], previous["right"]) and
                max(current["top"], previous["top"]) < min(current["bottom"], previous["bottom"])):
                raise ValueError("Overlapping same-record regions.")
        if index:
            prior_page, page = segments[index - 1]["page"], current["page"]
            if order is None:
                valid = page == prior_page or page == prior_page + 1
            else:
                valid = order[page] >= order[prior_page]
            if not valid:
                raise ValueError("Non-adjacent/nonphysical regions require explicit source_order.")


def _compile_fragments(fragments, *, order, role):
    """Join only explicitly marked continuations; never extrapolate a crop."""
    def position(fragment):
        return order[fragment[1]]

    indexed = {}
    ordered = sorted(fragments, key=position) if order is not None else fragments
    for raw, page, segments in ordered:
        number = raw["printed_number"]
        continuation = raw.get("continuation", False)
        if not isinstance(continuation, bool):
            raise ValueError("Invalid continuation flag.")
        if continuation:
            if number not in indexed:
                raise ValueError(f"Orphan {role} continuation {number}.")
            indexed[number].extend(segments)
        elif number in indexed:
            raise ValueError(f"Duplicate {role} start {number}.")
        else:
            indexed[number] = list(segments)
        _validate_segments(indexed[number], order)
    return indexed


def _reviewed_blocker_clearance(result, clearance, page_sizes, source_images):
    """Validate a separate, result-bound review attestation, never infer it from prose."""
    checks = {"geometry", "coverage", "exercise_identity", "content_completeness", "other_notes"}
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if (not isinstance(clearance, Mapping) or clearance.get("result_sha256") != digest or
        any(not isinstance(clearance.get(k), str) or not clearance[k].strip() for k in ("reviewer", "reason")) or
        not isinstance(clearance.get("cleared_checks"), list) or
        any(not isinstance(k, str) for k in clearance["cleared_checks"]) or
        set(clearance["cleared_checks"]) != checks or len(clearance["cleared_checks"]) != len(checks) or
        clearance.get("other_blockers") != []):
        raise ValueError("Blocked result requires a complete, result-bound cleared_blockers review.")
    remaining = clearance.get("remaining_nulls")
    if not isinstance(remaining, list) or not remaining:
        raise ValueError("Clearance requires an exact remaining-null inventory.")
    declared = set()
    for item in remaining:
        if (not isinstance(item, Mapping) or not _integer(item.get("page"), minimum=1) or
            not isinstance(item.get("exercise_id"), str) or
            not _integer(item.get("block_index")) or
            (item.get("role"), item.get("field")) not in (
                ("question_blocks", "printed_number"), ("answer_solution_blocks", "printed_number"),
                ("shared_context_blocks", "target_printed_number_start"),
                ("shared_context_blocks", "target_printed_number_end"))):
            raise ValueError("Unsupported remaining blocker; only null numbers and shared targets are adjudicable.")
        key = (item["page"], _canonical_exercise_id(item["exercise_id"]), item["role"], item["block_index"], item["field"])
        if key in declared:
            raise ValueError("Duplicate remaining-null declaration.")
        declared.add(key)
    actual, referenced = set(), set()
    pages = result.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing clearance source pages.")
    for page in pages:
        if not isinstance(page, Mapping) or not _integer(page.get("page"), minimum=1):
            raise ValueError("Invalid clearance page.")
        referenced.add(page["page"])
        if not isinstance(page.get("exercises"), list):
            raise ValueError("Missing exercise evidence.")
        for exercise in page["exercises"]:
            if not isinstance(exercise, Mapping) or not isinstance(exercise.get("exercise_id"), str):
                raise ValueError("Invalid exercise evidence.")
            for role in ("question_blocks", "answer_solution_blocks", "shared_context_blocks"):
                if not isinstance(exercise.get(role), list):
                    raise ValueError("Missing block evidence.")
                for index, block in enumerate(exercise[role]):
                    if not isinstance(block, Mapping) or "printed_number" not in block:
                        raise ValueError("Missing block number evidence.")
                    if role != "shared_context_blocks" and block["printed_number"] is None:
                        actual.add((page["page"], _canonical_exercise_id(exercise["exercise_id"]), role, index, "printed_number"))
                    if role == "shared_context_blocks":
                        for field in ("target_printed_number_start", "target_printed_number_end"):
                            if field not in block:
                                raise ValueError("Missing shared target field is not an adjudicable null.")
                            if block[field] is None:
                                actual.add((page["page"], _canonical_exercise_id(exercise["exercise_id"]), role, index, field))
                    referenced.update(s["page"] for s in _regions(block, page["page"], page_sizes))
    if actual != declared:
        raise ValueError("Remaining-null inventory does not match the blocked result.")
    evidence = clearance.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != referenced:
        raise ValueError("Clearance must bind every source page to pixel evidence.")
    for page, binding in evidence.items():
        if (page not in page_sizes or not isinstance(binding, Mapping) or
            not isinstance(binding.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None or
            not _integer(binding.get("width"), minimum=1) or not _integer(binding.get("height"), minimum=1) or
            (binding["width"], binding["height"]) != tuple(page_sizes[page]) or
            not isinstance(binding.get("review_reference"), str) or not binding["review_reference"].strip()):
            raise ValueError("Invalid or incomplete source pixel review evidence.")
        if source_images is not None:
            if page not in source_images:
                raise ValueError("Missing bound source image.")
            try:
                path = Path(source_images[page])
                if _sha256(path) != binding["sha256"]:
                    raise ValueError("Source image hash differs from reviewed pixel evidence.")
                with Image.open(path) as image:
                    if image.size != tuple(page_sizes[page]):
                        raise ValueError("Source pixel dimensions differ from region coordinate space.")
            except (OSError, TypeError) as error:
                raise ValueError("Cannot validate bound source pixels.") from error
    return copy.deepcopy(dict(clearance))


def _merge_context_orders(lists, evidence_order, order):
    """Stable topological union of explicit list constraints and page order."""
    keys = ("page", "left", "top", "right", "bottom")
    sequences = [tuple(tuple(s[k] for k in keys) for s in segments) for segments in lists]
    nodes = set(node for sequence in sequences for node in sequence)
    edges = {node: set() for node in nodes}
    for sequence in sequences:
        for before, after in zip(sequence, sequence[1:]):
            edges[before].add(after)
    for before in nodes:
        for after in nodes:
            if (order[before[0]] if order is not None else before[0]) < (order[after[0]] if order is not None else after[0]):
                edges[before].add(after)
    indegree = dict.fromkeys(nodes, 0)
    for targets in edges.values():
        for node in targets:
            indegree[node] += 1
    result = []
    while indegree:
        ready = [node for node, degree in indegree.items() if degree == 0]
        if not ready:
            raise ValueError("Shared context segment order contains a cycle.")
        node = min(ready, key=evidence_order.__getitem__)
        result.append(dict(zip(keys, node)))
        del indegree[node]
        for target in edges[node]:
            indegree[target] -= 1
    _validate_segments(result, order)
    return result


def reviewed_recovery_markers(
    results: Sequence[Mapping[str, Any]], *, exercise_id: str,
    page_sizes: Mapping[int, tuple[int, int]], internal_number_start: int = 1,
    source_order: Sequence[int] | None = None,
    expected_printed_numbers: Sequence[int] | None = None,
    adjudications: Mapping[tuple[int, str, str, int], Mapping[str, Any]] | None = None,
    context_adjudications: Mapping[tuple[int, str, int], Mapping[str, Any]] | None = None,
    cleared_blockers: Mapping[str, Mapping[str, Any]] | None = None,
    source_images: Mapping[int, Path] | None = None,
) -> tuple[ReviewedExerciseMarkers, dict[str, Any]]:
    """Compile approved recovery-page result objects for exactly one exercise.

    Regions are pixel rectangles in caller-supplied source image dimensions.
    Their list order is retained; fragments without source_order are consumed in
    input order (unlike the legacy file API). Page-local regions inherit only the
    containing page number. ``source_order`` is an explicit sequence of
    PDF page numbers authorizing nonphysical/gapped reading order; it must cover
    all selected pages/regions. No geometry or continuity is inferred from it.
    Legacy top/bottom blocks remain supported. Shared blocks use visible inclusive
    target_printed_number_start/end, default to question role, and may explicitly
    select role=solution or answer_key. Repeated targets require continuation=True.
    Overlapping target ranges are merged per question, then partitioned into V2
    groups with identical ordered evidence. Exact duplicate rectangles appear once.
    Input objects are result objects, not runner status envelopes. Blocked results
    may be used only with reviewed null-number and/or shared-target adjudications.
    Number decisions are keyed by (page, exercise_id, role, zero-based block_index).
    Reviewer and reason are
    mandatory; visible numbers are immutable. All other validation still applies.
    Blocked recovery additionally requires cleared_blockers[job_fingerprint]:
    result_sha256 (canonical JSON: sorted keys, compact separators, UTF-8,
    ensure_ascii=False), reviewer, reason, cleared_checks (geometry, coverage,
    exercise_identity, content_completeness, other_notes), other_blockers=[],
    remaining_nulls (exact page/exercise_id/role/block_index/field inventory), and
    source_evidence keyed by integer page with sha256,width,height,review_reference.
    The separate reviewer must clear every free-text note blocker against those
    pixels; a decision cannot supply this clearance implicitly. Shared targets may
    be adjudicated via context_adjudications[(page, exercise_id, shared_block_index)]
    with target_printed_number_start/end, role (question/answer_key/solution),
    reviewer and reason. Both original endpoints must be explicitly null in a
    blocked result; the positive ordered range must be wholly in the required
    expected_printed_numbers roster. The clearance inventory must declare each
    endpoint separately with role=shared_context_blocks and its exact field name.
    Context decisions are retained separately in provenance with original nulls.
    When source_images is supplied every
    bound image is hash/dimension checked; otherwise provenance explicitly records
    external pixel review, not an automated content/geometry inspection. Rectangle
    bounds are always checked; semantic crop completeness is the reviewer's claim.
    Supply expected_printed_numbers to detect records missing from both roles;
    otherwise completeness is limited to coverage/count declarations and matching.
    """
    if not _integer(internal_number_start, minimum=1) or not results:
        raise ValueError("Positive internal start and nonempty recovery results required.")
    if not isinstance(exercise_id, str) or re.fullmatch(r"(?:Exercise\s+)?\d+[A-Za-z0-9]*", exercise_id, re.I) is None:
        raise ValueError("Unknown exercise identity.")
    exercise_id = _canonical_exercise_id(exercise_id)
    if cleared_blockers is not None and not isinstance(cleared_blockers, Mapping):
        raise ValueError("cleared_blockers must be a mapping.")
    if source_images is not None and not isinstance(source_images, Mapping):
        raise ValueError("source_images must be a mapping.")
    used_clearances = set()
    if adjudications is not None and not isinstance(adjudications, Mapping):
        raise ValueError("Adjudications must be a mapping.")
    decisions = {}
    for key, decision in (adjudications or {}).items():
        if (not isinstance(key, tuple) or len(key) != 4 or not _integer(key[0], minimum=1) or
            not isinstance(key[1], str) or _canonical_exercise_id(key[1]) != exercise_id or
            key[2] not in ("question_blocks", "answer_solution_blocks") or not _integer(key[3]) or
            not isinstance(decision, Mapping) or not _integer(decision.get("printed_number"), minimum=1) or
            any(not isinstance(decision.get(k), str) or not decision[k].strip() for k in ("reviewer", "reason"))):
            raise ValueError("Invalid or out-of-scope number adjudication.")
        canonical_key = (key[0], exercise_id, key[2], key[3])
        if canonical_key in decisions:
            raise ValueError("Ambiguous duplicate number adjudication.")
        decisions[canonical_key] = dict(decision)
    used_decisions = set()
    if context_adjudications is not None and not isinstance(context_adjudications, Mapping):
        raise ValueError("context_adjudications must be a mapping.")
    context_decisions = {}
    if context_adjudications:
        if (not isinstance(expected_printed_numbers, (list, tuple)) or not expected_printed_numbers or
            any(not _integer(n, minimum=1) for n in expected_printed_numbers) or
            len(set(expected_printed_numbers)) != len(expected_printed_numbers)):
            raise ValueError("Context adjudications require an explicit valid expected_printed_numbers roster.")
        roster = set(expected_printed_numbers)
        for key, decision in context_adjudications.items():
            if (not isinstance(key, tuple) or len(key) != 3 or not _integer(key[0], minimum=1) or
                not isinstance(key[1], str) or _canonical_exercise_id(key[1]) != exercise_id or
                not _integer(key[2]) or not isinstance(decision, Mapping) or
                any(not isinstance(decision.get(k), str) or not decision[k].strip() for k in ("reviewer", "reason")) or
                decision.get("role") not in ("question", "answer_key", "solution")):
                raise ValueError("Invalid or out-of-scope context adjudication.")
            start, end = decision.get("target_printed_number_start"), decision.get("target_printed_number_end")
            if (not _integer(start, minimum=1) or not _integer(end, minimum=1) or end < start or
                end - start + 1 > len(roster) or any(n not in roster for n in range(start, end + 1))):
                raise ValueError("Context adjudication range must be ordered and wholly within the expected roster.")
            canonical_key = (key[0], exercise_id, key[2])
            if canonical_key in context_decisions:
                raise ValueError("Ambiguous duplicate context adjudication.")
            context_decisions[canonical_key] = dict(decision)
    used_context_decisions = set()
    for page, size in page_sizes.items():
        if not _integer(page, minimum=1) or not isinstance(size, (list, tuple)) or len(size) != 2 or any(not _integer(v, minimum=1) for v in size):
            raise ValueError("Invalid source page dimensions.")
    order = None
    if source_order is not None:
        if (not isinstance(source_order, (list, tuple)) or not source_order or
            any(not _integer(p, minimum=1) or p not in page_sizes for p in source_order) or
            len(set(source_order)) != len(source_order)):
            raise ValueError("Invalid source_order.")
        order = {page: index for index, page in enumerate(source_order)}
    roles = ("question_blocks", "answer_solution_blocks", "shared_context_blocks")
    fragments = {role: [] for role in roles}
    provenance = []
    seen_pages = set()
    for result in results:
        if not isinstance(result, Mapping) or result.get("verdict") not in ("approved", "blocked"):
            raise ValueError("Recovery result is blocked or not approved.")
        blocked = result["verdict"] == "blocked"
        if blocked and (not (decisions or context_decisions) or not isinstance(result.get("notes"), str) or not result["notes"].strip()):
            raise ValueError("Blocked results require reviewed adjudications and notes.")
        result_adjudications = 0
        if any(not isinstance(result.get(k), str) or not result[k] for k in ("job_id", "job_fingerprint")):
            raise ValueError("Incomplete recovery provenance.")
        clearance = None
        if blocked:
            fingerprint = result["job_fingerprint"]
            if fingerprint in used_clearances:
                raise ValueError("Ambiguous reused blocker clearance.")
            clearance = _reviewed_blocker_clearance(result, (cleared_blockers or {}).get(fingerprint), page_sizes, source_images)
            used_clearances.add(fingerprint)
        pages = result.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError("Incomplete recovery pages.")
        for page_result in pages:
            page_adjudications = []
            page_context_adjudications = []
            if not isinstance(page_result, Mapping) or page_result.get("coverage_complete") is not True:
                raise ValueError("Incomplete page coverage.")
            page = page_result.get("page")
            if not _integer(page, minimum=1) or page not in page_sizes or page in seen_pages:
                raise ValueError("Unknown or duplicate recovery page.")
            seen_pages.add(page)
            exercises, counts = page_result.get("exercises"), page_result.get("observed_counts")
            if not isinstance(exercises, list) or not isinstance(counts, Mapping):
                raise ValueError("Incomplete page exercise/count metadata.")
            actual = dict.fromkeys(roles, 0)
            identities = set()
            for exercise in exercises:
                label = exercise.get("exercise_id") if isinstance(exercise, Mapping) else None
                if not isinstance(label, str) or re.fullmatch(r"(?:Exercise\s+)?\d+[A-Za-z0-9]*", label, re.I) is None:
                    raise ValueError("Unknown exercise identity.")
                identity = _canonical_exercise_id(label)
                if identity in identities:
                    raise ValueError("Ambiguous duplicate exercise on page.")
                identities.add(identity)
                for role in roles:
                    blocks = exercise.get(role)
                    if not isinstance(blocks, list):
                        raise ValueError("Incomplete exercise blocks.")
                    actual[role] += len(blocks)
                    if identity != exercise_id and not blocked:
                        continue
                    for block_index, block in enumerate(blocks):
                        if not isinstance(block, Mapping):
                            raise ValueError("Invalid recovery block.")
                        decision_key = (page, identity, role, block_index)
                        if decision_key in decisions:
                            if not blocked or "printed_number" not in block or block["printed_number"] is not None:
                                raise ValueError("Adjudication may only replace a null number in a blocked result.")
                            decision = decisions[decision_key]
                            block = dict(block, printed_number=decision["printed_number"])
                            used_decisions.add(decision_key)
                            result_adjudications += 1
                            page_adjudications.append(dict(page=page, exercise_id=identity, role=role,
                                block_index=block_index, original_printed_number=None,
                                printed_number=decision["printed_number"], reviewer=decision["reviewer"], reason=decision["reason"]))
                        context_key = (page, identity, block_index)
                        if role == "shared_context_blocks" and context_key in context_decisions:
                            fields = ("target_printed_number_start", "target_printed_number_end")
                            if not blocked or any(field not in block or block[field] is not None for field in fields):
                                raise ValueError("Context adjudication may only replace both null endpoints in a blocked result.")
                            decision = context_decisions[context_key]
                            block = dict(block, **{field: decision[field] for field in fields}, role=decision["role"])
                            used_context_decisions.add(context_key)
                            result_adjudications += 1
                            page_context_adjudications.append(dict(page=page, exercise_id=identity,
                                block_index=block_index, original_target_printed_number_start=None,
                                original_target_printed_number_end=None, **{field: decision[field] for field in
                                    (*fields, "role", "reviewer", "reason")}))
                        if (not isinstance(block.get("continuation"), bool) or
                            not isinstance(block.get("start_question"), bool) or
                            block["start_question"] != (role == "question_blocks" and not block["continuation"])):
                            raise ValueError("Incomplete or conflicting start/continuation flags.")
                        if role != "shared_context_blocks" and not _integer(block.get("printed_number"), minimum=1):
                            raise ValueError("Unknown printed number.")
                        segments = _regions(block, page, page_sizes)
                        if blocked and role == "shared_context_blocks":
                            start, end = block.get("target_printed_number_start"), block.get("target_printed_number_end")
                            if not _integer(start, minimum=1) or not _integer(end, minimum=1) or end < start:
                                raise ValueError("Unresolved shared-context targets in blocked result.")
                        if identity != exercise_id:
                            continue
                        if order is not None and any(p not in order for p in [page] + [s["page"] for s in segments]):
                            raise ValueError("Incomplete source_order.")
                        fragments[role].append((block, page, segments))
            if any(not _integer(counts.get(role)) or counts[role] != actual[role] for role in roles):
                raise ValueError("Incomplete or inconsistent observed counts.")
            entry = dict(page=page, job_id=result["job_id"], job_fingerprint=result["job_fingerprint"])
            if blocked:
                entry.update(original_verdict="blocked", notes=result["notes"], adjudications=page_adjudications)
                if page_context_adjudications:
                    entry["context_adjudications"] = page_context_adjudications
                entry.update(blocker_clearance=copy.deepcopy(clearance), geometry_evidence_mode=(
                    "bound_source_images" if source_images is not None else "external_pixel_review"))
            provenance.append(entry)
        if blocked and not result_adjudications:
            raise ValueError("Blocked result has no adjudicated null numbers or shared targets.")
    if used_decisions != decisions.keys():
        raise ValueError("Unused or out-of-scope number adjudication.")
    if used_context_decisions != context_decisions.keys():
        raise ValueError("Unused or out-of-scope context adjudication.")
    if used_clearances != (cleared_blockers or {}).keys():
        raise ValueError("Unused or out-of-scope blocker clearance.")
    questions = _compile_fragments(fragments[roles[0]], order=order, role="question")
    answers = _compile_fragments(fragments[roles[1]], order=order, role="answer/solution")
    if not questions or questions.keys() != answers.keys():
        raise ValueError("Incomplete question and answer/solution sets: do not match.")
    printed = tuple(sorted(questions))
    if expected_printed_numbers is not None:
        if (not isinstance(expected_printed_numbers, (list, tuple)) or not expected_printed_numbers or
            any(not _integer(n, minimum=1) for n in expected_printed_numbers) or
            len(set(expected_printed_numbers)) != len(expected_printed_numbers) or
            set(expected_printed_numbers) != set(printed)):
            raise ValueError("Incomplete exercise: expected printed roster does not match.")
    internal = tuple(range(internal_number_start, internal_number_start + len(printed)))
    number_map = dict(zip(printed, internal))
    overrides = {role: {} for role in ("question", "answer_key", "solution")}
    for number in printed:
        for role, source in (("question", questions), ("answer_key", answers), ("solution", answers)):
            overrides[role][str(number_map[number])] = {"segments": [dict(s) for s in source[number]]}
    contexts = {}
    context_fragments = {}
    for raw, page, segments in fragments[roles[2]]:
        start, end = raw.get("target_printed_number_start"), raw.get("target_printed_number_end")
        role = raw.get("role", "question")
        if (not _integer(start, minimum=1) or not _integer(end, minimum=1) or end < start or
            role not in overrides or any(n not in number_map for n in range(start, end + 1))):
            raise ValueError("Unknown/incomplete shared context target range or role.")
        key = (role, start, end)
        # Shared context is not a question start, but still has explicit continuation semantics.
        normalized = dict(raw, printed_number=1)
        normalized.pop("start_question", None)
        context_fragments.setdefault(key, []).append((normalized, page, segments))
    applicable = {role: {} for role in overrides}
    for (role, start, end), parts in context_fragments.items():
        segments = _compile_fragments(parts, order=order, role="shared context")[1]
        for number in range(start, end + 1):
            applicable[role].setdefault(number, []).append(segments)
    segment_keys = ("page", "left", "top", "right", "bottom")
    evidence_order = {}
    for _, _, segments in fragments[roles[2]]:
        for segment in segments:
            key = tuple(segment[k] for k in segment_keys)
            evidence_order.setdefault(key, len(evidence_order))
    for role, per_question in applicable.items():
        groups = {}
        for number in sorted(per_question):
            merged = _merge_context_orders(per_question[number], evidence_order, order)
            signature = tuple(tuple(segment[k] for k in segment_keys) for segment in merged)
            groups.setdefault(signature, []).append(number)
        for signature, numbers in groups.items():
            context_id = f"{exercise_id.lower().replace(' ', '-')}-{numbers[0]}-{numbers[-1]}"
            contexts.setdefault(role, {})[context_id] = dict(
                question_numbers=[number_map[n] for n in numbers],
                segments=[dict(zip(segment_keys, values)) for values in signature])
    return ReviewedExerciseMarkers(exercise_id, internal, printed, overrides, tuple(provenance)), contexts


def _explicit_file_markers(pairs, raw_reviews, *, internal_number_start, source_order):
    sizes, results, provenance, identities = {}, [], [], set()
    for (review_path, grid_path), raw in zip(pairs, raw_reviews):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("reviewer_notes"), str):
            raise ValueError("Invalid page review metadata.")
        page = raw.get("page")
        if not _integer(page, minimum=1):
            raise ValueError("Invalid page number.")
        with Image.open(grid_path) as image:
            sizes[page] = image.size
        identity = raw.get("exercise_id")
        if not isinstance(identity, str):
            raise ValueError("Unknown exercise identity.")
        identities.add(_canonical_exercise_id(identity))
        exercise = {"exercise_id": identity, "question_blocks": raw.get("question_blocks"),
                    "answer_solution_blocks": raw.get("answer_solution_blocks"), "shared_context_blocks": []}
        if any(not isinstance(exercise[role], list) for role in ("question_blocks", "answer_solution_blocks")):
            raise ValueError("Incomplete review blocks.")
        for role in ("question_blocks", "answer_solution_blocks"):
            normalized = []
            for block in exercise[role]:
                if not isinstance(block, Mapping):
                    raise ValueError("Invalid review block.")
                copy = dict(block)
                copy.setdefault("continuation", False)
                copy.setdefault("start_question", role == "question_blocks" and not copy["continuation"])
                normalized.append(copy)
            exercise[role] = normalized
        provenance.append(dict(page=page, review_path=Path(review_path).as_posix(), review_sha256=_sha256(Path(review_path)),
                               grid_path=Path(grid_path).as_posix(), grid_sha256=_sha256(Path(grid_path))))
        results.append(dict(verdict="approved", job_id=str(review_path), job_fingerprint=provenance[-1]["review_sha256"],
            pages=[dict(page=page, coverage_complete=True, exercises=[exercise],
                        observed_counts={role: len(exercise[role]) for role in ("question_blocks", "answer_solution_blocks", "shared_context_blocks")})]))
    if len(identities) != 1:
        raise ValueError("All reviewed pages must belong to the same exercise.")
    markers, _ = reviewed_recovery_markers(results, exercise_id=next(iter(identities)), page_sizes=sizes,
                                         internal_number_start=internal_number_start, source_order=source_order)
    return replace(markers, review_provenance=tuple(provenance))
