"""Run application-render verification jobs through isolated Codex vision workers."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = PROJECT_ROOT / "data-engineering" / "textbook_chapters_v2" / "schemas" / "codex-verification-result.schema.json"
_PRINT_LOCK = threading.Lock()


def normalized_pipeline_result(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    if result.get("verdicts", {}).get("options.E") is None:
        result.get("verdicts", {}).pop("options.E", None)
    result["differences"] = {
        field: detail for field, detail in result.get("differences", {}).items() if detail is not None
    }
    return result


def _candidate(job: dict[str, Any]) -> dict[str, Any]:
    return next(source["record"] for source in job["sources"] if source["kind"] == "candidate_record")


def _images(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [source for source in job["sources"] if source.get("path")]


def _prompt(job: dict[str, Any]) -> str:
    candidate = _candidate(job)
    image_descriptions = ", ".join(
        f"{index + 1}:{source['kind']}:{source.get('role', source.get('field', ''))}:{source.get('viewport', '')}"
        for index, source in enumerate(_images(job))
    )
    return (
        f"{job['prompt'].strip()} "
        "Operational attachment, record, and output-envelope details follow. "
        f"Attachment order is {image_descriptions}. Candidate JSON is {json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))}. "
        f"Use job_id {job['job_id']} and job_fingerprint {job['job_fingerprint']}. "
        "Put null for options.E verdict when the candidate has no E. In differences, put a concise failure "
        "description for each failed field and null for every passing field. Emit only schema-valid JSON and set "
        "reviewer to Codex independent all-vision verification 2026-08-30."
    )


def _validate_bound_schema(schema: Path, job: dict[str, Any]) -> None:
    expected = job.get("schema_bindings", {}).get("codex-verification-result.schema.json")
    try:
        actual = hashlib.sha256(Path(schema).read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"Cannot read the verification output schema: {schema}") from error
    if not isinstance(expected, str) or actual != expected:
        raise ValueError("The verification output schema bytes are not bound to this job fingerprint.")


def _valid_existing(path: Path, job: dict[str, Any]) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return value.get("job_id") == job["job_id"] and value.get("job_fingerprint") == job["job_fingerprint"]


def _validate(value: dict[str, Any], job: dict[str, Any]) -> None:
    if value.get("job_id") != job["job_id"] or value.get("job_fingerprint") != job["job_fingerprint"]:
        raise ValueError("Verification result does not match its job.")
    labels = set(_candidate(job)["options"])
    expected = {"question", "answer_mapping", "solution", "readability", "clipping"} | {f"options.{label}" for label in labels}
    if set(value.get("verdicts", {})) != expected:
        raise ValueError("Verification verdict fields do not match the candidate options.")
    if any(verdict not in {"pass", "fail"} for verdict in value["verdicts"].values()):
        raise ValueError("Verification verdicts must be pass or fail.")
    failed = {field for field, verdict in value["verdicts"].items() if verdict == "fail"}
    if not failed <= set(value.get("differences", {})):
        raise ValueError("Every failed field requires a difference description.")


def _run_one(job: dict[str, Any], results: Path, schema: Path, model: str, reasoning: str, timeout: int, force: bool) -> tuple[str, str]:
    _validate_bound_schema(schema, job)
    output = results / f"{job['job_id']}.json"
    results.mkdir(parents=True, exist_ok=True)
    if not force and _valid_existing(output, job):
        return job["job_id"], "skipped"
    temporary = output.with_suffix(".codex.json")
    temporary.unlink(missing_ok=True)
    command = [
        "codex", "exec", _prompt(job), "--ephemeral", "--ignore-user-config", "--ignore-rules",
        "--model", model, "-c", f'model_reasoning_effort="{reasoning}"', "-s", "read-only",
        "--output-schema", str(schema), "-o", str(temporary),
    ]
    for source in _images(job):
        command.extend(["-i", str(source["path"])])
    completed = subprocess.run(
        command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )
    log_dir = results / "codex-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{job['job_id']}.log").write_text(
        completed.stdout + "\n--- STDERR ---\n" + completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0 or not temporary.is_file():
        raise RuntimeError(f"Codex exited {completed.returncode}; see {log_dir / (job['job_id'] + '.log')}")
    value = normalized_pipeline_result(json.loads(temporary.read_text(encoding="utf-8")))
    _validate(value, job)
    output.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.unlink(missing_ok=True)
    return job["job_id"], "completed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    jobs = [json.loads(line) for line in Path(args.queue).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit is not None:
        jobs = jobs[: args.limit]
    results = Path(args.results)
    counts = {"completed": 0, "skipped": 0}
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_run_one, job, results, Path(args.schema), args.model, args.reasoning, args.timeout, args.force): job
            for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            try:
                job_id, status = future.result()
                counts[status] += 1
                with _PRINT_LOCK:
                    print(json.dumps({"job_id": job_id, "status": status, "done": sum(counts.values()), "total": len(jobs)}), flush=True)
            except Exception as error:
                failures.append((job["job_id"], str(error)))
                with _PRINT_LOCK:
                    print(json.dumps({"job_id": job["job_id"], "status": "failed", "error": str(error)}), flush=True)
    print(json.dumps({"status": "finished", **counts, "failures": failures, "total": len(jobs)}))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
