import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "static" / "client" / "app.js"
INDEX = ROOT / "static" / "client" / "index.html"


class ClientUiContractTests(unittest.TestCase):
    def test_review_choice_states_and_poll_delay_execute_in_javascript(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
process.stdout.write(JSON.stringify({
  correct: ui.reviewChoiceState('B', 'B'),
  incorrect: ui.reviewChoiceState('A', 'B'),
  unanswered: ui.reviewChoiceState(null, 'B'),
  delay: ui.reviewPollDelay
}));
"""
        )
        self.assertEqual({"state": "correct", "selected": "B", "correct": "B"}, result["correct"])
        self.assertEqual({"state": "incorrect", "selected": "A", "correct": "B"}, result["incorrect"])
        self.assertEqual({"state": "unanswered", "selected": None, "correct": "B"}, result["unanswered"])
        self.assertEqual(5000, result["delay"])
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/api/reviews", source)
        self.assertNotIn("innerHTML", source)

    def test_acknowledged_score_survives_transient_review_poll_failures(self):
        source = SCRIPT.read_text(encoding="utf-8")
        acknowledged = source[
            source.index("function renderAcknowledged"):
            source.index("function showProblem")
        ]
        self.assertIn("checkAcknowledgedReview", acknowledged)
        self.assertNotIn("loadAssessments", acknowledged)
        self.assertIn("async function checkAcknowledgedReview", source)
        self.assertIn("Could not check yet. Your result remains available", source)

    def test_static_contract_has_safe_local_state_machine_and_no_coordinator_secrets(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Your answers are safe and will upload automatically.", source)
        self.assertIn("data-answer", source)
        self.assertIn("sealed_pending", source)
        self.assertIn("faculty_intervention_required", source)
        self.assertIn("acknowledged_result", source)
        self.assertIn("saving", source)
        self.assertIn("saved", source)
        self.assertNotIn("/api/client/v1", source)
        self.assertNotIn("Something went wrong", source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("insertAdjacentHTML", source)
        for marker in (
            "private_key", "content_key", "access_token",
            "coordinator_url", "coordinator_base_url", "trusted_ca", "signed_bundle",
        ):
            self.assertNotIn(marker, source.lower())

    def test_exact_problem_messages_and_unknown_fallback_execute_in_javascript(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
const codes = [
  'coordinator_unavailable', 'start_window_closed', 'content_hash_mismatch',
  'device_inactive', 'corrupt_local_attempt', 'faculty_intervention_required'
];
process.stdout.write(JSON.stringify({
  messages: Object.fromEntries(codes.map(code => [code, ui.problemMessage(code)])),
  unknown: ui.problemMessage('unknown', 'KSAT-ABC1234567')
}));
"""
        )
        self.assertEqual(
            "The assessment server is temporarily unavailable. Your saved work is safe.",
            result["messages"]["coordinator_unavailable"],
        )
        self.assertEqual(
            "The 10-minute start window has closed. Ask Faculty for help.",
            result["messages"]["start_window_closed"],
        )
        self.assertEqual(
            "The assessment download failed verification and will be downloaded again.",
            result["messages"]["content_hash_mismatch"],
        )
        self.assertEqual(
            "This lab computer is not registered. Ask Faculty or IT for help.",
            result["messages"]["device_inactive"],
        )
        self.assertEqual(
            "Saved assessment data could not be verified. Do not close the application; ask Faculty for help.",
            result["messages"]["corrupt_local_attempt"],
        )
        self.assertEqual(
            "The sealed submission needs Faculty attention. Your answers remain saved on this computer.",
            result["messages"]["faculty_intervention_required"],
        )
        self.assertEqual(
            "The requested action could not be completed. Reference: KSAT-ABC1234567",
            result["unknown"],
        )

    def test_sealed_state_disables_edit_and_optimistic_answer_can_roll_back(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
const optimistic = ui.optimisticSelection('A', 'B');
process.stdout.write(JSON.stringify({
  inProgress: ui.canEdit('in_progress'),
  sealed: ui.canEdit('sealed_pending'),
  acknowledged: ui.canEdit('acknowledged_result'),
  optimistic,
  restored: ui.restoreSelection(optimistic, 'A'),
  sealedMessage: ui.sealedMessage
}));
"""
        )
        self.assertTrue(result["inProgress"])
        self.assertFalse(result["sealed"])
        self.assertFalse(result["acknowledged"])
        self.assertEqual({"selected": "B", "status": "saving"}, result["optimistic"])
        self.assertEqual({"selected": "A", "status": "error"}, result["restored"])
        self.assertEqual(
            "Your answers are safe and will upload automatically.", result["sealedMessage"]
        )

    def test_deferred_local_save_keeps_saving_visible_then_persists_success_or_error(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
let resolveWrite;
let rejectWrite;
const successAttempt = {responses: {'7': 'A'}};
const successStates = [];
const pending = ui.persistOptimisticAnswer(
  successAttempt, 7, 'B',
  () => new Promise(resolve => { resolveWrite = resolve; }),
  state => successStates.push({state, selected: successAttempt.responses['7']})
);
const beforeResolve = {
  states: [...successStates], selected: successAttempt.responses['7']
};
resolveWrite({selected_answer: 'B'});
pending.then(async () => {
  const failureAttempt = {responses: {'7': 'A'}};
  const failureStates = [];
  const failure = ui.persistOptimisticAnswer(
    failureAttempt, 7, 'B',
    () => new Promise((_resolve, reject) => { rejectWrite = reject; }),
    state => failureStates.push({state, selected: failureAttempt.responses['7']})
  );
  const beforeReject = {
    states: [...failureStates], selected: failureAttempt.responses['7']
  };
  rejectWrite(new Error('disk full'));
  try { await failure; } catch (_error) {}
  process.stdout.write(JSON.stringify({
    beforeResolve,
    successStates,
    beforeReject,
    failureStates,
    failureSelected: failureAttempt.responses['7'],
    messages: ['saving', 'saved', 'error'].map(ui.saveStatusMessage)
  }));
});
"""
        )
        self.assertEqual(
            [{"state": "saving", "selected": "B"}],
            result["beforeResolve"]["states"],
        )
        self.assertEqual("B", result["beforeResolve"]["selected"])
        self.assertEqual("saved", result["successStates"][-1]["state"])
        self.assertEqual(
            [{"state": "saving", "selected": "B"}],
            result["beforeReject"]["states"],
        )
        self.assertEqual("error", result["failureStates"][-1]["state"])
        self.assertEqual("A", result["failureSelected"])
        self.assertEqual(
            [
                "Saving locally…",
                "Saved locally",
                "Local save failed; selection restored",
            ],
            result["messages"],
        )

    def test_navigation_uses_durable_local_position_contract(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/position", source)
        self.assertIn("current_question_id", source)
        self.assertIn("persistPosition", source)

    def test_untrusted_question_text_uses_text_content_not_html(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
const element = {textContent: '', innerHTML: 'unchanged'};
ui.setSafeText(element, '<img src=x onerror=steal()>');
process.stdout.write(JSON.stringify(element));
"""
        )
        self.assertEqual("<img src=x onerror=steal()>", result["textContent"])
        self.assertEqual("unchanged", result["innerHTML"])

    def test_public_asset_urls_are_local_attempt_bound_and_reject_hostile_references(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
const attempt = '11111111-1111-4111-8111-111111111111';
const digest = 'a'.repeat(64);
process.stdout.write(JSON.stringify({
  valid: ui.assetUrl(attempt, `assets/${digest}.png`),
  remote: ui.assetUrl(attempt, 'https://coordinator.invalid/secret.png'),
  traversal: ui.assetUrl(attempt, 'assets/%2e%2e%2fsecret.png'),
  data: ui.assetUrl(attempt, 'data:image/png;base64,AAAA'),
  otherAttemptShape: ui.assetUrl('../other', `assets/${digest}.png`)
}));
"""
        )
        self.assertEqual(
            "/api/attempts/11111111-1111-4111-8111-111111111111/assets/"
            + "a" * 64
            + ".png",
            result["valid"],
        )
        for key in ("remote", "traversal", "data", "otherAttemptShape"):
            self.assertIsNone(result[key])

    def test_page_has_accessible_announcements_and_exam_protection_hooks(self):
        index = INDEX.read_text(encoding="utf-8")
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('aria-live="polite"', index)
        self.assertIn('aria-live="assertive"', index)
        self.assertIn('id="fullscreen-gate"', index)
        self.assertIn('requestFullscreen', source)
        for event_name in (
            "visibilitychange", "blur", "copy", "cut", "paste", "contextmenu",
            "dragstart", "drop", "beforeprint", "keydown",
        ):
            self.assertIn(event_name, source)
        self.assertIn("/violations", source)
        self.assertIn("textContent", source)
        self.assertIn("createElement('img')", source)
        self.assertIn("display_media", source)
        self.assertIn("assetUrl", source)
        self.assertNotIn("data:image", source)

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
