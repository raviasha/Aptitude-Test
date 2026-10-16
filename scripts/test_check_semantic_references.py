from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("check_semantic_references.py")


class SemanticReferenceGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.record = {
            "key": "ch419-q0001", "question_text": "Compare A/B with 1/2 and x/y/z.",
            "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"},
            "solution_steps": [r"The printed expression is \frac{1}{2}."],
        }

    def package(self, records, name="bank.zip", manifest=None):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest or {
                "format_version": 3, "question_files": ["questions/ch419.jsonl"],
            }))
            archive.writestr("questions/ch419.jsonl", "\n".join(json.dumps(r) for r in records))
            archive.writestr("metadata/lineage.json", json.dumps({"source_pdf": r"C:\Users\reviewer\book.pdf"}))
            archive.writestr("assets/figure.png", b"asset is outside this semantic-only check")
        return path

    def run_guard(self, *paths, expected=0, report=None):
        command = [sys.executable, str(SCRIPT), *map(str, paths)]
        if report is not None:
            command.extend(["--report", str(report)])
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_blocks_reference_signatures_without_modifying_input(self):
        references = [
            r"C:\Users\reviewer\crop.png", "D:/crops/figure.png", r"\\server\share\figure.png",
            "/home/reviewer/crop.png", "/opt/book/figure.png", "/diagram.png", "~/figures/a.png",
            "../figures/a.png", "assets/figure.png", "file:///C:/crops/a.png",
            "data:image/png;base64,AAAA", "data:text/html,%3Cimg%3E", "data:,figure",
            "sandbox:/mnt/data/a.png", "https://example.test/figure.png",
            "![Figure](figure.png)", "![Figure][original]", "![original]",
            '<IMG SRC="figure.png">', "&lt;img src='figure.png'&gt;",
            "C%3A%5CUsers%5Creviewer%5Cfigure.png",
        ]
        records = [{**self.record, "key": f"q{i}", "question_text": value}
                   for i, value in enumerate(references)]
        path = self.package(records)
        before = path.read_bytes()
        report = self.run_guard(path, expected=21)
        self.assertEqual(report["status"], "blocked")
        result = report["inputs"][0]
        self.assertEqual(result["records_scanned"], len(records))
        self.assertEqual({f["record"] for f in result["findings"]}, {r["key"] for r in records})
        self.assertTrue(all(f["field"] == "question_text" and f["rule"] and f["match"]
                            and f["member"] == "questions/ch419.jsonl" for f in result["findings"]))
        self.assertEqual(path.read_bytes(), before)

    def test_checks_all_semantic_fields_and_nested_alt_text(self):
        record = copy.deepcopy(self.record)
        bad = "![Figure](figure.png)"
        record.update(question_text=bad, question_html=bad, explanation=bad, solution_steps=[bad])
        record["options"]["C"] = bad
        record["display_media"] = {
            "question": {"asset": "assets/q.png", "alt_text": bad},
            "options": {"B": {"asset": "assets/b.png", "alt_text": bad}},
            "solution": [{"asset": "assets/s.png", "alt_text": bad}],
        }
        report = self.run_guard(self.package([record]), expected=21)
        self.assertEqual({f["field"] for f in report["inputs"][0]["findings"]}, {
            "question_text", "question_html", "explanation", "solution_steps[0]", "options.C",
            "display_media.question.alt_text", "display_media.options.B.alt_text",
            "display_media.solution[0].alt_text",
        })

    def test_blocks_bare_media_filenames_in_semantic_fields_only(self):
        references = ["[Source table: question-combined.png]",
                      "Refer to ch416-q0010-solution.png", "[source](figure.png)",
                      "See FIGURE.JPEG.", "Use diagram.svg", "scan%2Ewebp"]
        records = [{**self.record, "key": f"bare{i}", "question_text": value}
                   for i, value in enumerate(references)]
        result = self.run_guard(self.package(records), expected=21)["inputs"][0]
        self.assertEqual({f["record"] for f in result["findings"]},
                         {f"bare{i}" for i in range(len(references))})
        record = copy.deepcopy(self.record)
        record["provenance"] = {"source": "question-combined.png"}
        record["display_media"] = {"question": {
            "asset": "figure.png", "source_path": "ch416-q0010-solution.png",
            "alt_text": "A square divided into four triangles.",
        }}
        self.run_guard(self.package([record], "metadata-only.zip"))
        record["display_media"]["question"]["alt_text"] = "Refer to figure.png"
        result = self.run_guard(self.package([record], "alt.zip"), expected=21)["inputs"][0]
        self.assertEqual({f["field"] for f in result["findings"]}, {"display_media.question.alt_text"})

    def test_prose_and_matrix_separators_are_not_media_or_unc_paths(self):
        clean = [
            "Compare A/B with 1/2 and x/y/z. PNG is an image format; the figure is a square.",
            "The value is 3.14159. Let x.png_value denote a variable; use Fig. 2.",
            r"With the help of (i) position \\(\\begin{matrix}6\\\\|\\\\2—4\\end{matrix}\\). We draw a net for cube.",
            r"With the help of (i) position \(\begin{matrix}6\\|\\2—4\end{matrix}\). We draw a net for cube.",
            r"The matrix is \begin{matrix}a\\b\\c\end{matrix}; compute \frac{1}{2}.",
        ]
        records = [{**self.record, "key": f"clean{i}", "solution_steps": [value]}
                   for i, value in enumerate(clean)]
        self.run_guard(self.package(records))
        unc = [r"\\server\share\figure.png", r"See \\server\share\document for details."]
        records = [{**self.record, "key": f"unc{i}", "question_text": value}
                   for i, value in enumerate(unc)]
        result = self.run_guard(self.package(records, "unc.zip"), expected=21)["inputs"][0]
        self.assertEqual({f["record"] for f in result["findings"] if f["rule"] == "windows_path"},
                         {"unc0", "unc1"})

    def test_matrix_row_before_end_command_is_not_a_unc_share(self):
        matrix = r"\begin{matrix}\frac{1}{2}\\3\end{matrix}"
        record = {**self.record, "solution_steps": [matrix]}
        self.run_guard(self.package([record], "matrix-end.zip"))

        unc_paths = [r"\\server\share\document", r"\\3\share\document",
                     r"\\server\end\document"]
        records = [{**self.record, "key": f"unc-end{i}",
                    "solution_steps": [matrix + " See " + path]}
                   for i, path in enumerate(unc_paths)]
        result = self.run_guard(self.package(records, "matrix-and-unc.zip"),
                                expected=21)["inputs"][0]
        self.assertEqual({f["record"] for f in result["findings"]
                          if f["rule"] == "windows_path"},
                         {r["key"] for r in records})
        self.assertTrue(all("end{matrix}" not in f["match"] for f in result["findings"]))

    def test_ignores_provenance_and_asset_locations_but_not_alt_text(self):
        record = copy.deepcopy(self.record)
        record["source_pdf"] = r"C:\Users\reviewer\source.pdf"
        record["provenance"] = {"path": "/home/reviewer/source.png", "notes": "![source](source.png)"}
        record["display_media"] = {"question": {
            "asset": "assets/figure.png", "source_path": "/tmp/source.png", "alt_text": "Square ABCD.",
        }}
        result = self.run_guard(self.package([record]))["inputs"][0]
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["kind"], "publication-zip-semantics")
        self.assertEqual(result["findings"], [])

    def test_binds_report_to_exact_zip_bytes_and_guard_bytes(self):
        path = self.package([self.record])
        output = self.root / "report.json"
        first = self.run_guard(path, report=output)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), first)
        self.assertEqual(first["inputs"][0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(first["guard_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        self.assertTrue(first["guard_version"])
        with zipfile.ZipFile(path, "a") as archive:
            archive.comment = b"same questions, different sealed bytes"
        second = self.run_guard(path)
        self.assertNotEqual(first["inputs"][0]["sha256"], second["inputs"][0]["sha256"])
        self.assertNotEqual(first["report_sha256"], second["report_sha256"])

    def test_diagnostic_json_and_jsonl_are_labelled_and_scan_candidate_alt_text(self):
        raw = {**self.record, "job_id": "extract-ch419-q0001"}
        del raw["key"]
        raw["options"] = {**raw["options"], "E": None}
        candidate = {**self.record, "chapter": 419, "question_number": 1,
                     "representation": {"media": {"solution": [{
                         "source_path": "/tmp/allowed.png", "alt_text": "file:///bad.png",
                     }]}}}
        paths = [self.root / "raw.json", self.root / "candidates.json", self.root / "records.jsonl"]
        paths[0].write_text(json.dumps(raw), encoding="utf-8")
        paths[1].write_text(json.dumps([candidate]), encoding="utf-8")
        paths[2].write_text(json.dumps(candidate) + "\n", encoding="utf-8")
        report = self.run_guard(*paths, expected=21)
        self.assertEqual([r["status"] for r in report["inputs"]], ["clean", "blocked", "blocked"])
        self.assertTrue(all(r["kind"] == "diagnostic-json" for r in report["inputs"]))
        self.assertEqual(report["inputs"][1]["findings"][0]["field"], "representation.media.solution[0].alt_text")

    def test_malformed_empty_and_ambiguous_packages_fail_closed(self):
        cases = [
            ([], None), ([{}], None), ([self.record, self.record], None),
            ([self.record], {"format_version": 3, "question_files": []}),
            ([self.record], {"format_version": 3, "question_files": ["missing.jsonl"]}),
            ([{**self.record, "options": {}}], None),
            ([{**self.record, "solution_steps": []}], None),
            ([{**self.record, "display_media": {"question": {"alt_text": 42}}}], None),
        ]
        paths = [self.package(records, f"bad{i}.zip", manifest) for i, (records, manifest) in enumerate(cases)]
        duplicate = self.package([self.record], "duplicate.zip")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "a") as archive:
                archive.writestr("questions/ch419.jsonl", json.dumps(self.record))
        paths.append(duplicate)
        report = self.run_guard(*paths, expected=22)
        self.assertEqual(report["status"], "error")
        self.assertTrue(all(item["status"] == "error" and item["errors"] for item in report["inputs"]))

    def test_duplicate_json_keys_unknown_shape_and_missing_files_fail_closed(self):
        raw = self.root / "duplicate.json"
        raw.write_text('{"question_text":"file:///bad.png","question_text":"clean"}', encoding="utf-8")
        unknown = self.root / "unknown.json"
        unknown.write_text('{"records":[]}', encoding="utf-8")
        report = self.run_guard(raw, unknown, self.root / "missing.zip", expected=22)
        self.assertTrue(all(item["status"] == "error" for item in report["inputs"]))

    def test_refuses_to_overwrite_input_or_existing_report(self):
        path = self.package([self.record])
        original = path.read_bytes()
        self.run_guard(path, expected=22, report=path)
        self.assertEqual(path.read_bytes(), original)
        report_path = self.root / "existing.json"
        report_path.write_bytes(b"preserve existing report")
        self.run_guard(path, expected=22, report=report_path)
        self.assertEqual(report_path.read_bytes(), b"preserve existing report")


if __name__ == "__main__":
    unittest.main()
