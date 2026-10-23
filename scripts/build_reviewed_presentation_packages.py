"""Build staged, non-promoting presentation corrections from an explicit review.

CLI: python scripts/build_reviewed_presentation_packages.py --root BASELINE_DIR
     --review REVIEW.json --source-pdf SOURCE.pdf --output NEW_OUTPUT_DIR

Review schema 1: {source_pdf_sha256, chapters: {"24": {baseline_sha256, bank_name?: ...,
records: [{key, original_text_sha256, question_text, review_note, fallback_text?: ...,
media?: {path, sha256, alt_text, placement: "context"}}]}}}.
Include schema_version: 1. Relative media paths resolve beside the review JSON.
The baseline folder's direct ZIP children are matched by exact archive hash;
names and missing keys are never inferred. Output ZIP names retain baseline names.

Only question_text, display_media.question and an explicitly reviewed
manifest.bank_name change. Categories and record identities remain unchanged.
The original full text is the media alt_text unless nonempty fallback_text is
explicitly reviewed; supplied media.alt_text documents the human review.
Directions and prompts must be retained by the human-authored replacement text.
This tool cannot establish semantic correctness or source-crop provenance from
pixels. It verifies the exact reviewed bytes, not whether a review is truthful.

Old metadata remains byte-for-byte baseline history, not approval of this output.
Record hashes use UTF-8 JSON with sorted keys, ensure_ascii=False and separators
(',', ':'); text hashes use exact UTF-8, without whitespace normalization.
Requires Pillow and the application's normal parser dependencies. No promotion,
database import, latest/master update, or source-image generation is performed.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import warnings
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE = "metadata/presentation-maintenance.json"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _json(content):
    return json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()), f"{label} must be nonempty text.")
    return value


def _hash(value, label):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), f"{label} must be a lowercase SHA-256.")
    return value


def _fields(value, required, optional, label):
    _require(isinstance(value, dict), f"{label} must be an object.")
    _require(set(required) <= value.keys() and not value.keys() - set(required) - set(optional),
             f"{label} has missing or unsupported fields.")


def _file_bytes(path, maximum):
    _require(path.is_file(), f"Missing file: {path}")
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    _require(len(content) <= maximum, f"File exceeds size limit: {path}")
    return content


def _file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_member(name):
    _require(isinstance(name, str) and name and "\\" not in name and ":" not in name
             and not any(ord(c) < 32 for c in name), "Unsafe ZIP member path.")
    path = PurePosixPath(name)
    _require(not path.is_absolute() and all(part not in {"", ".", ".."} for part in name.rstrip("/").split("/")),
             f"Unsafe ZIP member path: {name}")


def _read_archive(content):
    members = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        _require(len(infos) <= 10000 and sum(i.file_size for i in infos) <= MAX_ARCHIVE_BYTES,
                 "Baseline ZIP exceeds safe extraction limits.")
        seen = set()
        for info in infos:
            _safe_member(info.filename)
            folded = info.filename.casefold()
            _require(folded not in seen, "Duplicate ZIP member.")
            seen.add(folded)
            _require(not stat.S_ISLNK(info.external_attr >> 16), "ZIP symlinks are unsafe.")
            if not info.is_dir():
                members[info.filename] = archive.read(info)
    return members


def _png(content, expected):
    _require(_sha(content) == _hash(expected, "image sha256"), "Image hash mismatch.")
    _require(len(content) <= MAX_IMAGE_BYTES and content.startswith(b"\x89PNG\r\n\x1a\n"), "Unsafe image: expected bounded PNG bytes.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as picture:
                _require(picture.format == "PNG" and getattr(picture, "n_frames", 1) == 1,
                         "Unsafe image: only static PNGs are accepted.")
                width, height = picture.size
                _require(0 < width <= 16384 and 0 < height <= 16384 and width * height <= 40_000_000,
                         "Unsafe image dimensions.")
                picture.verify()
            with Image.open(io.BytesIO(content)) as picture:
                picture.load()
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("Unsafe or corrupt PNG image.") from error


def _check_source_claims(value, source_hash):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_pdf_sha256":
                _require(item == source_hash, "Baseline source PDF hash mismatch.")
            else:
                _check_source_claims(item, source_hash)
    elif isinstance(value, list):
        for item in value:
            _check_source_claims(item, source_hash)


def _parse_package(content):
    # Import the public parser only; do not start the app or inspect its source.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import app
    try:
        return app.parse_question_package(io.BytesIO(content))
    except Exception as error:
        raise ValueError(f"Question package parser rejected package: {error}") from error


def _question_files(manifest, members):
    files = manifest.get("question_files")
    _require(isinstance(files, list) and files and all(isinstance(f, str) for f in files)
             and len(set(files)) == len(files), "Invalid question_files list.")
    documents = {}
    records = {}
    for name in files:
        _require(name in members, f"Missing question file: {name}")
        if name.endswith(".jsonl"):
            document = [_json(line) for line in members[name].decode("utf-8").splitlines() if line.strip()]
            rows = document
        else:
            _require(name.endswith(".json"), "Question file must be JSON or JSONL.")
            document = _json(members[name])
            rows = document.get("questions") if isinstance(document, dict) else document
        _require(isinstance(rows, list) and rows, "Question file must contain records.")
        for row in rows:
            _require(isinstance(row, dict), "Question record must be an object.")
            key = _text(row.get("key"), "baseline stable key")
            _require(key not in records, f"Duplicate baseline key: {key}")
            records[key] = (name, row)
        documents[name] = document
    return documents, records


def _build_members(content, chapter, review, source_hash, review_hash, review_dir):
    members = _read_archive(content)
    _require("manifest.json" in members, "Baseline manifest is missing.")
    manifest = _json(members["manifest.json"])
    _require(isinstance(manifest, dict) and manifest.get("format_version") == 3, "Baseline must be format v3.")
    before_manifest_sha256 = _sha(members["manifest.json"])
    before_bank_name = manifest.get("bank_name")
    _require(MAINTENANCE not in members, "Baseline already contains presentation maintenance; refusing to overwrite history.")
    for name, payload in members.items():
        if name == "manifest.json" or name.startswith("metadata/"):
            if name.endswith(".json"):
                _check_source_claims(_json(payload), source_hash)
            elif name.endswith(".jsonl"):
                for line in payload.decode("utf-8").splitlines():
                    if line.strip():
                        _check_source_claims(_json(line), source_hash)
    documents, records = _question_files(manifest, members)
    _require(all(str(row.get("chapter")) == chapter for _, row in records.values()),
             "Baseline record chapter does not match reviewed chapter.")
    _, parsed_before, _, version = _parse_package(content)
    _require(version == 3 and len(parsed_before) == len(records), "Baseline parser record count mismatch.")
    changed = set()
    patched = set()
    history = []
    for patch in review["records"]:
        _fields(patch, {"key", "original_text_sha256", "question_text", "review_note"}, {"media", "fallback_text"}, "review record")
        key = _text(patch["key"], "review stable key")
        _require(key not in patched, f"Duplicate patch: {key}")
        patched.add(key)
        _require(key in records, f"Unknown baseline key: {key}")
        name, question = records[key]
        before = copy.deepcopy(question)
        original = _text(question.get("question_text"), "original question_text")
        _require(_sha(original.encode("utf-8")) == _hash(patch["original_text_sha256"], "original_text_sha256"),
                 f"Original text hash mismatch: {key}")
        replacement = _text(patch["question_text"], "replacement question_text")
        note = _text(patch["review_note"], "review_note")
        fallback = _text(patch["fallback_text"], "fallback_text") if "fallback_text" in patch else original
        display = question.get("display_media", {})
        _require(isinstance(display, dict), "Invalid baseline display_media.")
        if "media" in patch:
            media = patch["media"]
            _fields(media, {"path", "sha256", "alt_text", "placement"}, set(), "review media")
            _text(media["alt_text"], "review media alt_text")
            _require(media["placement"] == "context", "Reviewed image placement must be context.")
            path = Path(_text(media["path"], "review image path"))
            if not path.is_absolute():
                path = review_dir / path
            _require(path.suffix.lower() == ".png", "Reviewed image must be a PNG file.")
            image = _file_bytes(path, MAX_IMAGE_BYTES)
            _png(image, media["sha256"])
            asset = f"assets/{_sha(image)}.png"
            _require(asset not in members or members[asset] == image, "Image asset collision.")
            members[asset] = image
            question_media = {"asset": asset, "sha256": _sha(image), "placement": "context"}
        else:
            question_media = copy.deepcopy(display.get("question"))
            _require(isinstance(question_media, dict) and question_media.get("placement") == "context",
                     f"{key} requires existing display_media.question context or reviewed media.")
            asset = question_media.get("asset")
            _safe_member(asset)
            _require(asset in members and asset.lower().endswith(".png"), "Existing context PNG is missing.")
            image = members[asset]
            _png(image, question_media.get("sha256"))
        question_media["alt_text"] = fallback
        question["question_text"] = replacement
        question["display_media"] = {**display, "question": question_media}
        changed.add(name)
        entry = {"key": key, "before_text_sha256": _sha(original.encode("utf-8")),
                 "after_text_sha256": _sha(replacement.encode("utf-8")),
                 "before_record_sha256": _sha(_canonical(before)), "after_record_sha256": _sha(_canonical(question)),
                 "image_sha256": _sha(image), "image_asset": asset, "review_note": note,
                 "fallback_text_overridden": "fallback_text" in patch, "fallback_text_sha256": _sha(fallback.encode("utf-8"))}
        if "fallback_text" in patch:
            entry["fallback_text"] = fallback
        if "media" in patch:
            entry["reviewed_media_alt_text"] = patch["media"]["alt_text"]
        history.append(entry)
    baseline_metadata = {name: _sha(payload) for name, payload in members.items() if name.startswith("metadata/")}
    for name in changed:
        document = documents[name]
        members[name] = (b"".join(_canonical(row) + b"\n" for row in document)
                         if name.endswith(".jsonl") else _canonical(document))
    if "bank_name" in review:
        manifest["bank_name"] = _text(review["bank_name"], "bank_name")
        members["manifest.json"] = _canonical(manifest)
    members[MAINTENANCE] = _canonical({
        "schema_version": 1, "kind": "reviewed-presentation-maintenance", "chapter": chapter,
        "baseline_archive_sha256": _sha(content), "source_pdf_sha256": source_hash,
        "review_manifest_sha256": review_hash, "records": history,
        "before_manifest_sha256": before_manifest_sha256,
        "after_manifest_sha256": _sha(members["manifest.json"]),
        "before_bank_name": before_bank_name, "after_bank_name": manifest.get("bank_name"),
        "baseline_history": {"approvals_apply_to": "baseline_archive_only", "metadata_sha256": baseline_metadata,
            "note": "Existing metadata and vision approvals are retained as baseline history. They do not approve this modified output."},
        "promotion_status": "staged_only", "validation": "app.parse_question_package; human review is supplied by the manifest",
    })
    return members, len(records)


def _write_zip(path, members):
    with zipfile.ZipFile(path, "x") as archive:
        for name, payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload, compresslevel=9)


def _publish_directory(staging, output):
    """Atomic no-replace directory move; never fall back to an overwriting rename."""
    if os.name == "nt":
        os.rename(staging, output)  # Windows refuses even an existing empty directory.
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError("Atomic no-replace publication requires renameat2 on Linux.")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(output), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(output))
    else:
        raise OSError("Atomic no-replace directory publication is supported on Windows and Linux only.")


def build_packages(baseline_root: Path, review_path: Path, output_dir: Path, source_pdf: Path) -> list[Path]:
    """Verify all inputs, stage all ZIPs, then atomically expose a new output folder."""
    baseline = Path(baseline_root).resolve()
    output = Path(output_dir).absolute()
    review_path = Path(review_path).resolve()
    source_pdf = Path(source_pdf).resolve()
    _require(baseline.is_dir(), "Baseline root must be a directory.")
    resolved_output = output.resolve()
    _require(not (resolved_output == baseline or resolved_output.is_relative_to(baseline)
                  or baseline.is_relative_to(resolved_output)), "Input and output directories must be separate and non-overlapping.")
    if os.path.lexists(output):
        raise FileExistsError(f"Output already exists: {output}")
    review_bytes = _file_bytes(review_path, 16 * 1024 * 1024)
    review = _json(review_bytes)
    _fields(review, {"schema_version", "source_pdf_sha256", "chapters"}, set(), "review")
    _require(type(review["schema_version"]) is int and review["schema_version"] == 1, "Unsupported review schema_version.")
    source_hash = _hash(review["source_pdf_sha256"], "source_pdf_sha256")
    _require(source_pdf.is_file() and _file_sha(source_pdf) == source_hash, "Source PDF hash mismatch or missing file.")
    chapters = review["chapters"]
    _require(isinstance(chapters, dict) and chapters, "Review chapters must be nonempty.")
    archives = {}
    for path in sorted(baseline.glob("*.zip")):
        archives.setdefault(_file_sha(path), []).append(path)
    selected = set()
    outputs = []
    # The disposable directory is the only write before final publication.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".presentation-maintenance-", dir=output.parent) as temporary:
        staging = Path(temporary) / "packages"
        staging.mkdir()
        for chapter, chapter_review in chapters.items():
            _require(isinstance(chapter, str) and re.fullmatch(r"[1-9][0-9]*", chapter), "Chapter must be a canonical positive integer string.")
            _fields(chapter_review, {"baseline_sha256", "records"}, {"bank_name"}, "chapter review")
            expected = _hash(chapter_review["baseline_sha256"], "baseline_sha256")
            _require(isinstance(chapter_review["records"], list) and chapter_review["records"], "Chapter review records must be nonempty.")
            matches = archives.get(expected, [])
            _require(len(matches) == 1, f"Chapter {chapter}: baseline hash must match exactly one ZIP.")
            path = matches[0]
            _require(path not in selected, "Duplicate baseline selected for multiple chapters.")
            selected.add(path)
            content = _file_bytes(path, MAX_ARCHIVE_BYTES)
            _require(_sha(content) == expected, "Baseline hash changed while building.")
            members, count = _build_members(content, chapter, chapter_review, source_hash, _sha(review_bytes), review_path.parent)
            staged = staging / path.name
            _write_zip(staged, members)
            _, parsed, _, version = _parse_package(staged.read_bytes())
            _require(version == 3 and len(parsed) == count, "Staged package parser count mismatch.")
            outputs.append(output / path.name)
        _publish_directory(staging, output)
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", "--baseline-root", dest="baseline_root", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-pdf", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        paths = build_packages(args.baseline_root, args.review, args.output, args.source_pdf)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        parser.exit(2, f"Presentation maintenance blocked: {error}\n")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
