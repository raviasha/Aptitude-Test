"""Remove optional null difference fields from V2 verification JSON files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    updated = 0
    for path in sorted(args.results.glob("verify-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        differences = payload.get("differences")
        if not isinstance(differences, dict):
            continue
        normalized = {key: value for key, value in differences.items() if value is not None}
        if normalized == differences:
            continue
        payload["differences"] = normalized
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        updated += 1
    print(json.dumps({"status": "normalized", "updated": updated}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
