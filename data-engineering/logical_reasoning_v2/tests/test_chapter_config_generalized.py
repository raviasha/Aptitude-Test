from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "data-engineering"))

from logical_reasoning_v2.chapter_config import build_chapter_config


def manifest(chapter=101, exercise="Exercise 1A", count=1):
    page = {101: 22, 102: 116, 103: 160, 104: 190, 105: 241, 121: 582, 130: 741}[chapter]
    return {
        "schema_version": 1,
        "artifact_type": "logical-reasoning-reviewed-exercise-markers",
        "chapter_id": chapter,
        "exercise_id": exercise,
        "internal_numbers": list(range(1, count + 1)),
        "printed_numbers": list(range(1, count + 1)),
        "marker_overrides": {
            role: {str(n): {"segments": [{"page": page, "left": 95, "top": 300,
                                       "right": 1435, "bottom": 450}]}
                   for n in range(1, count + 1)}
            for role in ("question", "answer_key", "solution")
        },
    }


def build(items, **kwargs):
    return build_chapter_config(items, source_pdf="source.pdf", source_pdf_sha256="A" * 64, **kwargs)


class ChapterConfigTests(unittest.TestCase):
    def test_every_record_and_role_requires_nonempty_bounded_segments(self):
        good = {"page": 22, "left": 95, "top": 300, "right": 1435, "bottom": 450}
        invalid = [None, {}, {"page": 22, "top": 300}, {"segments": []},
                   {"segments": None}, {"segments": "bad"}, {"segments": [None]}]
        for field in good:
            invalid.append({"segments": [{key: val for key, val in good.items() if key != field}]})
            for value in (None, True, "22", 22.5):
                invalid.append({"segments": [dict(good, **{field: value})]})
        for update in ({"left": -1}, {"top": -1}, {"right": 95}, {"right": 94},
                       {"bottom": 300}, {"bottom": 299}):
            invalid.append({"segments": [dict(good, **update)]})
        # A valid first segment must not hide a malformed continuation.
        invalid.append({"segments": [good, {"page": 23, "top": 300}]})
        for role in ("question", "answer_key", "solution"):
            for number in ("1", "2"):
                for marker in invalid:
                    with self.subTest(role=role, number=number, marker=marker):
                        item = manifest(count=2)
                        item["marker_overrides"][role][number] = marker
                        with self.assertRaisesRegex(ValueError, "segment"):
                            build([item])

    def test_segments_must_stay_within_selected_chapter_pages(self):
        for chapter, start, end in ((101, 17, 110), (102, 111, 154)):
            for role in ("question", "answer_key", "solution"):
                for page in (start - 1, end + 1, 0, -1):
                    with self.subTest(chapter=chapter, role=role, page=page):
                        item = manifest(chapter)
                        segments = item["marker_overrides"][role]["1"]["segments"]
                        segments.append(dict(segments[0], page=page))
                        with self.assertRaisesRegex(ValueError, "page"):
                            build([item], chapter_id=chapter)

    def test_bounded_multisegment_evidence_at_chapter_edges_is_preserved(self):
        item = manifest(102)
        for role in ("question", "answer_key", "solution"):
            item["marker_overrides"][role]["1"]["segments"] = [
                {"page": 111, "left": 0, "top": 0, "right": 700, "bottom": 300},
                {"page": 154, "left": 775, "top": 300, "right": 1435, "bottom": 600},
            ]
        self.assertEqual(build([item], chapter_id=102)["marker_overrides"], item["marker_overrides"])

    def test_101_excluded_exercise_variants_are_rejected_before_marker_validation(self):
        for exercise in ("Exercise 1J", "exercise 1j", " EXERCISE  1 J ",
                         "Exercise-1J", "exercise_1j", "1J", "1 j", "Exercise 1(J)"):
            with self.subTest(exercise=exercise):
                item = manifest(exercise=exercise)
                item["marker_overrides"] = {}
                with self.assertRaisesRegex(ValueError, "Exercise 1J.*excluded"):
                    build([item])

    def test_1j_exclusion_does_not_apply_to_other_chapters_or_other_exercises(self):
        for chapter, exercise in ((102, "Exercise 1J"), (101, "Exercise 1JA"), (101, "Exercise 11J")):
            with self.subTest(chapter=chapter, exercise=exercise):
                result = build([manifest(chapter, exercise)], chapter_id=chapter)
                self.assertEqual(result["exercise_record_map"]["1"]["exercise_id"], exercise)

    def test_metadata_for_first_five_and_other_sections(self):
        cases = [(101, "Analogy", 17, 110, "s01", 1, "analogy"),
                 (102, "Classification", 111, 154, "s01", 2, "classification"),
                 (103, "Series Completion", 155, 184, "s01", 3, "series_completion"),
                 (104, "Coding-Decoding", 185, 235, "s01", 4, "coding_decoding"),
                 (105, "Blood Relations", 236, 257, "s01", 5, "blood_relations"),
                 (121, "Logic", 577, 605, "s02", 1, "logic"),
                 (130, "Series", 736, 870, "s03", 1, "series")]
        for chapter, title, start, end, section, number, slug in cases:
            with self.subTest(chapter=chapter):
                result = build([manifest(chapter)], chapter_id=chapter)
                self.assertEqual(result["chapter"], chapter)
                self.assertTrue(result["chapter_name"].endswith(": " + title))
                self.assertEqual(result["bank_name"], f"A Modern Approach to Logical Reasoning — Chapter {number}: {title} (vision-verified)")
                for role in ("question", "answer", "solution"):
                    self.assertEqual(result[role + "_pages"], [start, end])
                self.assertEqual(Path(result["candidate_path"]).name,
                                 f"logical_reasoning_{section}_ch{number:02d}_{slug}_v2_candidate.zip")
                if chapter != 101:
                    self.assertEqual(result["exercise_exclusions"], {})
                    self.assertEqual(result["known_source_issues"], {})

    def test_default_101_paths_and_exclusion_are_compatible(self):
        result = build([manifest()])
        self.assertEqual(result["work_root"], "tmp/logical-reasoning-v2")
        self.assertEqual(result["candidate_path"], "question-banks/candidates/logical_reasoning_s01_ch01_analogy_v2_candidate.zip")
        self.assertEqual(result["published_path"], "question-banks/logical_reasoning_s01_ch01_analogy_all_vision_text_only.zip")
        self.assertIn("Exercise 1J", result["exercise_exclusions"])

    def test_explicit_work_root_isolates_candidate_per_chapter(self):
        for chapter in (101, 102):
            result = build([manifest(chapter)], chapter_id=chapter, work_root=Path("tmp/recovery"))
            self.assertEqual(result["work_root"], "tmp/recovery")
            self.assertEqual(Path(result["candidate_path"]).parent, Path(f"tmp/recovery/chapter-{chapter}"))

    def test_rejects_unknown_chapter_and_mismatched_manifests(self):
        with self.assertRaisesRegex(ValueError, "[Uu]nknown"):
            build([manifest()], chapter_id=999)
        with self.assertRaisesRegex(ValueError, "Chapter 102"):
            build([manifest(102), manifest(101)], chapter_id=102)

    def test_issue_813_requires_exact_chapter_exercise_and_printed_number(self):
        for chapter, exercise, printed, expected in [
            (101, "Exercise 1M", 85, True), (101, "Exercise 1M", 86, False),
            (101, "Exercise 1L", 85, False), (102, "Exercise 1M", 85, False),
        ]:
            with self.subTest(chapter=chapter, exercise=exercise, printed=printed):
                item = manifest(chapter, exercise, 813)
                item["printed_numbers"][-1] = printed
                self.assertEqual("813" in build([item], chapter_id=chapter)["known_source_issues"], expected)

    def test_absent_record_813_has_no_issue(self):
        self.assertEqual(build([manifest()])["known_source_issues"], {})

    def test_cli_shared_contexts_input_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            reviews = base / "reviews"
            reviews.mkdir()
            (reviews / "exercise-1A.json").write_text(json.dumps(manifest()), encoding="utf-8")
            source = base / "source.pdf"
            source.write_bytes(b"test source")
            contexts_path = base / "contexts.json"
            output = base / "config.json"
            command = [sys.executable, "-B", str(ROOT / "scripts/build_logical_reasoning_chapter_config.py"),
                       "--manifests", str(reviews), "--source-pdf", str(source), "--output", str(output),
                       "--shared-contexts", str(contexts_path)]
            contexts = {"question": {"exercise-1a-1-1": {"question_numbers": [1], "segments": [
                {"page": 22, "left": 95, "top": 100, "right": 1435, "bottom": 200}]}}}
            contexts_path.write_text(json.dumps(contexts), encoding="utf-8")
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["shared_contexts"], contexts)
            original = output.read_bytes()
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("--force", refused.stderr)
            self.assertEqual(output.read_bytes(), original)
            contexts_path.write_text("{}", encoding="utf-8")
            forced = subprocess.run(command + ["--force"], capture_output=True, text=True)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["shared_contexts"], {})

    def test_cli_rejects_invalid_shared_contexts_without_writing_even_with_force(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            reviews = base / "reviews"
            reviews.mkdir()
            (reviews / "exercise-1A.json").write_text(json.dumps(manifest()), encoding="utf-8")
            source = base / "source.pdf"
            source.write_bytes(b"test source")
            contexts = base / "contexts.json"
            output = base / "config.json"
            command = [sys.executable, "-B", str(ROOT / "scripts/build_logical_reasoning_chapter_config.py"),
                       "--manifests", str(reviews), "--source-pdf", str(source), "--output", str(output),
                       "--shared-contexts", str(contexts)]
            invalid = ['{', '[]', 'null', '"text"', '42', 'true',
                       '{"question": {}, "question": {}}',
                       '{"question": {"group": {"segments": [], "segments": []}}}',
                       '{"question": NaN}', '{"question": Infinity}']
            segment = {"page": 22, "left": 95, "top": 100, "right": 1435, "bottom": 200}
            group = {"question_numbers": [1], "segments": [segment]}
            invalid += [json.dumps(value) for value in (
                {"questions": {"g": group}}, {"question": []}, {"question": {"g": []}},
                {"question": {"g": {"segments": [segment]}}},
                {"question": {"g": {"question_numbers": [1]}}},
                {"question": {"g": dict(group, question_numbers=[True])}},
                {"question": {"g": dict(group, question_numbers=[2])}},
                {"question": {"g": dict(group, question_numbers=[1, 1])}},
                {"question": {"g": dict(group, segments=[])}},
                {"question": {"g": dict(group, segments=[{"page": 22, "top": 100}])}},
                {"question": {"g": dict(group, segments=[dict(segment, left=-1)])}},
                {"question": {"g": dict(group, segments=[dict(segment, bottom=100)])}},
                {"question": {"g": dict(group, segments=[dict(segment, page=111)])}},
                {"question": {"g": dict(group, segments=[dict(segment, left=True)])}},
                {"question": {"g": group, "h": group}},
            )]
            for raw in invalid:
                for exists in (False, True):
                    with self.subTest(raw=raw, existing_output=exists):
                        contexts.write_text(raw, encoding="utf-8")
                        if exists:
                            output.write_bytes(b"preserve me")
                        else:
                            output.unlink(missing_ok=True)
                        result = subprocess.run(command + (["--force"] if exists else []),
                                                capture_output=True, text=True)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("Invalid shared-contexts", result.stderr)
                        if exists:
                            self.assertEqual(output.read_bytes(), b"preserve me")
                        else:
                            self.assertFalse(output.exists())

    def test_cli_chapter_root_and_overwrite_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            reviews = base / "reviews"
            reviews.mkdir()
            (reviews / "exercise-2A.json").write_text(json.dumps(manifest(102)), encoding="utf-8")
            source = base / "source.pdf"
            source.write_bytes(b"test source")
            output = base / "config.json"
            command = [sys.executable, "-B", str(ROOT / "scripts/build_logical_reasoning_chapter_config.py"),
                       "--manifests", str(reviews), "--source-pdf", str(source), "--output", str(output),
                       "--chapter-id", "102", "--work-root", str(base / "recovery")]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["chapter"], 102)
            self.assertEqual(payload["shared_contexts"], {})
            self.assertEqual(Path(payload["candidate_path"]).parent, base / "recovery/chapter-102")
            output.write_text("preserve me", encoding="utf-8")
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("--force", refused.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve me")
            forced = subprocess.run(command + ["--force"], capture_output=True, text=True)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["chapter"], 102)


if __name__ == "__main__":
    unittest.main()
