"""Synthetic packages only: exercise maintenance without touching promoted banks."""
from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "scripts/build_reviewed_presentation_packages.py"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def builder():
    assert SCRIPT.is_file(), "Reviewed presentation package builder is missing"
    return importlib.import_module("scripts.build_reviewed_presentation_packages")


def zip_members(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.fixture
def case(tmp_path):
    baseline = tmp_path / "latest" / "quantitative"
    baseline.mkdir(parents=True)
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, format="PNG")
    png = image.getvalue()
    (tmp_path / "reviewed.png").write_bytes(png)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% Synthetic source bytes, never a production source.\n%%EOF\n")
    original = "Study the table and answer. Values: A=2, B=4. What is B/A?"
    question = {"key": "ch24-q0001", "chapter": "24", "category": "Area",
                "difficulty": "Medium", "question_text": original,
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "correct_answer": "B", "explanation": "Divide 4 by 2.",
                "solution_steps": ["4 / 2 = 2"], "custom": {"preserve": True},
                "display_media": {"question": {"asset": "assets/chart.png", "sha256": sha(png),
                    "placement": "context", "alt_text": "Old chart description"},
                    "options": {"A": {"asset": "assets/chart.png", "sha256": sha(png), "alt_text": "One"}}}}
    other = {**copy.deepcopy(question), "key": "ch24-q0002", "question_text": "Unchanged question?"}
    manifest = {"format_version": 3, "bank_name": "Area", "question_files": ["questions/one.jsonl", "questions/two.jsonl"],
                "audit_summary_file": "metadata/audit.json", "lineage_file": "metadata/lineage.json"}
    members = {"manifest.json": encoded(manifest), "questions/one.jsonl": encoded(question) + b"\n",
               "questions/two.jsonl": encoded(other) + b"\n", "assets/chart.png": png,
               "metadata/audit.json": encoded({"approved_count": 2, "vision_verdict": "pass"}),
               "metadata/lineage.json": encoded({"source_pdf_sha256": sha(pdf.read_bytes()), "records": []})}
    archive = baseline / "ch24_area.zip"
    review = {"schema_version": 1, "source_pdf_sha256": sha(pdf.read_bytes()), "chapters": {"24": {
        "baseline_sha256": "", "records": [{"key": question["key"], "original_text_sha256": sha(original.encode()),
        "question_text": "Study the table and answer. What is B/A?", "review_note": "Checked source: retain directions and prompt; remove duplicate table values."}]}}}
    result = {"root": tmp_path, "baseline": baseline, "archive": archive, "members": members, "question": question,
              "other": other, "review": review, "review_path": tmp_path / "review.json", "pdf": pdf,
              "output": tmp_path / "staged", "png": png}
    rewrite(result)
    return result


def rewrite(case):
    with zipfile.ZipFile(case["archive"], "w") as archive:
        for name, content in case["members"].items():
            archive.writestr(name, content)
    case["review"]["chapters"]["24"]["baseline_sha256"] = sha(case["archive"].read_bytes())
    save_review(case)


def save_review(case):
    case["review_path"].write_bytes(encoded(case["review"]))


def run(case):
    return builder().build_packages(case["baseline"], case["review_path"], case["output"], case["pdf"])


def patch_record(case):
    return case["review"]["chapters"]["24"]["records"][0]


def add_diagram(case):
    patch_record(case)["media"] = {"path": "reviewed.png", "sha256": sha(case["png"]),
        "alt_text": "Reviewed diagram description", "placement": "context"}
    case["question"]["display_media"].pop("question")
    case["members"]["questions/one.jsonl"] = encoded(case["question"]) + b"\n"
    rewrite(case)


def test_fixture_is_a_real_importable_v3_package(case):
    import app
    with case["archive"].open("rb") as stream:
        name, questions, _, version = app.parse_question_package(stream)
    assert (name, len(questions), version) == ("Area", 2, 3)


