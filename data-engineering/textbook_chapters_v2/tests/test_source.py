from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import CropBox, SourceImage
from textbook_chapters_v2.source import crop_region, prepare_source_evidence, sha256_path


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _page(self, page_number: int, bands: list[tuple[int, int, str]]) -> SourceImage:
        path = self.root / f"page-{page_number:03d}.png"
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
        with Image.open(output_path) as result:
            self.assertEqual(result.getpixel((50, 40)), (0, 128, 0))

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


if __name__ == "__main__":
    unittest.main()
