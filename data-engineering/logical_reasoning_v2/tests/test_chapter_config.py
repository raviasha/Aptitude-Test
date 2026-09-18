from __future__ import annotations

import sys
import unittest
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))


class ChapterConfigTests(unittest.TestCase):
    def _manifest(self, exercise: str, numbers: list[int]) -> dict[str, object]:
        markers = {
            role: {
                str(number): {"segments": [{"page": 22, "left": 95, "top": number, "right": 1435, "bottom": number + 1}]}
                for number in numbers
            }
            for role in ("question", "answer_key", "solution")
        }
        return {
            "schema_version": 1,
            "artifact_type": "logical-reasoning-reviewed-exercise-markers",
            "chapter_id": 101,
            "exercise_id": exercise,
            "internal_numbers": numbers,
            "printed_numbers": list(range(1, len(numbers) + 1)),
            "marker_overrides": markers,
        }

    def test_builds_contiguous_config_and_exercise_mapping(self) -> None:
        from logical_reasoning_v2.chapter_config import build_chapter_config

        payload = build_chapter_config(
            [self._manifest("Exercise 1A", [1, 2]), self._manifest("Exercise 1B", [3])],
            source_pdf="data-engineering/source.pdf",
            source_pdf_sha256="a" * 64,
        )

        self.assertEqual(payload["chapter"], 101)
        self.assertEqual(payload["question_numbers"], [1, 3])
        self.assertEqual(payload["printed_question_count"], 3)
        self.assertEqual(payload["exercise_record_map"]["1"]["exercise_id"], "Exercise 1A")
        self.assertEqual(payload["exercise_record_map"]["3"]["printed_number"], 1)
        self.assertEqual(set(payload["marker_overrides"]["question"]), {"1", "2", "3"})

    def test_refuses_gap_or_mismatched_marker_keys(self) -> None:
        from logical_reasoning_v2.chapter_config import build_chapter_config

        with self.assertRaisesRegex(ValueError, "contiguous"):
            build_chapter_config(
                [self._manifest("Exercise 1A", [1]), self._manifest("Exercise 1B", [3])],
                source_pdf="source.pdf",
                source_pdf_sha256="a" * 64,
            )

        broken = self._manifest("Exercise 1A", [1])
        del broken["marker_overrides"]["solution"]["1"]  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "marker keys"):
            build_chapter_config([broken], source_pdf="source.pdf", source_pdf_sha256="a" * 64)


if __name__ == "__main__":
    unittest.main()
