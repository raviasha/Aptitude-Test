from __future__ import annotations

import copy
import unittest

from scripts.upgrade_v2_render_state import runtime_contract_is_compatible


class UpgradeRenderStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old = {
            "runtime_policy": {
                "python_abi": "3.14",
                "playwright_version": "1.55.0",
                "validation_viewports": [[1024, 768], [1600, 900]],
                "headless": True,
                "render_contract_version": "contract-2",
            },
            "runtime_evidence": {"selected_browser_identity": "microsoft-edge:140"},
        }
        self.new = copy.deepcopy(self.old)
        self.new["runtime_policy"]["render_contract_version"] = "contract-3"

    def test_allows_only_render_contract_version_change(self) -> None:
        self.assertTrue(runtime_contract_is_compatible(self.old, self.new))

    def test_rejects_viewport_runtime_or_browser_change(self) -> None:
        for section, key, value in (
            ("runtime_policy", "validation_viewports", [[1600, 900]]),
            ("runtime_policy", "playwright_version", "2.0.0"),
            ("runtime_evidence", "selected_browser_identity", "playwright-chromium:141"),
        ):
            changed = copy.deepcopy(self.new)
            changed[section][key] = value
            with self.subTest(section=section, key=key):
                self.assertFalse(runtime_contract_is_compatible(self.old, changed))


if __name__ == "__main__":
    unittest.main()