@pytest.mark.parametrize("diagram", [False, True])
def test_review_changes_only_presentation_and_binds_baseline_history(case, diagram):
    if diagram:
        add_diagram(case)
    baseline_bytes = case["archive"].read_bytes()
    paths = run(case)
    assert paths == [case["output"] / case["archive"].name]
    members = zip_members(paths[0])
    actual = json.loads(members["questions/one.jsonl"])
    assert actual["question_text"] == "Study the table and answer. What is B/A?"
    assert {k: v for k, v in actual.items() if k not in {"question_text", "display_media"}} == {
        k: v for k, v in case["question"].items() if k not in {"question_text", "display_media"}}
    media = actual["display_media"]["question"]
    assert media["placement"] == "context"
    assert media["alt_text"] == case["question"]["question_text"]
    assert members[media["asset"]] == case["png"]
    assert actual["display_media"]["options"] == case["question"]["display_media"]["options"]
    for name, content in case["members"].items():
        if name != "questions/one.jsonl":
            assert members[name] == content
    maintenance = json.loads(members["metadata/presentation-maintenance.json"])
    assert maintenance["baseline_archive_sha256"] == sha(baseline_bytes)
    assert maintenance["source_pdf_sha256"] == sha(case["pdf"].read_bytes())
    assert maintenance["review_manifest_sha256"] == sha(case["review_path"].read_bytes())
    assert maintenance["before_manifest_sha256"] == sha(case["members"]["manifest.json"])
    assert maintenance["after_manifest_sha256"] == sha(members["manifest.json"])
    assert maintenance["before_bank_name"] == maintenance["after_bank_name"] == "Area"
    assert maintenance["baseline_history"]["approvals_apply_to"] == "baseline_archive_only"
    record = maintenance["records"][0]
    assert record["key"] == "ch24-q0001"
    assert record["before_text_sha256"] == sha(case["question"]["question_text"].encode())
    assert record["after_text_sha256"] == sha(actual["question_text"].encode())
    assert record["before_record_sha256"] == sha(encoded(case["question"]))
    assert record["after_record_sha256"] == sha(encoded(actual))
    assert record["image_sha256"] == sha(case["png"])
    assert record["review_note"] == patch_record(case)["review_note"]
    assert record["fallback_text_overridden"] is False
    assert case["archive"].read_bytes() == baseline_bytes
    assert sorted(p.name for p in case["output"].iterdir()) == [case["archive"].name]
    import app
    with paths[0].open("rb") as stream:
        _, parsed, _, version = app.parse_question_package(stream)
    assert (len(parsed), version) == (2, 3)


@pytest.mark.parametrize("fault", ["source_hash", "source_bytes", "baseline_hash", "text_hash", "unknown_key",
    "duplicate_patch", "empty_text", "empty_note", "missing_context", "replacement_media", "image_hash",
    "image_missing", "image_not_png", "image_corrupt", "image_animated", "image_bomb", "image_placement",
    "empty_alt", "answer_edit", "duplicate_key", "missing_key", "source_lineage", "duplicate_json",
    "unsafe_member", "duplicate_zip_member", "schema", "empty_chapters", "empty_records", "ambiguous_baseline"])
def test_rejects_unreviewed_or_unsafe_inputs_without_output(case, fault):
    record = patch_record(case)
    if fault.startswith("image_") or fault == "empty_alt":
        add_diagram(case)
    if fault == "source_hash": case["review"]["source_pdf_sha256"] = "0" * 64
    elif fault == "source_bytes": case["pdf"].write_bytes(b"changed PDF")
    elif fault == "baseline_hash": case["review"]["chapters"]["24"]["baseline_sha256"] = "0" * 64
    elif fault == "text_hash": record["original_text_sha256"] = "0" * 64
    elif fault == "unknown_key": record["key"] = "ch24-q9999"
    elif fault == "duplicate_patch": case["review"]["chapters"]["24"]["records"].append(copy.deepcopy(record))
    elif fault == "empty_text": record["question_text"] = " \n"
    elif fault == "empty_note": record["review_note"] = " "
    elif fault == "answer_edit": record["correct_answer"] = "A"
    elif fault == "image_hash": record["media"]["sha256"] = "0" * 64
    elif fault == "image_missing": record["media"]["path"] = "missing.png"
    elif fault == "image_placement": record["media"]["placement"] = "replacement"
    elif fault == "empty_alt": record["media"]["alt_text"] = " "
    elif fault in {"image_not_png", "image_corrupt", "image_animated", "image_bomb"}:
        content = b"<svg><script>alert(1)</script></svg>" if fault == "image_not_png" else case["png"][:35]
        if fault == "image_animated":
            stream = io.BytesIO()
            Image.new("RGB", (8, 8), "white").save(stream, format="PNG", save_all=True,
                append_images=[Image.new("RGB", (8, 8), "black")], duration=100, loop=0)
            content = stream.getvalue()
        if fault == "image_bomb":
            import struct, zlib
            content = bytearray(case["png"])
            content[16:24] = struct.pack(">II", 100000, 100000)
            content[29:33] = struct.pack(">I", zlib.crc32(content[12:29]))
            content = bytes(content)
        (case["root"] / "reviewed.png").write_bytes(content)
        record["media"]["sha256"] = sha(content)
    elif fault in {"missing_context", "replacement_media", "missing_key"}:
        q = copy.deepcopy(case["question"])
        if fault == "missing_context": q["display_media"].pop("question")
        elif fault == "replacement_media": q["display_media"]["question"]["placement"] = "replacement"
        else: q.pop("key")
        case["members"]["questions/one.jsonl"] = encoded(q)
        rewrite(case)
    elif fault == "duplicate_key":
        case["members"]["questions/two.jsonl"] = encoded(case["question"])
        rewrite(case)
    elif fault == "source_lineage":
        case["members"]["metadata/lineage.json"] = encoded({"source_pdf_sha256": "0" * 64})
        rewrite(case)
    elif fault == "unsafe_member":
        case["members"]["../escape.png"] = case["png"]
        rewrite(case)
    elif fault == "schema": case["review"]["schema_version"] = 2
    elif fault == "empty_chapters": case["review"]["chapters"] = {}
    elif fault == "empty_records": case["review"]["chapters"]["24"]["records"] = []
    elif fault == "ambiguous_baseline": (case["baseline"] / "duplicate.zip").write_bytes(case["archive"].read_bytes())
    elif fault == "duplicate_zip_member":
        with zipfile.ZipFile(case["archive"], "a") as archive:
            with pytest.warns(UserWarning): archive.writestr("manifest.json", case["members"]["manifest.json"])
        case["review"]["chapters"]["24"]["baseline_sha256"] = sha(case["archive"].read_bytes())
    save_review(case)
    if fault == "duplicate_json":
        case["review_path"].write_text('{"schema_version":1,' + case["review_path"].read_text()[1:])
    with pytest.raises((ValueError, OSError)):
        run(case)
    assert not case["output"].exists()


