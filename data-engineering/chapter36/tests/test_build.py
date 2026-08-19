from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILD_PATH = PROJECT_ROOT / "data-engineering" / "chapter36" / "build.py"
SPEC = importlib.util.spec_from_file_location("chapter36_build", BUILD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {BUILD_PATH}")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class Chapter36BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp_directory.name) / "chapter36.zip"
        cls.summary = BUILD.build_package(
            source_dir=BUILD.DEFAULT_SOURCE_DIR,
            questions_path=BUILD.DEFAULT_QUESTIONS,
            analysis_path=BUILD.DEFAULT_ANALYSIS,
            solutions_path=BUILD.DEFAULT_TEXTBOOK_SOLUTIONS,
            output_path=cls.output,
        )
        cls.package_bytes = cls.output.read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_directory.cleanup()

    def test_audited_totals(self) -> None:
        self.assertEqual(
            self.summary,
            {"source_records": 65, "published_questions": 60, "rejected_questions": 5, "stimuli": 12},
        )

    def test_every_published_question_has_a_verified_stimulus(self) -> None:
        with zipfile.ZipFile(io.BytesIO(self.package_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            questions = [
                json.loads(line)
                for line in archive.read("questions/chapter-036.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
        stimuli = {stimulus["id"]: stimulus for stimulus in manifest["stimuli"]}
        self.assertEqual(len(questions), 60)
        for question in questions:
            stimulus = stimuli[question["stimulus_id"]]
            self.assertIn(question["key"], stimulus["question_keys"])
            self.assertEqual(
                question["image_association"]["visual_source_pages"], stimulus["source_pages"]
            )
        statuses = [question["image_association"]["status"] for question in questions]
        self.assertEqual(statuses.count("same_page_verified"), 40)
        self.assertEqual(statuses.count("cross_page_verified"), 20)

    def test_rejections_are_explicit_and_lossless(self) -> None:
        with zipfile.ZipFile(io.BytesIO(self.package_bytes)) as archive:
            rejected = [
                json.loads(line)
                for line in archive.read("metadata/rejected-questions.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
        reasons = [record["reason"] for record in rejected]
        self.assertEqual(reasons.count("source_question_not_found"), 5)

    def test_every_answer_and_solution_has_textbook_provenance(self) -> None:
        source_document = BUILD.load_json(BUILD.DEFAULT_TEXTBOOK_SOLUTIONS)
        source_records = {
            (record["exercise"], record["question_number"]): record
            for record in source_document["records"]
        }
        with zipfile.ZipFile(io.BytesIO(self.package_bytes)) as archive:
            questions = [
                json.loads(line)
                for line in archive.read("questions/chapter-036.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(len(source_records), 60)
        for question in questions:
            identity = (question["source_exercise"], question["source_question_number"])
            source = source_records[identity]
            self.assertEqual(question["correct_answer"], source["correct_answer"])
            self.assertEqual(question["solution_steps"], source["solution_steps"])
            self.assertEqual(question["explanation"], source["solution_steps"][-1])
            self.assertEqual(question["option_explanations"], {})
            self.assertEqual(question["answer_source"]["source_page"], source["answer_key_page"])
            self.assertEqual(question["solution_source"]["source_pages"], source["solution_pages"])

        self.assertEqual(
            "".join(source_records[("I", number)]["correct_answer"] for number in range(1, 26)),
            "BEDAECBDEBACDBDCBADECABDE",
        )
        self.assertEqual(
            "".join(source_records[("II", number)]["correct_answer"] for number in range(1, 36)),
            "AECCDDBCECECBBEBAECCDDBDADECBBBBDDB",
        )

    def test_reported_company_a_question_matches_the_textbook(self) -> None:
        with zipfile.ZipFile(io.BytesIO(self.package_bytes)) as archive:
            questions = {
                record["key"]: record
                for record in (
                    json.loads(line)
                    for line in archive.read("questions/chapter-036.jsonl").decode("utf-8").splitlines()
                    if line.strip()
                )
            }
        question = questions["ch36-ex2-q0006"]
        self.assertEqual(question["correct_answer"], "D")
        self.assertEqual(question["answer_source"]["source_page"], 905)
        self.assertEqual(question["solution_source"]["source_pages"], [905])
        self.assertIn("Production of Company A in 2009 = 550 tonnes", question["solution_steps"][0])
        self.assertIn("approximately 27%", question["solution_steps"][-1])

    def test_crop_boxes_stop_before_question_content(self) -> None:
        analysis = BUILD.load_json(BUILD.DEFAULT_ANALYSIS)
        for visual in analysis["visuals"]:
            for region in BUILD.source_regions(visual):
                self.assertLessEqual(region["crop_box"][3], region["question_content_starts_at_y"])

    def test_package_matches_the_application_contract(self) -> None:
        with zipfile.ZipFile(io.BytesIO(self.package_bytes)) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            questions = [
                json.loads(line)
                for line in archive.read(manifest["question_files"][0]).decode("utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(manifest["format_version"], 2)
        self.assertTrue(manifest["bank_name"])
        self.assertEqual(len(questions), 60)
        stimulus_ids = {stimulus["id"] for stimulus in manifest["stimuli"]}
        self.assertEqual(len(stimulus_ids), 12)
        self.assertTrue(all(stimulus["file"] in names for stimulus in manifest["stimuli"]))
        self.assertTrue(all(question["stimulus_id"] in stimulus_ids for question in questions))

    def test_pipeline_is_not_an_application_dependency(self) -> None:
        app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        app_requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("data-engineering", app_source)
        self.assertNotIn("Pillow", app_requirements)


if __name__ == "__main__":
    unittest.main()
