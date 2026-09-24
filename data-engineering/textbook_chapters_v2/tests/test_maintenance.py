from __future__ import annotations

import sys
import unittest
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from textbook_chapters_v2.maintenance import (
    affected_records,
    affected_records_for_registry_change,
    baseline_acceptance,
    stages_for_failure,
)


class MaintenanceSelectionTests(unittest.TestCase):
    def test_shared_dependency_selection_finds_every_consumer(self) -> None:
        registry = {
            "ch36-q0001": {"dependency_tags": ["crop:table-a"]},
            "ch36-q0002": {"dependency_tags": ["crop:table-a"]},
            "ch36-q0003": {"dependency_tags": ["crop:table-b"]},
        }
        self.assertEqual(
            affected_records(registry, {"crop:table-a"}),
            {"ch36-q0001", "ch36-q0002"},
        )

    def test_registry_change_selects_old_and_new_consumers(self) -> None:
        previous = {"ch36-q0001": {"dependency_tags": ["crop:old"]}}
        current = {"ch36-q0002": {"dependency_tags": ["crop:old"]}}
        self.assertEqual(
            affected_records_for_registry_change(previous, current, {"crop:old"}),
            {"ch36-q0001", "ch36-q0002"},
        )

    def test_frontend_failure_preserves_extraction_and_candidate_stages(self) -> None:
        self.assertEqual(stages_for_failure("frontend_rendering"), ("render", "verify"))
        self.assertEqual(stages_for_failure("math_representation"), ("build", "render", "verify", "package"))
        self.assertEqual(stages_for_failure("source_extraction")[0], "extract")

    def test_baseline_acceptance_requires_exact_inventory_hashes(self) -> None:
        registry = {
            "banks": [{"archive": "ch01.zip", "archive_sha256": "a" * 64}],
            "records": {
                "ch01-q0001": {
                    "archive": "ch01.zip",
                    "record_sha256": "b" * 64,
                }
            },
        }
        accepted = baseline_acceptance(
            registry, "ch01-q0001", archive_sha256="a" * 64, record_sha256="b" * 64
        )
        self.assertEqual(accepted["provenance"], "baseline_accepted")
        self.assertEqual(accepted["candidate_sha256"], "b" * 64)
        with self.assertRaisesRegex(ValueError, "baseline record hash"):
            baseline_acceptance(
                registry, "ch01-q0001", archive_sha256="a" * 64, record_sha256="c" * 64
            )


if __name__ == "__main__":
    unittest.main()