def test_existing_and_overlapping_output_are_never_modified(case):
    module = builder()
    original = case["archive"].read_bytes()
    for output in [case["baseline"], case["baseline"] / "new", case["root"]]:
        with pytest.raises((ValueError, OSError)):
            module.build_packages(case["baseline"], case["review_path"], output, case["pdf"])
    assert case["archive"].read_bytes() == original
    case["output"].mkdir()
    marker = case["output"] / "keep"
    marker.write_bytes(b"user file")
    with pytest.raises((ValueError, OSError)): run(case)
    assert marker.read_bytes() == b"user file"


def test_parser_failure_does_not_publish_any_zip(case, monkeypatch):
    module = builder()
    import app
    real = app.parse_question_package
    calls = []
    def reject_staged(stream):
        parsed = real(stream)
        calls.append(stream)
        if len(calls) == 2:
            raise ValueError("Injected staged package validation failure")
        return parsed
    monkeypatch.setattr(app, "parse_question_package", reject_staged)
    with pytest.raises(ValueError, match="staged package validation failure"):
        module.build_packages(case["baseline"], case["review_path"], case["output"], case["pdf"])
    assert len(calls) == 2
    assert not case["output"].exists()


@pytest.mark.parametrize("empty", [False, True])
def test_publish_race_never_overwrites_other_output(case, monkeypatch, empty):
    module = builder()
    publish = module._publish_directory
    def competing_output(staging, output):
        output.mkdir()
        if not empty:
            (output / "user.txt").write_bytes(b"race winner")
        publish(staging, output)
    monkeypatch.setattr(module, "_publish_directory", competing_output)
    with pytest.raises((ValueError, OSError)): run(case)
    assert list(case["output"].iterdir()) == ([] if empty else [case["output"] / "user.txt"])
    if not empty:
        assert (case["output"] / "user.txt").read_bytes() == b"race winner"


@pytest.mark.parametrize("container", ["jsonl", "json", "wrapped_json"])
def test_unpatched_record_and_json_document_fields_survive(case, container):
    name = "questions/all.jsonl" if container == "jsonl" else "questions/all.json"
    rows = [case["question"], case["other"]]
    document = {"questions": rows, "source_note": "Keep this too"} if container == "wrapped_json" else rows
    content = b"\n".join(encoded(row) for row in rows) if container == "jsonl" else encoded(document)
    manifest = json.loads(case["members"]["manifest.json"])
    manifest["question_files"] = [name]
    case["members"]["manifest.json"] = encoded(manifest)
    case["members"][name] = content
    rewrite(case)
    result = zip_members(run(case)[0])
    document = [json.loads(line) for line in result[name].splitlines()] if container == "jsonl" else json.loads(result[name])
    if container == "wrapped_json":
        assert document["source_note"] == "Keep this too"
        document = document["questions"]
    assert document[1] == case["other"]


