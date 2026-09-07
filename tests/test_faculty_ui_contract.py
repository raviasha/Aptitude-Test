import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "static" / "app.js"


class FacultyUiContractTests(unittest.TestCase):
    def test_used_release_has_no_launch_control_and_directs_faculty_to_duplicate(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
process.stdout.write(JSON.stringify({
  used: ui.facultyLaunchAction({test_id: 17, launched: false, release_used: true, release_state: 'prepared'}),
  fresh: ui.facultyLaunchAction({test_id: 18, launched: false, release_used: false, release_state: 'prepared'}),
  active: ui.facultyLaunchAction({test_id: 19, launched: true, release_used: true, release_state: 'launched'})
}));
"""
        )

        self.assertNotIn("data-launch-test", result["used"])
        self.assertIn("duplicate to rerun", result["used"])
        self.assertIn('data-launch-test="18"', result["fresh"])
        self.assertIn('data-close-test="19"', result["active"])

    def _run_node(self, program):
        node = os.environ.get("KSAT_NODE") or shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for the JavaScript behavior contract.")
        completed = subprocess.run(
            [node, "-e", program, str(SCRIPT)],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
