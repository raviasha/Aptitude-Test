from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class WindowsReleaseContractTests(unittest.TestCase):
    def test_installer_and_build_script_publish_ksat_1_3_4(self) -> None:
        installer = (ROOT / "installer" / "AptitudeLab.iss").read_text(encoding="utf-8")
        build_script = (ROOT / "build-windows.bat").read_text(encoding="utf-8")
        application = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertEqual(
            re.search(r'^#define AppVersion "([^"]+)"$', installer, re.MULTILINE).group(1),
            "1.3.4",
        )
        self.assertIn("OutputBaseFilename=KSAT-Setup-1.3.4", installer)
        self.assertIn(r"Complete: release\KSAT-Setup-1.3.4.exe", build_script)
        self.assertEqual(
            re.search(r'^APP_VERSION = "([^"]+)"$', application, re.MULTILINE).group(1),
            "1.3.4",
        )


if __name__ == "__main__":
    unittest.main()