@pytest.mark.parametrize("fail_second", [False, True])
def test_multiple_chapters_are_published_together_or_not_at_all(case, fail_second):
    other_members = {}
    for name, content in case["members"].items():
        if name.endswith(".jsonl"):
            q = json.loads(content)
            q["key"] = q["key"].replace("ch24-", "ch25-")
            q["chapter"] = "25"
            content = encoded(q)
        other_members[name] = content
    second = case["baseline"] / "ch25_volume.zip"
    with zipfile.ZipFile(second, "w") as archive:
        for name, content in other_members.items(): archive.writestr(name, content)
    review = copy.deepcopy(case["review"]["chapters"]["24"])
    review["baseline_sha256"] = sha(second.read_bytes())
    review["records"][0]["key"] = "ch25-q0001"
    if fail_second: review["records"][0]["original_text_sha256"] = "0" * 64
    case["review"]["chapters"]["25"] = review
    save_review(case)
    if fail_second:
        with pytest.raises(ValueError, match="text hash mismatch"): run(case)
        assert not case["output"].exists()
    else:
        assert {path.name for path in run(case)} == {"ch24_area.zip", "ch25_volume.zip"}
    assert not list(case["root"].glob(".presentation-maintenance-*"))


def test_chapter_mismatch_is_not_silently_relabelled(case):
    case["review"]["chapters"]["25"] = case["review"]["chapters"].pop("24")
    save_review(case)
    with pytest.raises(ValueError, match="chapter"):
        run(case)
    assert not case["output"].exists()


@pytest.mark.parametrize("fault", ["hash", "corrupt", "history"])
def test_existing_context_and_maintenance_history_are_verified(case, fault):
    if fault == "hash":
        case["question"]["display_media"]["question"]["sha256"] = "0" * 64
        case["members"]["questions/one.jsonl"] = encoded(case["question"])
    elif fault == "corrupt":
        case["members"]["assets/chart.png"] = b"bad image"
    else:
        case["members"]["metadata/presentation-maintenance.json"] = encoded({"existing": "history"})
    rewrite(case)
    with pytest.raises(ValueError): run(case)
    assert not case["output"].exists()


@pytest.mark.parametrize("diagram", [False, True])
def test_explicit_revision_name_and_fallback_override_are_bound_to_review(case, diagram):
    if diagram:
        add_diagram(case)
    case["review"]["chapters"]["24"]["bank_name"] = "Area — reviewed presentation revision"
    patch_record(case)["fallback_text"] = "Study the diagram. Rectangle ABCD has sides 2 and 4. What is B/A?"
    save_review(case)
    members = zip_members(run(case)[0])
    manifest = json.loads(members["manifest.json"])
    original_manifest = json.loads(case["members"]["manifest.json"])
    assert manifest == {**original_manifest, "bank_name": "Area — reviewed presentation revision"}
    question = json.loads(members["questions/one.jsonl"])
    assert question["category"] == "Area"
    assert question["display_media"]["question"]["alt_text"] == patch_record(case)["fallback_text"]
    history = json.loads(members["metadata/presentation-maintenance.json"])
    assert history["before_bank_name"] == "Area"
    assert history["after_bank_name"] == manifest["bank_name"]
    assert history["before_manifest_sha256"] == sha(case["members"]["manifest.json"])
    assert history["after_manifest_sha256"] == sha(members["manifest.json"])
    record = history["records"][0]
    assert record["fallback_text_overridden"] is True
    assert record["fallback_text"] == patch_record(case)["fallback_text"]
    assert record["fallback_text_sha256"] == sha(patch_record(case)["fallback_text"].encode())
    assert record["before_text_sha256"] == sha(case["question"]["question_text"].encode())
    assert record["after_record_sha256"] == sha(encoded(question))


@pytest.mark.parametrize("level,field,value", [
    ("chapter", "bank_name", " "), ("chapter", "bank_name", None),
    ("record", "fallback_text", "\n"), ("record", "fallback_text", None),
    ("chapter", "bank_nam", "unreviewed"), ("record", "fallback_txt", "unreviewed"),
])
def test_optional_fields_remain_strict_and_reject_unknown_fields(case, level, field, value):
    target = case["review"]["chapters"]["24"] if level == "chapter" else patch_record(case)
    target[field] = value
    save_review(case)
    with pytest.raises(ValueError): run(case)
    assert not case["output"].exists()


def test_cli_and_deterministic_builds(case):
    builder()
    command = [sys.executable, "-B", str(SCRIPT), "--root", str(case["baseline"]),
        "--review", str(case["review_path"]), "--output", str(case["output"]), "--source-pdf", str(case["pdf"])]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60,
        env={**os.environ, "KSAT_DATA_DIR": str(case["root"] / "isolated-data")})
    assert result.returncode == 0, result.stderr
    first = (case["output"] / case["archive"].name).read_bytes()
    case["output"] = case["root"] / "second-build"
    assert run(case)[0].read_bytes() == first
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert repeated.returncode != 0
