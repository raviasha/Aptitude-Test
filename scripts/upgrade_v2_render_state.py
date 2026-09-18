"""Safely bind legacy V2 render records to candidates and the pixel-only contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ENGINEERING_ROOT = PROJECT_ROOT / "data-engineering"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from textbook_chapters_v2.cli import (  # noqa: E402
    _application_renderer_manifest,
    _atomic_json,
    _candidate_path,
    _candidates,
    _chapter_root,
    _render_dependency_fingerprint,
    _render_files_are_current,
    _render_from_payload,
    _render_state,
)
from textbook_chapters_v2.config import ChapterConfig  # noqa: E402
from textbook_chapters_v2.models import PipelineBlocked  # noqa: E402


def runtime_contract_is_compatible(old_manifest: dict, new_manifest: dict) -> bool:
    old_policy = dict(old_manifest.get("runtime_policy", {}))
    new_policy = dict(new_manifest.get("runtime_policy", {}))
    # This migration deliberately narrows the hashed source-file set and bumps
    # only the render-contract version. Every actual runtime/rendering input
    # must remain identical.
    old_policy.pop("render_contract_version", None)
    new_policy.pop("render_contract_version", None)
    return (
        old_policy == new_policy
        and old_manifest.get("runtime_evidence") == new_manifest.get("runtime_evidence")
    )


def _require_render_coverage(config: ChapterConfig, candidate, rendered) -> None:
    viewports = {f"{width}x{height}" for width, height in config.extras.get("validation_viewports", ())}
    if not viewports:
        raise PipelineBlocked("Render-state migration requires configured validation viewports.")
    if set(rendered.question_screenshots) != viewports or set(rendered.solution_screenshots) != viewports:
        raise PipelineBlocked(f"Question {candidate.question_number} lacks full-card viewport coverage.")
    fields = set(rendered.field_screenshots)
    required = {
        *(f"unanswered.{viewport}.question" for viewport in viewports),
        *(f"submitted.{viewport}.solution" for viewport in viewports),
        *(
            f"unanswered.{viewport}.option-{label}"
            for viewport in viewports
            for label in candidate.options
        ),
    }
    if not required <= fields:
        raise PipelineBlocked(f"Question {candidate.question_number} lacks required field screenshot coverage.")


def upgrade(config_path: Path) -> dict[str, object]:
    config = ChapterConfig.load(config_path)
    candidates = _candidates(config)
    candidates_by_number = {candidate.question_number: candidate for candidate in candidates}
    state = dict(_render_state(config))
    records = [
        {"question_number": int(item["question_number"]), "artifacts": dict(item["artifacts"])}
        for item in state["records"]
    ]
    if {item["question_number"] for item in records} != set(candidates_by_number):
        raise PipelineBlocked("Render-state and candidate question sets differ; migration refused.")
    old_manifest = state.get("application_renderer_manifest")
    if not isinstance(old_manifest, dict):
        raise PipelineBlocked("Legacy render state has no manifest; migration refused.")
    if state.get("dependency_fingerprint") != _render_dependency_fingerprint(candidates, old_manifest, records):
        raise PipelineBlocked("Legacy render state is not cryptographically bound to current candidates.")
    for item in records:
        candidate = candidates_by_number[item["question_number"]]
        rendered = _render_from_payload(item["artifacts"])
        _require_render_coverage(config, candidate, rendered)
        if not _render_files_are_current(rendered):
            raise PipelineBlocked(f"Render files for question {item['question_number']} are missing or stale.")
    browser_identities = tuple(str(item["artifacts"].get("browser_runtime", "")) for item in records)
    new_manifest = _application_renderer_manifest(config, browser_identities)
    if old_manifest.get("application_fingerprint") != new_manifest.get("application_fingerprint"):
        raise PipelineBlocked("Application pixels changed; migration requires a real rerender.")
    if not runtime_contract_is_compatible(old_manifest, new_manifest):
        raise PipelineBlocked("Browser/runtime/viewports changed; migration requires a real rerender.")
    old_contract = {
        item.get("path"): item.get("sha256")
        for item in old_manifest.get("renderer_contract", [])
        if isinstance(item, dict)
    }
    for item in new_manifest["renderer_contract"]:
        if old_contract.get(item["path"]) != item["sha256"]:
            raise PipelineBlocked(f"Pixel renderer changed at {item['path']}; migration refused.")
    manifest_fingerprint = str(new_manifest["manifest_fingerprint"])
    for item in records:
        candidate = candidates_by_number[item["question_number"]]
        item["artifacts"]["candidate_sha256"] = candidate.sha256
        item["artifacts"]["renderer_version"] = manifest_fingerprint
    upgraded = {
        "application_renderer_manifest": new_manifest,
        "dependency_fingerprint": _render_dependency_fingerprint(candidates, new_manifest, records),
        "records": records,
    }
    destination = _chapter_root(config) / "state" / "renders.json"
    _atomic_json(destination, upgraded)
    return {
        "status": "upgraded",
        "chapter": config.chapter,
        "records": len(records),
        "path": str(destination),
        "candidate_path": str(_candidate_path(config)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(upgrade(args.config.resolve())))


if __name__ == "__main__":
    main()
