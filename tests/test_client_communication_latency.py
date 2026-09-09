"""Exercise client responsiveness while coordinator requests are still in flight."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from client_app import ClientServices, create_client_app
from tests.test_client_app_api import (
    ATTEMPT_ID, OTHER_ATTEMPT_ID, RELEASE_ID,
    FakeCoordinator, FakeIdentityStore, FakeOutbox, FakeRuntime,
    FakeSnapshot, FakeStore,
)


class ClientCommunicationLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FakeStore(None)
        self.runtime = FakeRuntime(self.store)
        self.coordinator = FakeCoordinator()
        self.coordinator.login("S100", "password")
        self.outbox = FakeOutbox()
        self.services = ClientServices(
            FakeIdentityStore(), self.store, self.runtime, self.coordinator,
            self.outbox, cache_dir=Path(self.temporary.name),
        )
        self.app = create_client_app(self.services)
        self.context = self.app.state.client_context
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8010")
        self.client.__enter__()
        self.headers = {
            "Origin": "http://127.0.0.1:8010",
            "X-KSAT-CSRF": self.context.csrf_token,
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def test_slow_login_does_not_block_local_state_or_restore_session_after_logout(self):
        entered, release = threading.Event(), threading.Event()
        original_login = self.coordinator.login

        def blocked_login(student_id, password):
            entered.set()
            release.wait(5)
            return original_login(student_id, password)

        self.coordinator.login = blocked_login
        with ThreadPoolExecutor(max_workers=3) as pool:
            login = pool.submit(
                self.client.post, "/api/login", headers=self.headers,
                json={"student_id": "S200", "password": "password"},
            )
            try:
                self.assertTrue(entered.wait(1))
                logout = pool.submit(
                    self.client.post, "/api/logout", headers=self.headers,
                    json={"confirmed": True},
                )
                state = pool.submit(self.client.get, "/api/state")
                self.assertEqual(200, state.result(timeout=1).status_code)
            finally:
                release.set()
            self.assertEqual(200, login.result(timeout=2).status_code)
            self.assertEqual(200, logout.result(timeout=2).status_code)
        self.assertIsNone(self.coordinator.session)
        self.assertEqual("login", self.client.get("/api/state").json()["state"])

    def test_slow_assessment_and_review_lists_leave_local_state_responsive(self):
        for endpoint, method in (
            ("/api/assessments", "assessments"),
            ("/api/reviews", "completed_reviews"),
        ):
            with self.subTest(endpoint=endpoint):
                entered, release = threading.Event(), threading.Event()

                def blocked_list():
                    entered.set()
                    release.wait(5)
                    return []

                with patch.object(self.coordinator, method, blocked_list):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        listing = pool.submit(self.client.get, endpoint)
                        try:
                            self.assertTrue(entered.wait(1))
                            state = pool.submit(self.client.get, "/api/state")
                            response = state.result(timeout=1)
                            self.assertEqual(200, response.status_code)
                            self.assertEqual("S100", response.json()["student"]["student_id"])
                        finally:
                            release.set()
                        self.assertEqual(200, listing.result(timeout=2).status_code)

    def test_slow_review_grant_leaves_local_state_responsive_and_keeps_session_owner(self):
        entered, release = threading.Event(), threading.Event()
        self.coordinator.review_rows = [SimpleNamespace(
            attempt_id=ATTEMPT_ID, release_id=RELEASE_ID,
            test_name="Aptitude", review_state="available",
        )]
        self.store.verified_path = Path(self.temporary.name) / "review.ksatpack"

        def blocked_review(attempt_id):
            self.assertEqual(ATTEMPT_ID, attempt_id)
            entered.set()
            release.wait(5)
            return SimpleNamespace(
                attempt_id=ATTEMPT_ID, student_id="S100", release_id=RELEASE_ID,
                content_hash="a" * 64,
            )

        self.coordinator.review = blocked_review
        with ThreadPoolExecutor(max_workers=3) as pool:
            review = pool.submit(self.client.get, f"/api/reviews/{ATTEMPT_ID}")
            try:
                self.assertTrue(entered.wait(1))
                logout = pool.submit(
                    self.client.post, "/api/logout", headers=self.headers,
                    json={"confirmed": True},
                )
                state = pool.submit(self.client.get, "/api/state")
                response = state.result(timeout=1)
                self.assertEqual("S100", response.json()["student"]["student_id"])
            finally:
                release.set()
            response = review.result(timeout=2)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("B", response.json()["questions"][0]["correct_answer"])
            self.assertEqual(200, logout.result(timeout=2).status_code)
        self.assertIsNone(self.coordinator.session)
        self.assertEqual(401, self.client.get(f"/api/reviews/{ATTEMPT_ID}").status_code)

    def test_submit_wakes_upload_before_blocked_deadline_poll_returns(self):
        entered, release = threading.Event(), threading.Event()
        self.store.snapshot = FakeSnapshot()
        applied = []

        def blocked_deadline(attempt_id):
            self.assertEqual(ATTEMPT_ID, attempt_id)
            entered.set()
            release.wait(5)
            return object()

        self.coordinator.deadline_update = blocked_deadline
        self.runtime.apply_deadline_update = lambda update: applied.append(update)
        with patch("client_app.random.uniform", return_value=0.01):
            self.context.observe_snapshot(self.store.snapshot)
            self.assertTrue(entered.wait(1))
            control_thread = self.context._control_thread
            with ThreadPoolExecutor(max_workers=1) as pool:
                submitted = pool.submit(
                    self.client.post, f"/api/attempts/{ATTEMPT_ID}/submit",
                    headers=self.headers, json={"confirmed": True},
                )
                try:
                    response = submitted.result(timeout=1)
                    self.assertEqual(202, response.status_code, response.text)
                    self.assertEqual("sealed_pending", response.json()["state"])
                    self.assertEqual(1, self.outbox.wakes)
                    self.assertTrue(control_thread.is_alive())
                    self.assertIs(control_thread, self.context._control_thread)
                finally:
                    release.set()
                control_thread.join(timeout=2)
            self.assertFalse(control_thread.is_alive())
            self.assertEqual([], applied)

    def test_submit_and_result_stay_responsive_during_slow_review_listing(self):
        entered, release = threading.Event(), threading.Event()
        self.store.snapshot = FakeSnapshot()

        def blocked_reviews():
            entered.set()
            release.wait(5)
            return []

        self.coordinator.completed_reviews = blocked_reviews
        with ThreadPoolExecutor(max_workers=2) as pool:
            listing = pool.submit(self.client.get, "/api/reviews")
            try:
                self.assertTrue(entered.wait(1))
                submitted = pool.submit(
                    self.client.post, f"/api/attempts/{ATTEMPT_ID}/submit",
                    headers=self.headers, json={"confirmed": True},
                )
                response = submitted.result(timeout=1)
                self.assertEqual(202, response.status_code, response.text)
                self.assertEqual(1, self.outbox.wakes)
                result = pool.submit(self.client.get, f"/api/attempts/{ATTEMPT_ID}/result")
                response = result.result(timeout=1)
                self.assertEqual(202, response.status_code, response.text)
                self.assertEqual("sealed_pending", response.json()["state"])
            finally:
                release.set()
            self.assertEqual(200, listing.result(timeout=2).status_code)

    def test_new_attempt_resumes_control_after_previous_stopped_poll_returns(self):
        entered, release, new_polled = threading.Event(), threading.Event(), threading.Event()
        self.store.snapshot = FakeSnapshot()

        def deadline(attempt_id):
            if attempt_id == ATTEMPT_ID:
                entered.set()
                release.wait(5)
            elif attempt_id == OTHER_ATTEMPT_ID:
                new_polled.set()
            return None

        self.coordinator.deadline_update = deadline
        with patch("client_app.random.uniform", return_value=0.01):
            self.context.observe_snapshot(self.store.snapshot)
            self.assertTrue(entered.wait(1))
            original_thread = self.context._control_thread
            with ThreadPoolExecutor(max_workers=1) as pool:
                sealed = pool.submit(self.context.observe_snapshot, self.runtime.submit())
                try:
                    sealed.result(timeout=1)
                    self.store.snapshot = FakeSnapshot(attempt_id=OTHER_ATTEMPT_ID)
                    self.context.observe_snapshot(self.store.snapshot)
                finally:
                    release.set()
                original_thread.join(timeout=2)
            self.assertTrue(new_polled.wait(1), "The next attempt lost deadline polling.")

    def test_concurrent_attempt_observations_never_start_an_unowned_control_thread(self):
        self.context._stop_control_thread()
        self.store.snapshot = FakeSnapshot()
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        real_start = threading.Thread.start
        control_threads = []

        def delayed_control_start(thread):
            if thread.name == "ksat-attempt-control":
                control_threads.append(thread)
                if len(control_threads) == 1:
                    entered.set()
                    release.wait(5)
            return real_start(thread)

        def second_observation():
            second_entered.set()
            self.context.observe_snapshot(self.store.snapshot)

        try:
            with patch.object(threading.Thread, "start", delayed_control_start):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.context.observe_snapshot, self.store.snapshot)
                    try:
                        self.assertTrue(entered.wait(1))
                        second = pool.submit(second_observation)
                        self.assertTrue(second_entered.wait(1))
                        try:
                            second.result(timeout=0.2)
                        except TimeoutError:
                            pass  # A safe second starter waits for publication to finish.
                    finally:
                        release.set()
                    first.result(timeout=2)
                    second.result(timeout=2)
            self.assertEqual(1, len(control_threads), "Two control threads were started for one attempt.")
            self.assertIs(control_threads[0], self.context._control_thread)
        finally:
            release.set()
            self.context._stop_control_thread()
            for thread in control_threads:
                thread.join(timeout=2)

    def test_finishing_restart_does_not_swallow_the_next_attempt_restart(self):
        first_entered, first_release = threading.Event(), threading.Event()
        second_entered, second_release = threading.Event(), threading.Event()
        restart_started, restart_release, third_polled = (
            threading.Event(), threading.Event(), threading.Event()
        )
        third_attempt = "77777777-7777-4777-8777-777777777777"
        self.store.snapshot = FakeSnapshot()

        def deadline(attempt_id):
            if attempt_id == ATTEMPT_ID:
                first_entered.set()
                first_release.wait(5)
            elif attempt_id == OTHER_ATTEMPT_ID:
                second_entered.set()
                second_release.wait(5)
            else:
                third_polled.set()
            return None

        original_start = self.context._start_control_thread

        def delayed_restart_finish():
            original_start()
            if threading.current_thread().name == "ksat-attempt-control-restart":
                if not restart_started.is_set():
                    restart_started.set()
                    restart_release.wait(5)

        self.coordinator.deadline_update = deadline
        with patch("client_app.random.uniform", return_value=0.01), patch.object(
            self.context, "_start_control_thread", delayed_restart_finish
        ):
            try:
                self.context.observe_snapshot(self.store.snapshot)
                self.assertTrue(first_entered.wait(1))
                self.context.observe_snapshot(self.runtime.submit())
                self.store.snapshot = FakeSnapshot(attempt_id=OTHER_ATTEMPT_ID)
                self.context.observe_snapshot(self.store.snapshot)
                first_release.set()
                self.assertTrue(restart_started.wait(1))
                self.assertTrue(second_entered.wait(1))
                self.context.observe_snapshot(self.runtime.submit())
                self.store.snapshot = FakeSnapshot(attempt_id=third_attempt)
                self.context.observe_snapshot(self.store.snapshot)
                restart_release.set()
                second_release.set()
                self.assertTrue(third_polled.wait(1), "A finishing restart hid the next stopped worker.")
            finally:
                first_release.set()
                second_release.set()
                restart_release.set()


if __name__ == "__main__":
    unittest.main()
