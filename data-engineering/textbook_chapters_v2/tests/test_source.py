from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import CropBox, SourceImage
from textbook_chapters_v2.source import crop_region, prepare_source_evidence, render_page, sha256_path
from textbook_chapters_v2.vision import create_extraction_job


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _page(self, page_number: int, bands: list[tuple[int, int, str]], name: str | None = None) -> SourceImage:
        path = self.root / (name or f"page-{page_number:03d}.png")
        image = Image.new("RGB", (100, 200), "white")
        for top, bottom, colour in bands:
            image.paste(colour, (0, top, 100, bottom))
        image.save(path, format="PNG", optimize=False, compress_level=9)
        return SourceImage(path=path, page_number=page_number, dpi=180, sha256=sha256_path(path))

    def test_crop_region_preserves_real_pixels_and_provenance(self) -> None:
        page = self._page(38, [(0, 40, "red"), (40, 120, "green"), (120, 200, "blue")])
        output_path = self.root / "crop.png"

        crop = crop_region(page, CropBox(left=0, top=40, right=100, bottom=120), output_path)

        self.assertEqual((crop.width, crop.height), (100, 80))
        self.assertEqual(crop.page_number, 38)
        self.assertEqual(crop.box, CropBox(0, 40, 100, 120))
        self.assertEqual(crop.sha256, sha256_path(output_path))
        self.assertEqual(crop.source_image_sha256, page.sha256)
        self.assertEqual(crop.source_dpi, 180)
        with Image.open(output_path) as result:
            self.assertEqual(result.getpixel((50, 40)), (0, 128, 0))

    def test_prepare_source_evidence_can_scope_render_crop_and_artifact_work_to_exact_questions(self) -> None:
        pages = {
            page: self._page(page, [(0, 200, "red")], name=f"fixture-{page}.png")
            for page in range(1, 10)
        }
        config = ChapterConfig.from_dict({
            "chapter": 14,
            "bank_name": "scoped source",
            "question_pages": [1, 3],
            "answer_pages": [4, 6],
            "solution_pages": [7, 9],
            "question_numbers": [1, 3],
            "marker_overrides": {
                role: {
                    str(number): {"segments": [{"page": offset + number, "left": 0, "top": 0, "right": 100, "bottom": 100}]}
                    for number in range(1, 4)
                }
                for role, offset in (("question", 0), ("answer_key", 3), ("solution", 6))
            },
        })
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"scoped reviewed source")
        rendered: list[int] = []

        def render_fixture(_pdf: Path, page: int, _dpi: int, _output: Path) -> SourceImage:
            rendered.append(page)
            return pages[page]

        work = self.root / "scoped-work"
        with patch("textbook_chapters_v2.source.render_page", side_effect=render_fixture):
            evidence = prepare_source_evidence(
                config, pdf_path, work, question_numbers=(2,)
            )

        self.assertEqual([item.question_number for item in evidence], [2])
        self.assertEqual(rendered, [2, 5, 8])
        self.assertEqual(
            [path.name for path in (work / "source-evidence").glob("*.json")],
            ["ch014-q0002.json"],
        )
        crop_names = [path.name for path in (work / "crops").glob("*.png")]
        self.assertTrue(crop_names)
        self.assertTrue(all("q0002" in name for name in crop_names))

    def test_sha256_path_retries_a_transient_permission_denied_from_a_synced_worktree(self) -> None:
        path = self.root / "source.bin"
        path.write_bytes(b"stable source bytes")
        real_open = Path.open
        attempts = 0

        def transient_open(candidate: Path, *args: object, **kwargs: object):
            nonlocal attempts
            if candidate == path and attempts == 0:
                attempts += 1
                raise PermissionError(13, "transient sync lock", str(candidate))
            return real_open(candidate, *args, **kwargs)

        with patch("pathlib.Path.open", new=transient_open):
            digest = sha256_path(path)

        self.assertEqual(digest, hashlib.sha256(b"stable source bytes").hexdigest())
        self.assertEqual(attempts, 1)

    def test_render_page_rejects_a_stale_output_when_renderer_writes_nothing(self) -> None:
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"synthetic source")
        output_path = self.root / "page.png"
        Image.new("RGB", (100, 100), "red").save(output_path)
        fake_renderer = self.root / "fake-pdftoppm.cmd"
        fake_renderer.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

        with patch.dict(os.environ, {"PDFTOPPM": str(fake_renderer)}):
            with self.assertRaisesRegex(RuntimeError, "did not create"):
                render_page(pdf_path, 1, 180, output_path)

        with Image.open(output_path) as stale:
            self.assertEqual(stale.getpixel((50, 50)), (255, 0, 0))

    def test_reviewed_adjacent_markers_bound_crops_and_keep_multi_page_solutions_ordered(self) -> None:
        pages = {
            1: self._page(1, [(0, 100, "red"), (100, 200, "green")]),
            3: self._page(3, [(0, 100, "yellow"), (100, 200, "purple")]),
            4: self._page(4, [(0, 200, "blue")]),
            5: self._page(5, [(0, 30, "orange"), (30, 200, "black")]),
        }
        config = ChapterConfig.from_dict(
            {
                "chapter": 7,
                "bank_name": "synthetic",
                "question_pages": [1, 1],
                "answer_pages": [3, 3],
                "solution_pages": [4, 5],
                "question_numbers": [1, 2],
                "boundary_reviews": {
                    "question:1": {
                        "first_visible_content": "first controlled band",
                        "last_visible_content": "last controlled band",
                        "segments": [{"page": 1, "left": 0, "top": 60, "right": 50, "bottom": 120}],
                        "crop_sha256s": ["reviewed-at-source-stage"],
                    }
                },
                "marker_overrides": {
                    "question": {"1": {"page": 1, "top": 0}, "2": {"page": 1, "top": 100}},
                    "answer_key": {"1": {"page": 3, "top": 0}, "2": {"page": 3, "top": 100}},
                    "solution": {"1": {"page": 4, "top": 0}, "2": {"page": 5, "top": 30}},
                },
            }
        )
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"reviewed source bytes")

        def render_fixture(_pdf_path: Path, page_number: int, _dpi: int, _output_path: Path) -> SourceImage:
            return pages[page_number]

        with patch("textbook_chapters_v2.source.render_page", side_effect=render_fixture):
            evidence = prepare_source_evidence(config, pdf_path, self.root / "work")

        first, second = evidence
        self.assertEqual(first.question_crops[0].box, CropBox(0, 0, 100, 100))
        self.assertEqual(second.question_crops[0].box, CropBox(0, 100, 100, 200))
        self.assertEqual(first.answer_key_crops[0].box, CropBox(0, 0, 100, 100))
        self.assertEqual([(crop.page_number, crop.box) for crop in first.solution_crops], [
            (4, CropBox(0, 0, 100, 200)),
            (5, CropBox(0, 0, 100, 30)),
        ])
        self.assertEqual([crop.page_number for crop in first.solution_crops], [4, 5])
        self.assertTrue(all(crop.path.is_file() for crop in first.solution_crops))
        self.assertEqual(first.source_pdf_sha256, hashlib.sha256(pdf_path.read_bytes()).hexdigest())
        self.assertEqual(
            first.boundary_review["question:1"]["first_visible_content"],
            "first controlled band",
        )

    def test_explicit_bottom_ends_a_multi_page_crop_before_the_next_marker_page(self) -> None:
        pages = {
            1: self._page(1, [(0, 100, "red"), (100, 200, "green")]),
            3: self._page(3, [(0, 100, "yellow"), (100, 200, "purple")]),
            4: self._page(4, [(0, 200, "blue")]),
            5: self._page(5, [(0, 30, "orange"), (30, 200, "black")]),
        }
        config = ChapterConfig.from_dict(
            {
                "chapter": 8,
                "bank_name": "explicit-bottom",
                "question_pages": [1, 1],
                "answer_pages": [3, 3],
                "solution_pages": [4, 5],
                "question_numbers": [1, 2],
                "marker_overrides": {
                    "question": {"1": {"page": 1, "top": 0}, "2": {"page": 1, "top": 100}},
                    "answer_key": {"1": {"page": 3, "top": 0}, "2": {"page": 3, "top": 100}},
                    "solution": {"1": {"page": 4, "top": 0, "bottom": 100}, "2": {"page": 5, "top": 30}},
                },
            }
        )
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"reviewed source bytes")

        with patch("textbook_chapters_v2.source.render_page", side_effect=lambda _pdf, page, _dpi, _output: pages[page]):
            evidence = prepare_source_evidence(config, pdf_path, self.root / "work")

        self.assertEqual([(crop.page_number, crop.box) for crop in evidence[0].solution_crops], [
            (4, CropBox(0, 0, 100, 100)),
        ])

    def test_explicit_segments_support_grid_order_and_reviewed_missing_role_evidence(self) -> None:
        pages = {
            1: self._page(1, [(0, 60, "red"), (60, 200, "green")]),
            2: self._page(2, [(0, 60, "yellow"), (60, 200, "purple")]),
            3: self._page(3, [(0, 60, "orange"), (60, 200, "blue")]),
        }
        config = ChapterConfig.from_dict(
            {
                "chapter": 11,
                "bank_name": "explicit-grid",
                "question_pages": [1, 1],
                "answer_pages": [2, 2],
                "solution_pages": [3, 3],
                "question_numbers": [1, 2],
                "known_source_issues": {
                    "2": {
                        "status": "missing_solution",
                        "reason": "textbook_solution_missing",
                        "detail": "The source prints no numbered solution.",
                        "requires_reviewed_rejection": True,
                    }
                },
                "marker_overrides": {
                    "question": {
                        "1": {"segments": [{"page": 1, "left": 0, "top": 60, "right": 50, "bottom": 120}]},
                        "2": {"segments": [{"page": 1, "left": 50, "top": 0, "right": 100, "bottom": 60}]},
                    },
                    "answer_key": {
                        "1": {"segments": [{"page": 2, "left": 0, "top": 0, "right": 50, "bottom": 60}]},
                        "2": {"segments": [{"page": 2, "left": 50, "top": 0, "right": 100, "bottom": 60}]},
                    },
                    "solution": {
                        "1": {
                            "segments": [
                                {"page": 3, "left": 0, "top": 60, "right": 50, "bottom": 200},
                                {"page": 3, "left": 50, "top": 0, "right": 100, "bottom": 60},
                            ]
                        },
                        "2": {"missing": True, "reason": "The source prints no numbered solution."},
                    },
                },
            }
        )
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"reviewed source bytes")

        with patch("textbook_chapters_v2.source.render_page", side_effect=lambda _pdf, page, _dpi, _output: pages[page]):
            evidence = prepare_source_evidence(config, pdf_path, self.root / "work")

        self.assertEqual(evidence[0].question_crops[0].box, CropBox(0, 60, 50, 120))
        self.assertEqual(evidence[1].question_crops[0].box, CropBox(50, 0, 100, 60))
        self.assertEqual(
            [(crop.page_number, crop.box) for crop in evidence[0].solution_crops],
            [(3, CropBox(0, 60, 50, 200)), (3, CropBox(50, 0, 100, 60))],
        )
        self.assertEqual(evidence[1].solution_crops, ())
        self.assertEqual(evidence[1].source_status, "missing_solution")
        self.assertEqual(
            evidence[1].source_reasons,
            ("textbook_solution_missing: The source prints no numbered solution.",),
        )
        self.assertTrue(evidence[1].requires_reviewed_rejection)
        manifest = json.loads(
            (self.root / "work" / "source-evidence" / "ch011-q0002.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["payload"]["source_status"], "missing_solution")
        self.assertEqual(manifest["payload"]["source_reasons"], list(evidence[1].source_reasons))

    def test_named_shared_question_context_is_authorized_for_each_target_job_only(self) -> None:
        pages = {
            1: self._page(
                1,
                [(0, 40, "red"), (40, 80, "yellow"), (80, 120, "green"), (120, 160, "blue")],
            ),
            2: self._page(2, [(0, 200, "orange")]),
            3: self._page(3, [(0, 200, "purple")]),
        }
        config = ChapterConfig.from_dict(
            {
                "chapter": 13,
                "bank_name": "shared-directions",
                "question_pages": [1, 1],
                "answer_pages": [2, 2],
                "solution_pages": [3, 3],
                "question_numbers": [1, 3],
                "shared_contexts": {
                    "question": {
                        "questions-2-3": {
                            "question_numbers": [2, 3],
                            "segments": [
                                {"page": 1, "left": 0, "top": 40, "right": 100, "bottom": 80}
                            ],
                        }
                    }
                },
                "marker_overrides": {
                    "question": {
                        "1": {"segments": [{"page": 1, "left": 0, "top": 0, "right": 100, "bottom": 40}]},
                        "2": {"segments": [{"page": 1, "left": 0, "top": 80, "right": 100, "bottom": 120}]},
                        "3": {"segments": [{"page": 1, "left": 0, "top": 120, "right": 100, "bottom": 160}]},
                    },
                    "answer_key": {
                        str(number): {
                            "segments": [{"page": 2, "left": 0, "top": (number - 1) * 40, "right": 100, "bottom": number * 40}]
                        }
                        for number in range(1, 4)
                    },
                    "solution": {
                        str(number): {
                            "segments": [{"page": 3, "left": 0, "top": (number - 1) * 40, "right": 100, "bottom": number * 40}]
                        }
                        for number in range(1, 4)
                    },
                },
            }
        )
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"reviewed source bytes")

        with patch("textbook_chapters_v2.source.render_page", side_effect=lambda _pdf, page, _dpi, _output: pages[page]):
            evidence = prepare_source_evidence(config, pdf_path, self.root / "work")

        self.assertEqual(
            [(crop.box, crop.context_id) for crop in evidence[0].question_crops],
            [(CropBox(0, 0, 100, 40), "")],
        )
        for record, own_box in (
            (evidence[1], CropBox(0, 80, 100, 120)),
            (evidence[2], CropBox(0, 120, 100, 160)),
        ):
            self.assertEqual(
                [(crop.box, crop.context_id) for crop in record.question_crops],
                [(CropBox(0, 40, 100, 80), "questions-2-3"), (own_box, "")],
            )
            job = create_extraction_job(record, self.root / "jobs" / f"q{record.question_number}.json")
            question_sources = [source for source in job.sources if source["role"] == "question"]
            self.assertEqual(question_sources[0]["context_id"], "questions-2-3")
            self.assertNotIn("context_id", question_sources[1])

        with_context = create_extraction_job(evidence[1], self.root / "jobs" / "q2.json")
        same_record_without_context = create_extraction_job(
            replace(evidence[1], question_crops=(evidence[1].question_crops[1],)),
            self.root / "jobs" / "q2-without-context.json",
        )
        self.assertNotEqual(with_context.fingerprint, same_record_without_context.fingerprint)

    def test_prepare_rejects_a_source_pdf_that_does_not_match_the_pinned_hash(self) -> None:
        page = self._page(1, [(0, 100, "red")])
        config = ChapterConfig.from_dict(
            {
                "chapter": 12,
                "bank_name": "hash-pinned",
                "question_pages": [1, 1],
                "answer_pages": [1, 1],
                "solution_pages": [1, 1],
                "question_numbers": [1, 1],
                "source_pdf_sha256": "0" * 64,
                "marker_overrides": {
                    role: {"1": {"page": 1, "top": 0, "bottom": 100}}
                    for role in ("question", "answer_key", "solution")
                },
            }
        )
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"different source bytes")

        with patch("textbook_chapters_v2.source.render_page", return_value=page):
            with self.assertRaisesRegex(ValueError, "does not match configured SHA-256"):
                prepare_source_evidence(config, pdf_path, self.root / "work")

    def test_excluded_marker_bounds_the_prior_record_without_creating_excluded_artifacts(self) -> None:
        page = self._page(1, [(0, 100, "red"), (100, 200, "green")])
        config = ChapterConfig.from_dict(
            {
                "chapter": 9,
                "bank_name": "exclusions",
                "question_pages": [1, 1],
                "answer_pages": [1, 1],
                "solution_pages": [1, 1],
                "question_numbers": [1, 2],
                "intentional_exclusions": [2],
                "marker_overrides": {
                    role: {"1": {"page": 1, "top": 0}, "2": {"page": 1, "top": 100}}
                    for role in ("question", "answer_key", "solution")
                },
            }
        )
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"reviewed source bytes")
        work_dir = self.root / "work"

        with patch("textbook_chapters_v2.source.render_page", return_value=page):
            evidence = prepare_source_evidence(config, pdf_path, work_dir)

        self.assertEqual([record.question_number for record in evidence], [1])
        self.assertEqual(evidence[0].question_crops[0].box, CropBox(0, 0, 100, 100))
        self.assertEqual(list((work_dir / "crops").glob("*q0002*")), [])

    def test_evidence_fingerprint_and_manifest_bind_source_render_hash_and_dpi(self) -> None:
        first_page = self._page(1, [(0, 100, "red"), (100, 200, "blue")], "first.png")
        second_page = self._page(1, [(0, 100, "red"), (100, 200, "green")], "second.png")
        config = ChapterConfig.from_dict(
            {
                "chapter": 10,
                "bank_name": "provenance",
                "question_pages": [1, 1],
                "answer_pages": [1, 1],
                "solution_pages": [1, 1],
                "question_numbers": [1, 1],
                "marker_overrides": {
                    role: {"1": {"page": 1, "top": 0, "bottom": 100}}
                    for role in ("question", "answer_key", "solution")
                },
            }
        )
        pdf_path = self.root / "source.pdf"
        pdf_path.write_bytes(b"reviewed source bytes")

        with patch("textbook_chapters_v2.source.render_page", return_value=first_page):
            first = prepare_source_evidence(config, pdf_path, self.root / "first-work")[0]
        with patch("textbook_chapters_v2.source.render_page", return_value=second_page):
            second = prepare_source_evidence(config, pdf_path, self.root / "second-work")[0]

        self.assertEqual(first.question_crops[0].source_image_sha256, first_page.sha256)
        self.assertEqual(first.question_crops[0].source_dpi, first_page.dpi)
        self.assertEqual(first.question_crops[0].sha256, second.question_crops[0].sha256)
        self.assertNotEqual(first.dependency_fingerprint, second.dependency_fingerprint)
        manifest = json.loads((self.root / "first-work" / "source-evidence" / "ch010-q0001.json").read_text(encoding="utf-8"))
        persisted = manifest["payload"]["question_crops"][0]
        self.assertEqual(persisted["source_image_sha256"], first_page.sha256)
        self.assertEqual(persisted["source_dpi"], first_page.dpi)


if __name__ == "__main__":
    unittest.main()
