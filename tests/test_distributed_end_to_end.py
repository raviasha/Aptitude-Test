import os
import hashlib
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


class DistributedEndToEndTests(unittest.TestCase):
    def test_external_fixture_is_namespaced_owned_and_cleanup_rejects_wrong_owner(self):
        import app
        from scripts.load_distributed_assessment import _Fixture

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ), _Fixture(Path(directory), 1, 2) as fixture:
            with app.db() as connection:
                connection.execute(
                    "INSERT INTO admins VALUES (?,?,?)",
                    ("load-admin", "Load Admin", app.hash_password("load-password")),
                )
            login = fixture.test_client.post(
                "/api/login",
                json={"identifier": "load-admin", "password": "load-password", "role": "admin"},
            )
            csrf = login.json()["csrf_token"]
            namespace = f"LOAD-{os.urandom(16).hex().upper()}"
            created = fixture.test_client.post(
                "/api/admin/load-tests",
                headers={"X-KSAT-CSRF": csrf},
                json={
                    "namespace": namespace,
                    "ownership_token": "o" * 43,
                    "clients": 2,
                    "questions": 3,
                },
            )
            self.assertEqual(201, created.status_code, created.text)
            denied = fixture.test_client.delete(
                f"/api/admin/load-tests/{namespace}",
                headers={"X-KSAT-CSRF": csrf, "X-KSAT-Load-Ownership": "wrong"},
            )
            self.assertEqual(403, denied.status_code)
            with app.db() as connection:
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM students WHERE student_id LIKE ?",
                        (f"{namespace}-STUDENT-%",),
                    ).fetchone()["count"],
                )
            cleaned = fixture.test_client.delete(
                f"/api/admin/load-tests/{namespace}",
                headers={"X-KSAT-CSRF": csrf, "X-KSAT-Load-Ownership": "o" * 43},
            )
            self.assertEqual(200, cleaned.status_code, cleaned.text)
            self.assertEqual(0, cleaned.json()["residual_rows"])

    def test_external_gate_uses_supplied_https_ca_and_safe_report(self):
        import app
        from scripts.load_distributed_assessment import _Fixture, run_external_gate

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ), _Fixture(Path(directory) / "coordinator", 1, 2, real_https=True) as fixture:
            with app.db() as connection:
                connection.execute(
                    "INSERT INTO admins VALUES (?,?,?)",
                    ("load-admin", "Load Admin", app.hash_password("load-password")),
                )
            report = run_external_gate(
                Path(directory) / "external-clients",
                base_url=fixture.base_url,
                ca_file=fixture.security.ca_certificate_path,
                admin_username="load-admin",
                admin_password="load-password",
                enrollment_code=fixture.enrollment_code,
                clients=1,
                questions=2,
                outage=False,
                start_spread_seconds=0,
                enforce_performance_thresholds=False,
            )
            serialized = json.dumps(report)
            self.assertEqual("authorized_external_https", report["mode"])
            self.assertEqual(1, report["accepted_results"])
            self.assertEqual(0, report["cleanup"]["residual_rows"])
            for secret in ("load-password", fixture.enrollment_code, "ownership_token"):
                self.assertNotIn(secret, serialized)

    def test_percentile_uses_hyndman_fan_type_7_linear_interpolation(self):
        from scripts.load_distributed_assessment import _percentile

        self.assertEqual(7.5, _percentile([0.0, 10.0, 20.0, 30.0], 25))
        self.assertEqual(29.7, _percentile([0.0, 10.0, 20.0, 30.0], 99))

    def test_smoke_mode_exempts_only_latency_thresholds(self):
        from scripts.load_distributed_assessment import _failed_threshold_names

        thresholds = {
            "local_answer_p99_below_100_ms": False,
            "submission_ack_p95_below_10_s": False,
            "zero_missing": True,
        }
        self.assertEqual([], _failed_threshold_names(thresholds, False))
        self.assertEqual(
            [
                "local_answer_p99_below_100_ms",
                "submission_ack_p95_below_10_s",
            ],
            _failed_threshold_names(thresholds, True),
        )
        thresholds["zero_missing"] = False
        self.assertEqual(["zero_missing"], _failed_threshold_names(thresholds, False))

    def test_load_gate_requires_explicit_fixture_authorization(self):
        from scripts.load_distributed_assessment import _Fixture, run_isolated_gate

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "0"}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "KSAT_LOAD_TEST=1"):
                run_isolated_gate(
                    Path(directory), clients=1, questions=2, outage=False
                )
            with self.assertRaisesRegex(RuntimeError, "KSAT_LOAD_TEST=1"):
                _Fixture(Path(directory), 1, 2)

    def test_failed_threshold_report_is_written_before_nonzero_exit(self):
        from scripts import load_distributed_assessment as load

        failed_report = {"thresholds": {"all_results_acknowledged": False}}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "failed.json"
            with patch.dict(os.environ, {"KSAT_LOAD_TEST": "1"}), patch.object(
                load,
                "run_isolated_gate",
                side_effect=load.LoadGateFailure(failed_report),
            ):
                exit_code = load.main(["--isolated", "--report", str(destination)])
            self.assertEqual(1, exit_code)
            self.assertEqual(
                failed_report["thresholds"],
                json.loads(destination.read_text(encoding="utf-8"))["thresholds"],
            )

    def test_failed_production_start_unwinds_client_lifespan_and_process_lock(self):
        from client_app import ClientProcessLock
        from scripts.load_distributed_assessment import _Fixture

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ), _Fixture(Path(directory), 1, 2, real_https=True) as fixture, patch.object(
            fixture, "start_machine", side_effect=RuntimeError("start failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                fixture.make_machine(0)
            state_dir = (
                Path(directory)
                / "clients"
                / "machine-000"
                / "program-data"
                / "KSAT Client"
                / "state"
            )
            lock = ClientProcessLock(state_dir).acquire()
            lock.release()

    def test_client_starts_answers_offline_restarts_and_retries_after_outage(self):
        from scripts.load_distributed_assessment import run_isolated_gate

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ):
            report = run_isolated_gate(
                Path(directory), clients=3, questions=4, outage=True,
                start_spread_seconds=0,
                enforce_performance_thresholds=False,
            )

        self.assertEqual(3, report["accepted_results"])
        self.assertEqual(0, report["missing_attempt_count"])
        self.assertEqual(0, report["duplicate_count"])
        self.assertEqual(0, report["editable_sealed_attempt_count"])
        self.assertEqual(3, report["outage"]["sealed_pending_before_restart"])
        self.assertEqual(3, report["outage"]["acknowledged_after_restart"])
        self.assertTrue(report["outage"]["coordinator_service_restarted"])
        self.assertFalse(report["performance_thresholds_enforced"])
        self.assertEqual(12, report["latency_ms"]["local_answer"]["count"])
        self.assertGreaterEqual(report["latency_ms"]["local_answer"]["maximum"], 0.0)
        self.assertEqual(0, report["cleanup"]["residual_rows"])

    def test_simultaneous_submission_uses_production_routes_and_writer(self):
        from scripts.load_distributed_assessment import run_isolated_gate

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ):
            report = run_isolated_gate(
                Path(directory), clients=8, questions=5, outage=False,
                start_spread_seconds=0,
                enforce_performance_thresholds=False,
            )

        self.assertEqual("production_https_subprocess", report["mode"])
        self.assertEqual("coordinator_subprocess_only", report["coordinator"]["scope"])
        self.assertEqual(8, report["accepted_results"])
        self.assertEqual(0, report["errors"].get("database is locked", 0))
        self.assertEqual(8, report["request_counts"]["POST /api/client/v1/submissions"])
        self.assertEqual(8, report["latency_ms"]["submission_ack"]["count"])
        self.assertEqual(0, report["submissions_before_barrier"])
        self.assertFalse(report["performance_thresholds_enforced"])
        self.assertEqual(40, report["latency_ms"]["local_answer"]["count"])
        self.assertGreaterEqual(report["latency_ms"]["local_answer"]["maximum"], 0.0)
        self.assertGreaterEqual(report["maximum_writer_queue_depth"], 0)
        self.assertEqual(report["cleanup"]["created_rows"], report["cleanup"]["deleted_rows"])

    def test_load_gate_measures_the_requested_client_start_spread(self):
        from scripts.load_distributed_assessment import run_isolated_gate

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ):
            report = run_isolated_gate(
                Path(directory), clients=3, questions=2, outage=False,
                start_spread_seconds=0.2,
                enforce_performance_thresholds=False,
            )

        self.assertGreaterEqual(report["observed_start_spread_seconds"], 0.15)
        self.assertTrue(report["thresholds"]["requested_start_spread_observed"])

    def test_offline_expiry_seals_locally_without_contacting_coordinator(self):
        from ksat.client.runtime import AssessmentRuntime
        from ksat.client.store import ClientStore
        from scripts.load_distributed_assessment import _Fixture

        class ExpiredClock:
            def __init__(self, wall):
                self.wall = wall

            def utcnow(self):
                return self.wall

            def monotonic(self):
                return 10_000.0

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ), _Fixture(Path(directory), 1, 2, real_https=True) as fixture:
            machine = fixture.make_machine(0)
            try:
                record = machine.store.load_attempt(machine.attempt_id)
                deadline = record.ticket.ticket.deadline
                machine.coordinator.close()
                machine.store.close()
                fixture.stop_coordinator()
                machine.store = ClientStore(machine.root / "state" / "client.sqlite3")
                machine.runtime = AssessmentRuntime(
                    machine.store,
                    machine.identity_store.load_or_create(),
                    ExpiredClock(deadline + timedelta(seconds=1)),
                )

                recovered = machine.runtime.recover()

                self.assertIsNotNone(recovered)
                self.assertEqual("sealed_pending", recovered.state)
                self.assertEqual(
                    "sealed_pending",
                    machine.store.load_attempt(machine.attempt_id).state,
                )
            finally:
                machine.close()
                cleanup = fixture.cleanup()
                self.assertEqual(0, cleanup["residual_rows"])

    def test_lost_ack_replays_idempotently_and_faculty_void_authorizes_one_retry(self):
        import app
        import httpx
        from ksat.client.outbox import OutboxWorker
        from scripts.load_distributed_assessment import _Fixture

        class LoseFirstAcknowledgment:
            def __init__(self, coordinator):
                self.coordinator = coordinator
                self.first = True

            def submit_bundle(self, bundle):
                receipt = self.coordinator.submit_bundle(bundle)
                if self.first:
                    self.first = False
                    raise httpx.ConnectError(
                        "acknowledgment lost",
                        request=httpx.Request("POST", "https://localhost/submissions"),
                    )
                return receipt

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ), _Fixture(Path(directory), 1, 3, real_https=True) as fixture:
            machine = fixture.make_machine(0)
            try:
                for question_id in machine.runtime.snapshot().question_order:
                    machine.runtime.answer(question_id, "A")
                machine.restart(fixture.transport)
                machine.runtime.submit()
                machine.clock.advance(10)
                machine.outbox = OutboxWorker(
                    machine.store,
                    LoseFirstAcknowledgment(machine.coordinator),
                    machine.clock,
                    random_source=lambda: 0.5,
                )

                self.assertEqual(1, machine.outbox.process_due_once())
                self.assertEqual(
                    "sealed_pending", machine.store.load_attempt(machine.attempt_id).state
                )
                with app.db() as connection:
                    self.assertEqual(
                        1,
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM submissions WHERE attempt_id=?",
                            (machine.attempt_id,),
                        ).fetchone()["count"],
                    )

                machine.clock.advance(31)
                self.assertEqual(1, machine.outbox.process_due_once())
                self.assertEqual(
                    "acknowledged", machine.store.load_attempt(machine.attempt_id).state
                )
                with app.db() as connection:
                    connection.execute(
                        "INSERT INTO admins VALUES (?,?,?)",
                        (
                            f"{fixture.namespace.casefold()}-admin",
                            "Load Faculty",
                            app.hash_password("faculty-password"),
                        ),
                    )
                denied = fixture.test_client.post(
                    f"/api/admin/attempts/{machine.attempt_id}/void",
                    json={
                        "reason": "machine recovery",
                        "authorize_retake": True,
                        "confirm_submitted": True,
                    },
                )
                self.assertIn(denied.status_code, {401, 403})
                login = fixture.test_client.post(
                    "/api/login",
                    json={
                        "identifier": f"{fixture.namespace.casefold()}-admin",
                        "password": "faculty-password",
                        "role": "admin",
                    },
                )
                self.assertEqual(200, login.status_code, login.text)
                voided = fixture.test_client.post(
                    f"/api/admin/attempts/{machine.attempt_id}/void",
                    headers={"X-KSAT-CSRF": login.json()["csrf_token"]},
                    json={
                        "reason": "machine recovery",
                        "authorize_retake": True,
                        "confirm_submitted": True,
                    },
                )
                self.assertEqual(200, voided.status_code, voided.text)
                machine.coordinator.login(machine.student_id, fixture.password)
                replacement = machine.coordinator.start_attempt(
                    fixture.release_id,
                    machine.store.load_attempt(machine.attempt_id).ticket.ticket.content_hash,
                )
                self.assertNotEqual(
                    machine.attempt_id, replacement.ticket.ticket.attempt_id
                )
            finally:
                machine.close()
                cleanup = fixture.cleanup()
                self.assertEqual(0, cleanup["residual_rows"])

    def test_corrupt_pack_is_rejected_redownloaded_and_attempt_is_device_bound(self):
        from ksat.client.coordinator import ContentVerificationError, CoordinatorClient, CoordinatorProblem
        from ksat.client.identity import DeviceIdentityStore
        from scripts.load_distributed_assessment import _Fixture

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KSAT_LOAD_TEST": "1"}, clear=False
        ), _Fixture(Path(directory), 1, 3, real_https=True) as fixture:
            first = fixture.make_machine(0)
            other_coordinator = None
            try:
                other_root = Path(directory) / "other-device"
                (other_root / "packs").mkdir(parents=True)
                identity = DeviceIdentityStore(other_root / "identity")
                other_coordinator = CoordinatorClient(
                    fixture.base_url,
                    fixture.security.ca_certificate_path,
                    identity,
                )
                other_coordinator.enroll(
                    f"{fixture.namespace}-DEVICE-OTHER", fixture.enrollment_code
                )
                other_coordinator.login(first.student_id, fixture.password)
                entry = next(
                    item
                    for item in other_coordinator.prefetch_catalog()
                    if item.release_id == fixture.release_id
                )
                destination = other_root / "packs" / entry.filename
                other_coordinator.download_pack(entry, destination)
                destination.write_bytes(b"corrupt")
                with self.assertRaises(ContentVerificationError):
                    other_coordinator.download_pack(entry, destination)
                destination.unlink()
                other_coordinator.download_pack(entry, destination)
                self.assertEqual(entry.content_hash, hashlib.sha256(destination.read_bytes()).hexdigest())

                with self.assertRaises(CoordinatorProblem) as rejected:
                    other_coordinator.start_attempt(fixture.release_id, entry.content_hash)
                self.assertEqual("attempt_bound_to_other_device", rejected.exception.code)
            finally:
                if other_coordinator is not None:
                    other_coordinator.close()
                first.close()
                cleanup = fixture.cleanup()
                self.assertEqual(0, cleanup["residual_rows"])


if __name__ == "__main__":
    unittest.main()
