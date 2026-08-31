"""Remove explicitly excluded question numbers from a V2 audit ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    excluded = set(config.get("intentional_exclusions", []))
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    before = len(ledger["records"])
    ledger["records"] = [
        record for record in ledger["records"]
        if int(record["question_number"]) not in excluded
    ]
    removed = before - len(ledger["records"])
    args.ledger.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "pruned", "removed": removed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
