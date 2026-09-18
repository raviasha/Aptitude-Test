"""Compile vision-reviewed logical-reasoning page boundaries into a manifest.

This deliberately creates an exercise-level artifact, not a V2 chapter config.
The chapter config can only be written once every exercise has a matched set of
question and answer/solution source boundaries.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ENGINEERING_ROOT = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from logical_reasoning_v2.marker_review import reviewed_exercise_markers  # noqa: E402


def _pair(value: str) -> tuple[Path, Path]:
    review, separator, grid = value.partition("=")
    if not separator or not review or not grid:
        raise argparse.ArgumentTypeError("Each --pair must use REVIEW_JSON=GRID_PNG.")
    return Path(review), Path(grid)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chapter-id", type=int, required=True)
    parser.add_argument("--internal-number-start", type=int, default=1)
    parser.add_argument("--pair", type=_pair, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    markers = reviewed_exercise_markers(
        args.pair,
        internal_number_start=args.internal_number_start,
    )
    manifest = {
        "schema_version": 1,
        "artifact_type": "logical-reasoning-reviewed-exercise-markers",
        "chapter_id": args.chapter_id,
        "exercise_id": markers.exercise_id,
        "internal_numbers": list(markers.internal_numbers),
        "printed_numbers": list(markers.printed_numbers),
        "marker_overrides": markers.marker_overrides,
        "review_provenance": list(markers.review_provenance),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "compiled", "exercise_id": markers.exercise_id, "records": len(markers.internal_numbers)}))


if __name__ == "__main__":
    main()
