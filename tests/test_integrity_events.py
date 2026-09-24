import unittest

from ksat.integrity import (
    CLIENT_EMITTED_CODES,
    integrity_definition,
    summarize_integrity_events,
)


class IntegrityEventCatalogueTests(unittest.TestCase):
    def test_context_menu_alias_is_informational(self):
        item = integrity_definition("contextmenu")

        self.assertEqual("context_menu", item.code)
        self.assertFalse(item.counts_as_violation)
        self.assertIn("right-click", item.explanation.lower())

    def test_every_client_emitted_code_has_readable_metadata(self):
        for code in CLIENT_EMITTED_CODES:
            with self.subTest(code=code):
                item = integrity_definition(code)
                self.assertNotEqual(code, item.title)
                self.assertIn(
                    item.evidence_class,
                    {"blocked_action", "visibility_change", "monitoring_anomaly"},
                )

    def test_monitor_gap_does_not_claim_a_known_cause(self):
        item = integrity_definition("browser_monitor_gap")

        self.assertIn("cause is unverified", item.explanation.lower())
        self.assertEqual("monitoring_anomaly", item.evidence_class)

    def test_visibility_events_within_two_seconds_are_one_incident(self):
        incidents = summarize_integrity_events(
            [
                {"violation_type": "visibility_hidden", "occurred_at": "2026-09-24T10:00:00+00:00"},
                {"violation_type": "visibility_hidden", "occurred_at": "2026-09-24T10:00:01+00:00"},
                {"violation_type": "visibility_hidden", "occurred_at": "2026-09-24T10:00:04+00:00"},
            ]
        )

        self.assertEqual(2, len(incidents))
        self.assertEqual(2, len(incidents[0]["details"]))
        self.assertEqual(1, len(incidents[1]["details"]))

    def test_unknown_event_keeps_raw_code_only_as_technical_detail(self):
        [incident] = summarize_integrity_events(
            [{"violation_type": "future_signal", "occurred_at": "2026-09-24T10:00:00+00:00"}]
        )

        self.assertEqual("Unrecognized integrity event", incident["title"])
        self.assertEqual("future_signal", incident["original_code"])
        self.assertNotIn("future_signal", incident["explanation"])


if __name__ == "__main__":
    unittest.main()
