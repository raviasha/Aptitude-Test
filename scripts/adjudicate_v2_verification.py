"""Apply explicit human/agent adjudications to V2 verification results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--questions", type=int, nargs="+", required=True)
    parser.add_argument("--field", choices=("clipping", "answer_mapping"), required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    root = Path("tmp/textbook-v2") / f"chapter-{args.chapter:03d}"
    adjudications = root / "adjudications"
    adjudications.mkdir(parents=True, exist_ok=True)
    for question in args.questions:
        result_path = root / "verification-results" / f"verify-ch{args.chapter:02d}-q{question:04d}.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if payload["verdicts"].get(args.field) not in {"fail", "pass"}:
            raise ValueError(f"Expected adjudicable {args.field} verdict: {result_path}")
        if args.field == "clipping":
            render_root = root / "renders" / f"q{question:04d}"
            for viewport in ("1024x768", "1600x900"):
                if not (render_root / f"{viewport}-unanswered-card.png").is_file():
                    raise FileNotFoundError(f"Missing full-card render for q{question}: {viewport}")
        payload["verdicts"][args.field] = "pass"
        differences = payload.get("differences")
        if not isinstance(differences, dict):
            differences = {}
        differences.pop(args.field, None)
        payload["differences"] = differences
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record = {
            "chapter": args.chapter,
            "question": question,
            "field": args.field,
            "decision": "pass",
            "reason": args.reason,
            "verification_result": str(result_path),
        }
        (adjudications / f"q{question:04d}-{args.field}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"status": "adjudicated", "count": len(args.questions), "field": args.field}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
