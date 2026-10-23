"""Prepare authorized maintenance release artifacts locally; never upload or seal.

Only the two outer ZIP READMEs and external publication.json are generated.
Chapter ZIPs, original V2 metadata, latest/selection.json and the original master
are read-only inputs. validation.json remains the original creation-time report.
Run with --check to verify the prepared artifacts without writing anything.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = "question-banks/quantitative-visual-update-20261022"
UPDATE = "quantitative-visual-updates-6-chapters.zip"
MASTER = "quantitative-aptitude-master-39-chapters.zip"
MAINTENANCE = "metadata/presentation-maintenance.json"
REVISED = {24, 25, 36, 37, 38, 39}


def sha(content):
    return hashlib.sha256(content).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def replace_outputs(output, generated):
    """Publish a validated batch with rollback and retained recovery originals.

    A hard process termination leaves .publication-* originals for recovery.
    An ordinary replacement failure restores the full pre-publication state.
    If restoration itself fails, keep the recovery directory and report it.
    """
    require(all(Path(name).name == name for name in generated), "Unsafe output name")
    staging = Path(tempfile.mkdtemp(prefix=".publication-", dir=output))
    originals = staging / "originals"
    originals.mkdir()
    existed = {name: (output / name).exists() for name in generated}
    replaced = []
    try:
        for name, content in generated.items():
            if existed[name]:
                shutil.copyfile(output / name, originals / name)
            (staging / name).write_bytes(content)
        (staging / "recovery.json").write_text(json.dumps(existed), encoding="utf-8")
        try:
            for name in generated:
                (staging / name).replace(output / name)
                replaced.append(name)
        except BaseException:
            try:
                for name in reversed(replaced):
                    if existed[name]:
                        # Copy, rather than move, so recovery originals survive a
                        # second failure during rollback.
                        restore = staging / (name + ".restore")
                        shutil.copyfile(originals / name, restore)
                        restore.replace(output / name)
                    else:
                        (output / name).unlink()
            except BaseException as recovery_error:
                raise RuntimeError(f"Publication rollback incomplete; originals retained in {staging}") from recovery_error
            shutil.rmtree(staging)
            raise
    except BaseException:
        # Never discard the only recovery copies on an unexpected failure.
        raise
    else:
        shutil.rmtree(staging)


def repack(content, chapters, readme):
    """Validate the old container before replacing only its README member."""
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        names = source.namelist()
        require(len(names) == len(set(names)), "Duplicate outer ZIP members")
        require(set(names) == set(chapters) | {"README.txt", "SHA256SUMS.json"},
                "Unexpected outer ZIP inventory")
        require(json.loads(source.read("SHA256SUMS.json")) == {
            name: sha(data) for name, data in chapters.items()
        }, "Chapter hash inventory mismatch")
        for name, data in chapters.items():
            require(source.read(name) == data, f"Embedded chapter differs: {name}")
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w") as target:
            target.comment = source.comment
            for info in source.infolist():
                target.writestr(info, readme if info.filename == "README.txt" else source.read(info))
    with zipfile.ZipFile(io.BytesIO(result.getvalue())) as target:
        require(target.testzip() is None, "Repacked ZIP integrity failure")
        require(target.read("README.txt") == readme, "Repacked README mismatch")
        for name, data in chapters.items():
            require(target.read(name) == data, f"Repacked chapter differs: {name}")
    return result.getvalue()


def prepare(root=ROOT, check=False):
    root = Path(root).resolve()
    output = root / DIRECTORY
    latest = root / "question-banks/latest"
    baseline = latest / "quantitative-aptitude"
    validation_path = output / "validation.json"
    report = json.loads(validation_path.read_bytes())
    selection_path = latest / "selection.json"
    selection = json.loads(selection_path.read_bytes())
    selected = {Path(row["path"]).name: row for row in selection["packages"]
                if row["path"].startswith("quantitative-aptitude/")}
    require(len(selected) == 39, "Expected 39 baseline selection entries")
    original_master = latest / "textbook-downloads/quantitative-aptitude-chapters-01-39.zip"
    originals = sorted(baseline.glob("*.zip"))
    require({p.name for p in originals} == set(selected), "Baseline chapter inventory differs")
    revised = {row["file"]: row for row in report["chapters"]}
    require(len(revised) == 6 and {int(row["chapter"]) for row in revised.values()} == REVISED,
            "Expected six revised chapters: 24, 25, 36–39")
    require({p.name for p in (output / "chapters").glob("*.zip")} == set(revised),
            "Revised chapter inventory differs")
    protected = originals + [output / "chapters" / name for name in revised]
    protected += [selection_path, original_master, validation_path, output / "review-manifest.json"]
    before = {path: sha(path.read_bytes()) for path in protected}
    review_hash = before[output / "review-manifest.json"]
    all_chapters, updated_chapters, inventory = {}, {}, []
    for path in originals:
        baseline_data = path.read_bytes()
        require(sha(baseline_data) == selected[path.name]["sha256"], f"Baseline hash differs: {path.name}")
        chapter = int(path.name[2:4])
        is_revised = path.name in revised
        current = output / "chapters" / path.name if is_revised else path
        content = current.read_bytes()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            require(archive.testzip() is None, f"Invalid chapter ZIP: {path.name}")
            manifest = json.loads(archive.read("manifest.json"))
            if is_revised:
                row = revised[path.name]
                require(sha(content) == row["sha256"], f"Revised chapter hash differs: {path.name}")
                require(manifest["bank_name"] == row["bank_name"] and
                        manifest["bank_name"].endswith(" [Visual revision 2026-10-22]"),
                        f"Revision bank name differs: {path.name}")
                maintenance = json.loads(archive.read(MAINTENANCE))
                require(maintenance["promotion_status"] == "staged_only",
                        "Chapter creation-stage history changed")
                require(maintenance["baseline_archive_sha256"] == sha(baseline_data) and
                        maintenance["review_manifest_sha256"] == review_hash,
                        "Maintenance provenance differs")
                require(maintenance["baseline_history"]["approvals_apply_to"] == "baseline_archive_only",
                        "Original approvals must apply only to baseline history")
                with zipfile.ZipFile(io.BytesIO(baseline_data)) as old:
                    for name in old.namelist():
                        if name.startswith("metadata/"):
                            require(archive.read(name) == old.read(name), f"Baseline metadata changed: {name}")
        member = "chapters/" + path.name
        all_chapters[member] = content
        if is_revised:
            updated_chapters[member] = content
        inventory.append({
            "chapter": chapter,
            "path": "chapters/" + path.name if is_revised else "../latest/quantitative-aptitude/" + path.name,
            "sha256": sha(content), "baseline_sha256": sha(baseline_data),
            "revision": "presentation-maintenance" if is_revised else "unchanged-baseline",
            "questions": selected[path.name]["questions"], "bank_name": manifest["bank_name"],
        })

    publication_path = output / "publication.json"
    previous = json.loads(publication_path.read_bytes()) if publication_path.exists() else None
    expected_bundles = {row["file"]: row["sha256"] for row in (previous or report)["bundles"]}
    readme = (output / "README.txt").read_bytes()
    bundles, generated = [], {}
    for name, chapters in ((UPDATE, updated_chapters), (MASTER, all_chapters)):
        current = (output / name).read_bytes()
        require(sha(current) == expected_bundles[name], f"Unexpected outer ZIP hash: {name}")
        updated = repack(current, chapters, readme)
        if check:
            require(current == updated, f"Outer README or packaging needs refresh: {name}")
        generated[name] = updated
        bundles.append({"file": name, "sha256": sha(updated), "chapters": len(chapters),
                        "chapter_sha256": {key: sha(value) for key, value in chapters.items()}})
    publication = {
        "schema_version": 1, "revision_date": "2026-10-22",
        "kind": "presentation-maintenance",
        "status": "release_candidate_for_approved_publication",
        "release_authorized": True,
        "authorization": "User authorized publication of the completed visual fixes and question banks with Coordinator 2.2.3 and Windows Client/Builder 2.1.6.",
        "github_publication": "not_confirmed",
        "accompanying_release": {"coordinator": "2.2.3", "windows_client": "2.1.6", "windows_builder": "2.1.6"},
        "approval_scope": {
            "original_v2_metadata": "Preserved byte-for-byte as baseline history; original approvals apply only to the baseline.",
            "new_vision_approvals": False,
            "chapter_promotion_status": "staged_only is unchanged creation-stage history; this external index records release authorization.",
        },
        "baseline_index": {"path": "../latest/selection.json",
                           "sha256": sha(selection_path.read_bytes().replace(b"\r\n", b"\n")),
                           "hash_normalization": "CRLF line endings converted to LF; original file is not modified",
                           "role": "Original baseline index retained unchanged; this publication.json indexes the maintenance revision."},
        "baseline_master": {"path": "../latest/textbook-downloads/" + original_master.name,
                            "sha256": before[original_master]},
        "creation_validation": {"path": "validation.json", "sha256": before[validation_path],
                                "role": "Unchanged creation-time evidence; its bundle hashes predate the outer README replacement. Current hashes are in bundles below."},
        "review_manifest": {"path": "review-manifest.json", "sha256": review_hash},
        "readme_sha256": sha(readme),
        "revised_chapters": sorted(REVISED), "changed_records": report["changed_records"],
        "chapters": inventory, "bundles": bundles,
        "preservation": {"embedded_chapter_zip_bytes_unchanged": True,
                         "baseline_archives_and_index_unchanged": True,
                         "original_v2_metadata_unchanged": True},
        "import_guidance": {
            "coordinator": "2.2.3 directly imports either outer ZIP.",
            "existing_installations": UPDATE, "new_installations": MASTER,
            "bank_name_suffix": "[Visual revision 2026-10-22]",
            "history": "Revised banks coexist with original banks. Keep old banks and results; select only one version per chapter for new tests. Existing releases remain unchanged.",
        },
    }
    require(all(sha(path.read_bytes()) == digest for path, digest in before.items()),
            "Protected input changed during preparation")
    if check:
        require(previous == publication, "Publication index does not match verified artifacts")
    else:
        # Validate the whole batch first; retain originals until every replacement succeeds.
        replace_outputs(output, {**generated, "publication.json":
            (json.dumps(publication, ensure_ascii=False, indent=2) + "\n").encode("utf-8")})
        require(all(sha(path.read_bytes()) == digest for path, digest in before.items()),
                "Protected input changed during preparation")
    return publication


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify prepared release artifacts without writing")
    args = parser.parse_args()
    publication = prepare(check=args.check)
    print(json.dumps({"status": publication["status"], "check_only": args.check,
                      "verified_chapters": len(publication["chapters"]),
                      "bundles": [{k: row[k] for k in ("file", "sha256", "chapters")}
                                  for row in publication["bundles"]]}, indent=2))


if __name__ == "__main__":
    main()
