"""Read-only semantic-reference guard; no pipeline/app imports or repairs.

Usage: python scripts/check_semantic_references.py bank.zip [other.zip ...]
       --report NEW_REPORT.json
JSON objects/arrays and JSONL are optional diagnostics, NOT publication checks.
Exit 0: clean; 21: references require review; 22: malformed input/report error.

Only question_text, question_html, options, explanation, solution_steps and
display_media/representation alt_text are scanned. Provenance and media asset
locations are deliberately excluded. This is not source approval, ZIP seal
validation, asset validation, or a substitute for independent visual review.
Even genuinely printed URLs require source review, never automatic stripping.

Reports bind the exact bytes read and this standalone script's version/hash.
Before publication, compare those hashes with the actual ZIP and reviewed guard;
a clean report for another hash is stale. report_sha256 detects accidental
report changes, not forgery. Existing reports are never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote


GUARD_VERSION = "semantic-references-v2"
RULES = (
    # Require a hostname-like UNC server and a nonempty share component.
    # Do not start inside a doubled/escaped LaTeX separator or treat \end{...} as a share.
    ("windows_path", re.compile(
        r"(?<![\w])[A-Za-z]:[\\/]"
        r'|(?<![\w\\])\\\\[\w.-]+\\(?!end\{)[^\s\\/:*?"<>|]+')),
    ("unix_path", re.compile(
        r"(?<![\w/])(?:~?/|\.{1,2}/)(?:[\w.~-]+/)+[^\s<>\"']+"
        r"|(?<![\w/])/(?:[^\s/<>\"']+\.(?:png|jpe?g|gif|webp|svg|pdf))\b", re.I)),
    ("url", re.compile(r"\b(?:https?://|file\s*:|sandbox\s*:|blob:"
                       r"|data:(?:[^\s,]*,|[\w.+-]+/))", re.I)),
    ("markdown_image", re.compile(r"!\[")),
    ("html_image", re.compile(r"<\s*(?:img|image|svg|picture|object|embed)\b", re.I)),
    ("relative_media_path", re.compile(
        r"(?<![\w])(?:[\w.-]+[\\/])+[^\s<>\"']+\.(?:png|jpe?g|gif|webp|svg)\b", re.I)),
    ("media_filename", re.compile(
        r"(?<![\w.-])[\w-][\w.-]*\.(?:png|jpe?g|gif|webp|svg|bmp|tiff?|avif|pdf)(?![\w])", re.I)),
)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _json(content):
    return json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _records(content: bytes, *, jsonl: bool):
    text = content.decode("utf-8-sig")
    value = [_json(line) for line in text.splitlines() if line.strip()] if jsonl else _json(text)
    records = [value] if isinstance(value, dict) else value
    if not isinstance(records, list) or not records or not all(isinstance(r, dict) for r in records):
        raise ValueError("Expected a nonempty record object/array or JSONL stream.")
    return records


def _text(value, field: str, *, allow_empty: bool = False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{field} must be a {'possibly empty' if allow_empty else 'nonempty'} string.")
    return value


def _alt_fields(value, field: str):
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            path = f"{field}.{key}"
            if key == "alt_text":
                yield path, _text(item, path)
            else:
                yield from _alt_fields(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _alt_fields(item, f"{field}[{index}]")


def _semantic_fields(record, *, diagnostic: bool):
    yield "question_text", _text(record.get("question_text"), "question_text")
    for field in ("question_html", "explanation"):
        if field in record:
            yield field, _text(record[field], field, allow_empty=True)
    options = record.get("options")
    if not isinstance(options, dict) or set(options) not in (set("ABCD"), set("ABCDE")):
        raise ValueError("options must contain exactly A-D or A-E.")
    for label, value in sorted(options.items()):
        if diagnostic and label == "E" and value is None:
            continue
        yield f"options.{label}", _text(value, f"options.{label}")
    steps = record.get("solution_steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("solution_steps must be a nonempty array.")
    for index, value in enumerate(steps):
        yield f"solution_steps[{index}]", _text(value, f"solution_steps[{index}]")
    for field in ("display_media", "representation"):
        if field in record:
            if not isinstance(record[field], dict):
                raise ValueError(f"{field} must be an object.")
            yield from _alt_fields(record[field], field)


def _identity(record, *, diagnostic: bool):
    if isinstance(record.get("key"), str) and record["key"].strip():
        return record["key"]
    if diagnostic:
        if isinstance(record.get("job_id"), str) and record["job_id"].strip():
            return record["job_id"]
        if all(type(record.get(k)) is int and record[k] > 0 for k in ("chapter", "question_number")):
            return f"ch{record['chapter']:03d}-q{record['question_number']:04d}"
    raise ValueError("Missing record identity (ZIP requires key; diagnostics also accept job_id or chapter/question_number).")


def _zip_records(content: bytes):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP member names.")
        manifest = _json(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("format_version") != 3:
            raise ValueError("Expected format-3 question-bank manifest.")
        members = manifest.get("question_files")
        if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
            raise ValueError("question_files must be a nonempty string array.")
        if len(members) != len(set(members)):
            raise ValueError("Repeated question_files member.")
        for member in members:
            path = PurePosixPath(member)
            if (path.is_absolute() or ".." in path.parts or "\\" in member or ":" in member
                    or path.suffix not in (".json", ".jsonl")):
                raise ValueError(f"Invalid question member: {member}")
            for record in _records(archive.read(member), jsonl=path.suffix == ".jsonl"):
                yield member, record


def scan_input(path: Path):
    path = Path(path).resolve()
    diagnostic = path.suffix.lower() in (".json", ".jsonl")
    result = {"path": str(path), "sha256": None,
              "kind": "diagnostic-json" if diagnostic else "publication-zip-semantics",
              "status": "error", "records_scanned": 0, "fields_scanned": 0, "findings": [], "errors": []}
    try:
        # Hash and parse one byte snapshot; never hash a different reopening.
        content = path.read_bytes()
        result["sha256"] = _sha(content)
        if path.suffix.lower() == ".zip":
            records = _zip_records(content)
        elif diagnostic:
            records = ((path.name, r) for r in _records(content, jsonl=path.suffix.lower() == ".jsonl"))
        else:
            raise ValueError("Input must be .zip, .json, or .jsonl.")
        identities = set()
        for member, record in records:
            identity = _identity(record, diagnostic=diagnostic)
            if identity in identities:
                raise ValueError(f"Duplicate record identity: {identity}")
            identities.add(identity)
            try:
                fields = list(_semantic_fields(record, diagnostic=diagnostic))
            except ValueError as error:
                raise ValueError(f"{member}: {identity}: {error}") from error
            for field, text in fields:
                # Decode common reference escapes only for detection; input stays untouched.
                normalized = html.unescape(unquote(text))
                for rule, pattern in RULES:
                    match = pattern.search(normalized)
                    if match:
                        result["findings"].append({"member": member, "record": identity, "field": field,
                                                   "rule": rule, "match": match.group(0)[:240]})
            result["records_scanned"] += 1
            result["fields_scanned"] += len(fields)
        if not result["records_scanned"]:
            raise ValueError("No records scanned.")
        result["status"] = "blocked" if result["findings"] else "clean"
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
        result["errors"].append(str(error))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--report", type=Path, help="Create a new report; never overwrite an existing file.")
    args = parser.parse_args(argv)
    report = {"guard_version": GUARD_VERSION, "guard_sha256": _sha(Path(__file__).read_bytes()),
              "scope": "Semantic-reference detection only; not source, seal, asset, or publication approval.",
              "inputs": [scan_input(path) for path in args.inputs], "errors": []}
    statuses = {item["status"] for item in report["inputs"]}
    report["status"] = "error" if "error" in statuses else "blocked" if "blocked" in statuses else "clean"
    report["report_sha256"] = _sha(_canonical(report))
    if args.report is not None:
        try:
            if args.report.resolve() in {path.resolve() for path in args.inputs}:
                raise ValueError("Report path must not alias an input.")
            with args.report.open("x", encoding="utf-8") as output:
                output.write(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
        except (OSError, ValueError) as error:
            report["status"] = "error"
            report["errors"].append(f"Report not saved: {error}")
            report.pop("report_sha256")
            report["report_sha256"] = _sha(_canonical(report))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return {"clean": 0, "blocked": 21, "error": 22}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
