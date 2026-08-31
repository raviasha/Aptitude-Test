"""Run V2 textbook extraction jobs through isolated Codex vision workers."""

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
DEFAULT_SCHEMA = (
    PROJECT_ROOT
    / "data-engineering"
    / "textbook_chapters_v2"
    / "schemas"
    / "codex-extraction-result.schema.json"
)
_PRINT_LOCK = threading.Lock()


def normalized_pipeline_result(value: dict[str, Any]) -> dict[str, Any]:
    """Remove the structured-output placeholder for an absent option E."""
    result = copy.deepcopy(value)
    options = result.get("options", {})
    representation_options = result.get("representation", {}).get("options", {})
    if options.get("E") is None:
        options.pop("E", None)
        representation_options.pop("E", None)
    return result


def _result_path(job: dict[str, Any]) -> Path:
    job_path = Path(job["output_path"])
    return job_path.parent.parent / "extraction-results" / f"{job['job_id']}.json"


def _prompt(job: dict[str, Any]) -> str:
    sources = job["sources"]
    roles = ", ".join(f"{index + 1}:{source['role']}" for index, source in enumerate(sources))
    answer = next(source for source in sources if source["role"] == "answer_key")
    return (
        f"{job['prompt'].strip()} "
        "Operational attachment and output-envelope details follow. "
        f"Attachment order and roles are {roles}. "
        f"Use job_id {job['job_id']} and job_fingerprint {job['job_fingerprint']}. "
        f"The answer_key crop_sha256 is {answer['sha256']}. "
        "Put null in option E and representation option E when the source has only A through D. "
        "Set differences_from_legacy to an empty array and reviewer to Codex all-vision extraction 2026-08-30. "
        "Emit only one schema-valid JSON object."
    )


def _validate_bound_schema(schema: Path, job: dict[str, Any]) -> None:
    expected = job.get("schema_bindings", {}).get("codex-extraction-result.schema.json")
    try:
        actual = hashlib.sha256(Path(schema).read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"Cannot read the extraction output schema: {schema}") from error
    if not isinstance(expected, str) or actual != expected:
        raise ValueError("The extraction output schema bytes are not bound to this job fingerprint.")


def _valid_existing(path: Path, job: dict[str, Any]) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return value.get("job_id") == job["job_id"] and value.get("job_fingerprint") == job["job_fingerprint"]


def _validate_result(value: dict[str, Any], job: dict[str, Any]) -> None:
    if value.get("job_id") != job["job_id"] or value.get("job_fingerprint") != job["job_fingerprint"]:
        raise ValueError("Codex result does not match its extraction job.")
    labels = set(value.get("options", {}))
    if labels not in ({"A", "B", "C", "D"}, {"A", "B", "C", "D", "E"}):
        raise ValueError(f"Unsupported option labels: {sorted(labels)}")
    representation = value.get("representation", {})
    if representation.get("question") != "text" or representation.get("solution") != "text":
        raise ValueError("All-vision text-only extraction returned non-text representation.")
    if set(representation.get("options", {})) != labels:
        raise ValueError("Option representation labels do not match options.")
    if any(kind != "text" for kind in representation["options"].values()):
        raise ValueError("All option representations must be text.")
    correct = value.get("correct_answer")
    if correct not in labels or value.get("answer_key", {}).get("correct_answer") != correct:
        raise ValueError("Correct answer is inconsistent with the answer-key crop.")


def _run_one(
    job: dict[str, Any],
    *,
    schema: Path,
    model: str,
    reasoning: str,
    timeout: int,
    force: bool,
) -> tuple[str, str]:
    _validate_bound_schema(schema, job)
    output = _result_path(job)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not force and _valid_existing(output, job):
        return job["job_id"], "skipped"
    temporary = output.with_suffix(".codex.json")
    temporary.unlink(missing_ok=True)
    command = [
        "codex",
        "exec",
        _prompt(job),
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning}"',
        "-s",
        "read-only",
        "--output-schema",
        str(schema),
        "-o",
        str(temporary),
    ]
    for source in job["sources"]:
        command.extend(["-i", str(source["path"])])
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    log_dir = output.parent / "codex-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{job['job_id']}.log").write_text(
        completed.stdout + "\n--- STDERR ---\n" + completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode != 0 or not temporary.is_file():
        raise RuntimeError(f"Codex exited {completed.returncode}; see {log_dir / (job['job_id'] + '.log')}")
    value = normalized_pipeline_result(json.loads(temporary.read_text(encoding="utf-8")))
    _validate_result(value, job)
    output.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.unlink(missing_ok=True)
    return job["job_id"], "completed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    queue = Path(args.queue)
    jobs = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit is not None:
        jobs = jobs[: args.limit]
    failures: list[tuple[str, str]] = []
    counts = {"completed": 0, "skipped": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _run_one,
                job,
                schema=Path(args.schema),
                model=args.model,
                reasoning=args.reasoning,
                timeout=args.timeout,
                force=args.force,
            ): job
            for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            try:
                job_id, status = future.result()
                counts[status] += 1
                with _PRINT_LOCK:
                    print(json.dumps({"job_id": job_id, "status": status, "done": sum(counts.values()), "total": len(jobs)}), flush=True)
            except Exception as error:  # keep independent jobs running
                failures.append((job["job_id"], str(error)))
                with _PRINT_LOCK:
                    print(json.dumps({"job_id": job["job_id"], "status": "failed", "error": str(error)}), flush=True)
    print(json.dumps({"status": "finished", **counts, "failures": failures, "total": len(jobs)}))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
