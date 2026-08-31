"""Rebind completed V2 extraction results after a scope-only config change.

The command is intentionally conservative: it updates only the two stored job
fingerprints, and only when every source crop hash and schema binding in the
current job still matches the completed result's evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--prior-queue", type=Path, required=True)
    args = parser.parse_args()
    prior_jobs = {
        item["job_id"]: item
        for line in args.prior_queue.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for item in (json.loads(line),)
    }
    updated = 0
    for job_path in sorted(args.jobs.glob("extract-*.json")):
        job = json.loads(job_path.read_text(encoding="utf-8"))
        result_path = args.results / f"{job['job_id']}.json"
        if not result_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("job_id") != job["job_id"]:
            raise ValueError(f"Job id mismatch: {result_path}")
        prior = prior_jobs.get(job["job_id"])
        if prior is None:
            raise ValueError(f"Prior job is missing: {job['job_id']}")
        comparable_job = {key: value for key, value in job.items() if key != "job_fingerprint"}
        comparable_prior = {key: value for key, value in prior.items() if key != "job_fingerprint"}
        if comparable_job != comparable_prior:
            raise ValueError(f"Job evidence or instructions changed: {job['job_id']}")
        answer_sources = [source for source in job["sources"] if source.get("role") == "answer_key"]
        if len(answer_sources) != 1 or result.get("answer_key", {}).get("crop_sha256") != answer_sources[0]["sha256"]:
            raise ValueError(f"Answer-key evidence changed: {result_path}")
        current = job["job_fingerprint"]
        if result.get("job_fingerprint") == current:
            continue
        result["job_fingerprint"] = current
        result["answer_key"]["job_fingerprint"] = current
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        updated += 1
    print(json.dumps({"status": "rebound", "updated": updated}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
