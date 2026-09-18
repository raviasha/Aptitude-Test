from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _jsonl_bytes(records: list[dict]) -> bytes:
    return b"".join(_json_bytes(record) + b"\n" for record in records)


def _write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, payload, compresslevel=9)


def relabel_package(source: Path, output: Path, bank_name: str) -> Path:
    source = Path(source)
    output = Path(output)
    bank_name = bank_name.strip()
    if not bank_name:
        raise ValueError("bank_name must be non-empty")
    if output.exists():
        raise FileExistsError(output)

    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    if "manifest.json" not in members:
        raise ValueError("Question package has no manifest.json")
    manifest = json.loads(members["manifest.json"])
    if int(manifest.get("format_version", 0)) < 2:
        raise ValueError("Only manifest format_version 2 or newer can be relabelled")
    question_files = manifest.get("question_files")
    if not isinstance(question_files, list):
        raise ValueError("manifest.json has no valid question_files list")

    manifest["bank_name"] = bank_name
    members["manifest.json"] = _json_bytes(manifest)
    for name in question_files:
        if name not in members:
            raise ValueError(f"Question file is missing: {name}")
        records = [json.loads(line) for line in members[name].decode("utf-8").splitlines() if line.strip()]
        for record in records:
            record["category"] = bank_name
        members[name] = _jsonl_bytes(records)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x") as archive:
        for name in sorted(members):
            _write_member(archive, name, members[name])
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a display-labelled copy of a KSAT question package.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bank-name", required=True)
    args = parser.parse_args()
    print(relabel_package(args.input, args.output, args.bank_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
