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
    def test_exam_integrity_behaviors(self):
        for scenario in (
            'two_tabs_cannot_overwrite_pending_integrity_events',
            'focus_loss_blocks_immediately_while_still_fullscreen',
            'polling_catches_missing_browser_events_and_fullscreen_exit',
            'failed_violation_survives_reload_and_is_retried_with_same_id',
            'submit_waits_for_violation_acknowledgment',
        ):
            with self.subTest(scenario=scenario):
                self._run_flow(scenario)

    def test_login_shows_supported_departments_and_rights_notice(self):
        self._run_flow("login_shows_supported_departments")
        index = INDEX.read_text(encoding="utf-8")
        self.assertIn("Rights Reserved: AIML Department, KSIT", index)

    def test_pending_upload_reveals_local_score_and_review_only_after_faculty_close(self):
        self._run_flow("pending_review_waits_for_faculty_close")

    def test_slow_pending_review_check_does_not_block_receipt_or_replace_result(self):
        self._run_flow("slow_pending_review_does_not_delay_upload_receipt")

    def test_local_review_updates_upload_receipt_without_replacing_questions(self):
        self._run_flow("local_review_receipt_updates_without_replacing_questions")

    def test_local_review_upload_failure_keeps_pending_attempt_protected(self):
        self._run_flow("local_review_reports_failed_upload_without_unlocking_navigation")

    def test_back_to_pending_submission_ignores_late_review_receipt(self):
        self._run_flow("pending_review_back_ignores_late_receipt_and_does_not_reopen_itself")

    def test_new_signin_cannot_reuse_previous_students_cached_review_grant(self):
        self._run_flow("new_signin_cannot_reuse_previous_session_review_grant")

    def test_browser_session_marker_distinguishes_reopen_from_same_tab_refresh(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
function storage() {
  const values = new Map();
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, value)
  };
}
const firstTab = storage();
process.stdout.write(JSON.stringify({
  firstOpen: ui.beginBrowserSession(firstTab),
  sameTabRefresh: ui.beginBrowserSession(firstTab),
  reopenedTab: ui.beginBrowserSession(storage())
}));
"""
        )
        self.assertEqual(
            {"firstOpen": True, "sameTabRefresh": False, "reopenedTab": True},
            result,
        )

    def test_fresh_browser_session_logs_out_only_idle_or_completed_students(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
const states = [
  'login', 'device_setup', 'waiting_or_ready', 'in_progress',
  'sealed_pending', 'faculty_intervention_required', 'acknowledged_result'
];
process.stdout.write(JSON.stringify({
  fresh: Object.fromEntries(states.map(state => [state, ui.shouldAutoLogout(true, state)])),
  resumed: Object.fromEntries(states.map(state => [state, ui.shouldAutoLogout(false, state)]))
}));
"""
        )
        self.assertEqual(
            {
                "login": False,
                "device_setup": False,
                "waiting_or_ready": True,
                "in_progress": False,
                "sealed_pending": False,
                "faculty_intervention_required": False,
                "acknowledged_result": True,
            },
            result["fresh"],
        )
        self.assertTrue(all(value is False for value in result["resumed"].values()))

    def test_logout_posts_explicit_confirmation(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
const calls = [];
ui.logoutStudent(async (path, options) => {
  calls.push({path, options});
  return {state: 'login'};
}).then(response => process.stdout.write(JSON.stringify({calls, response})));
"""
        )
        self.assertEqual(
            [{
                "path": "/api/logout",
                "options": {"method": "POST", "body": '{"confirmed":true}'},
            }],
            result["calls"],
        )
        self.assertEqual({"state": "login"}, result["response"])

    def test_registration_payload_requires_matching_passwords_and_excludes_confirmation(self):
        result = self._run_node(
            """
const ui = require(process.argv[1]);
let mismatch;
let shortPassword;
try {
  ui.registrationPayload({
    student_id: ' S100 ', name: ' Student One ', student_class: ' AIML ',
    section: ' a ', password: 'student123', confirm_password: 'different'
  });
} catch (error) {
  mismatch = error.message;
}
try {
  ui.registrationPayload({
    student_id: 'S100', name: 'Student One', student_class: 'AIML',
    section: 'A', password: '12345', confirm_password: '12345'
  });
} catch (error) {
  shortPassword = error.message;
}
process.stdout.write(JSON.stringify({
  valid: ui.registrationPayload({
    student_id: ' S100 ', name: ' Student One ', student_class: ' AIML ',
    section: ' a ', password: 'student123', confirm_password: 'student123'
  }),
  mismatch,
  shortPassword
}));
"""
        )
        self.assertEqual(
            {
                "student_id": "S100",
                "name": "Student One",
                "student_class": "AIML",
                "section": "a",
                "password": "student123",
            },
            result["valid"],
        )
        self.assertEqual("Passwords do not match.", result["mismatch"])
        self.assertEqual(
            "Password must be at least 6 characters.", result["shortPassword"]
        )
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("Enrollment code", source)
        self.assertNotIn("Computer label", source)
        self.assertIn("New student? Create account", source)

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
        result = self._run_node(
            """
const ui = require(process.argv[1]);
const expired = {code: 'invalid_client_session', retryable: false};
process.stdout.write(JSON.stringify({
  invalidSession: ui.reviewFailureAction({code: 'invalid_client_session', retryable: false}),
  missingSession: ui.reviewFailureAction({code: 'client_session_required', retryable: false}),
  inactiveDevice: ui.reviewFailureAction({code: 'device_inactive', retryable: false}),
  unenrolledDevice: ui.reviewFailureAction({code: 'device_not_enrolled', retryable: false}),
  outage: ui.reviewFailureAction({code: 'coordinator_unavailable', retryable: true}),
  transport: ui.reviewFailureAction(),
  corrupt: ui.reviewFailureAction({code: 'content_hash_mismatch', retryable: false}),
  availableExpired: ui.acknowledgedReviewStatus(ui.reviewFailureAction(expired), expired)
}));
"""
        )
        self.assertEqual("signin", result["invalidSession"])
        self.assertEqual("signin", result["missingSession"])
        self.assertEqual("device", result["inactiveDevice"])
        self.assertEqual("device", result["unenrolledDevice"])
        self.assertEqual("retry", result["outage"])
        self.assertEqual("retry", result["transport"])
        self.assertEqual("stop", result["corrupt"])
        self.assertEqual(
            {
                "message": "Your sign-in has expired. Sign in again to check review availability.",
                "action": "signin",
                "label": "Sign in again",
                "autoRetry": False,
            },
            result["availableExpired"],
        )
        source = SCRIPT.read_text(encoding="utf-8")
        acknowledged = source[
            source.index("function renderAcknowledged"):
            source.index("function showProblem")
        ]
        self.assertIn("checkAcknowledgedReview", acknowledged)
        self.assertIn("async function checkAcknowledgedReview", source)
        self.assertIn("Could not check yet. Your result remains available", source)

    def test_student_can_start_another_test_without_signing_out(self):
        self._run_flow("result_to_next_test")

    def test_slow_completed_reviews_do_not_delay_available_tests(self):
        self._run_flow("slow_reviews_do_not_block_launches")

    def test_new_launches_refresh_with_completed_reviews_and_recover_after_failure(self):
        self._run_flow("launched_tests_refresh_with_closed_reviews")

    def test_pending_list_refresh_cannot_replace_an_active_exam(self):
        self._run_flow("late_lists_cannot_replace_exam")

    def test_pending_review_check_cannot_restore_result_after_signout(self):
        self._run_flow("late_review_cannot_replace_signin")

    def test_review_and_back_navigation_ignore_pending_list_refresh(self):
        self._run_flow("review_navigation_owns_screen")

    def test_attempt_poll_is_single_flight_and_uses_embedded_result(self):
        self._run_flow("attempt_poll_reuses_embedded_result")

    def test_attempt_result_endpoint_fallback_remains_supported(self):
        self._run_flow("result_fallback_remains_supported")

    def test_pending_test_start_cannot_be_hidden_by_other_navigation(self):
        self._run_flow("pending_start_keeps_navigation_from_hiding_the_attempt")

    def test_pending_signout_cannot_be_cancelled_by_other_navigation(self):
        self._run_flow("pending_signout_keeps_navigation_from_reviving_the_session")

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
  'device_inactive', 'corrupt_local_attempt', 'faculty_intervention_required',
  'invalid_registration', 'student_id_exists', 'registration_rate_limited'
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
            "Check the student details and use a password of at least 6 characters.",
            result["messages"]["invalid_registration"],
        )
        self.assertEqual(
            "That Student ID is already registered.",
            result["messages"]["student_id_exists"],
        )
        self.assertEqual(
            "Too many accounts are being created. Wait one minute and try again.",
            result["messages"]["registration_rate_limited"],
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

    def _run_flow(self, scenario):
        node = os.environ.get("KSAT_NODE") or shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for the JavaScript behavior contract.")
        completed = subprocess.run(
            [node, str(ROOT / "tests" / "client_ui_flow_harness.js"), str(SCRIPT), scenario],
            cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"passed": True}, json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()
