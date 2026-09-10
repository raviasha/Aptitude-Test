import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "static" / "app.js"


class FacultyUiContractTests(unittest.TestCase):
    def test_manual_enrollment_code_control_is_absent(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("data-rotate-enrollment", source)
        self.assertNotIn("Rotate enrollment code", source)

    def test_student_deletion_warns_about_faculty_assessment_history(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            "practice and faculty assessment history, including submissions and results",
            source,
        )

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

    def test_faculty_timing_shows_exam_extension_separately_from_start_window(self):
        result = self._run_node('''
const ui = require(process.argv[1]);
if (typeof ui.facultyTimingMarkup !== 'function') {
  process.stdout.write(JSON.stringify({before: '', after: '', idle: ''}));
} else {
  const test = {test_id: 17, launched: true, remaining_seconds: 120,
    exam_timing: {duration_seconds: 600, extension_seconds: 0, active_count: 2,
      earliest_remaining_seconds: 300, latest_remaining_seconds: 420}};
  const before = ui.facultyTimingMarkup(test, 10000);
  test.exam_timing = {duration_seconds: 900, extension_seconds: 300, active_count: 2,
    earliest_remaining_seconds: 600, latest_remaining_seconds: 720};
  const after = ui.facultyTimingMarkup(test, 10000);
  test.launched = false;
  test.exam_timing.active_count = 0;
  test.exam_timing.earliest_remaining_seconds = null;
  test.exam_timing.latest_remaining_seconds = null;
  process.stdout.write(JSON.stringify({before, after, idle: ui.facultyTimingMarkup(test, 10000)}));
}
''')
        self.assertIn('Exam time left', result['before'])
        self.assertIn('05:00–07:00', result['before'])
        self.assertIn('10:00–12:00', result['after'])
        self.assertIn('5 min added', result['after'])
        self.assertIn('Start window', result['after'])
        self.assertIn('02:00', result['after'])
        self.assertIn('2 active students', result['after'])
        self.assertIn('Duration 15:00', result['idle'])
        self.assertIn('No active attempts', result['idle'])
        self.assertNotIn('data-faculty-exam-timer', result['idle'])

    def test_faculty_countdowns_tick_and_poll_without_reverting_extensions(self):
        result = self._run_node('''
const ui = require(process.argv[1]);
if (typeof ui.tickFacultyTimers !== 'function' || typeof ui.syncFacultyTimers !== 'function') {
  process.stdout.write(JSON.stringify({exam: '', start: '', refreshed: '', closed: ''}));
} else {
  const exam = {dataset: {firstDeadline: '310000', lastDeadline: '430000'}, textContent: ''};
  const start = {dataset: {deadline: '130000'}, textContent: ''};
  const cell = {innerHTML: ''};
  const root = {
    querySelectorAll: selector => selector === '[data-faculty-exam-timer]' ? [exam] : [start],
    querySelector: selector => selector === '[data-faculty-timing="17"]' ? cell : null
  };
  ui.tickFacultyTimers(root, 11000);
  ui.syncFacultyTimers([{test_id: 17, launched: true, remaining_seconds: 119,
    exam_timing: {duration_seconds: 900, extension_seconds: 300, active_count: 1,
      earliest_remaining_seconds: 599, latest_remaining_seconds: 599}}], root, 11000);
  const refreshed = cell.innerHTML;
  ui.tickFacultyTimers(root, 500000);
  process.stdout.write(JSON.stringify({exam: exam.textContent, start: start.textContent,
    refreshed, closed: cell.innerHTML}));
}
''')
        self.assertEqual('00:00', result['exam'])
        self.assertEqual('Closed', result['start'])
        self.assertIn('09:59', result['refreshed'])
        self.assertIn('5 min added', result['refreshed'])

    def test_slow_faculty_polls_never_overlap_or_write_to_a_replaced_page(self):
        result = self._run_node('''
const ui = require(process.argv[1]);
(async () => {
  if (typeof ui.createFacultyTimerSync !== 'function') {
    process.stdout.write(JSON.stringify({requests: -1, refreshed: '', detached: ''})); return;
  }
  const cell = {innerHTML: ''};
  const root = {isConnected: true, querySelector: () => cell};
  let resolve, calls = 0;
  const poll = ui.createFacultyTimerSync(root, () => {
    calls++; return new Promise(done => { resolve = done; });
  });
  const first = poll();
  await poll(); // A slow response must not allow another request to overtake it.
  const requests = calls;
  resolve({tests: [{test_id: 17, exam_timing: {active_count: 0, duration_seconds: 600, extension_seconds: 0}}]});
  await first;
  const extended = poll();
  resolve({tests: [{test_id: 17, exam_timing: {active_count: 0, duration_seconds: 900, extension_seconds: 300}}]});
  await extended;
  const refreshed = cell.innerHTML;
  const replaced = poll();
  root.isConnected = false;
  resolve({tests: [{test_id: 17, exam_timing: {active_count: 0, duration_seconds: 600, extension_seconds: 0}}]});
  await replaced;
  process.stdout.write(JSON.stringify({requests, refreshed, detached: cell.innerHTML}));
})();
''')
        self.assertEqual(1, result['requests'])
        self.assertIn('5 min added', result['refreshed'])
        self.assertEqual(result['refreshed'], result['detached'])


if __name__ == "__main__":
    unittest.main()
