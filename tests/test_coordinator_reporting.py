"""Authenticated contact and managed-update reporting regressions."""

import asyncio
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

import app
from ksat.coordinator import routes
from ksat.coordinator.client_updates import ClientUpdateStore
from ksat.protocol import canonical_json
from tests import test_client_update_api as api_fixtures
from tests import test_client_update_store as store_fixtures


class CoordinatorContactTests(unittest.TestCase):
    setUp = api_fixtures.ClientUpdateApiTests.setUp
    tearDown = api_fixtures.ClientUpdateApiTests.tearDown
    _stage_release = api_fixtures.ClientUpdateApiTests._stage_release
    _headers = api_fixtures.ClientUpdateApiTests._headers

    def contact(self):
        with app.db() as connection:
            return connection.execute("SELECT last_seen_at FROM devices WHERE device_id=?",
                                      (self.device_id,)).fetchone()[0]

    def test_authenticated_read_only_contact_persists_without_update_progress(self):
        for path in ("/api/client/v1/releases", "/api/client/v1/update-policy"):
            with self.subTest(path=path):
                with app.db() as connection:
                    connection.execute("UPDATE devices SET last_seen_at=NULL")
                    connection.commit()
                before = datetime.now(timezone.utc)
                response = self.client.get(path, headers=self._headers(
                    "GET", path, **{"X-KSAT-Client-Version": "2.1.5"}))
                self.assertEqual(200, response.status_code, response.text)
                self.assertIsNotNone(self.contact())
                self.assertGreaterEqual(datetime.fromisoformat(self.contact()), before - timedelta(seconds=1))
                with app.db() as connection:
                    row = ClientUpdateStore(connection, app.DATA_DIR / "Client Updates").dashboard(self.release.release_id)[0]
                self.assertEqual("Waiting", row["status"])
                self.assertIsNone(row["stage"])

    def test_invalid_device_proofs_never_change_contact(self):
        path = "/api/client/v1/update-policy"
        bad_signature = self._headers("GET", path, **{"X-KSAT-Signature": "invalid"})
        bad_timestamp = self._headers("GET", path, **{"X-KSAT-Timestamp": "not-a-time"})
        for initial in (None, "2026-01-01T00:00:00+00:00"):
            for headers in ({}, bad_signature, bad_timestamp):
                with self.subTest(initial=initial, headers=list(headers)):
                    with app.db() as connection:
                        connection.execute("UPDATE devices SET last_seen_at=?", (initial,))
                        connection.commit()
                    response = self.client.get(path, headers=headers)
                    self.assertEqual(403, response.status_code)
                    self.assertEqual(initial, self.contact())

    def test_replayed_proof_does_not_refresh_contact(self):
        path = "/api/client/v1/releases"
        headers = self._headers("GET", path)
        self.assertEqual(200, self.client.get(path, headers=headers).status_code)
        self.assertIsNotNone(self.contact())
        before = self.contact()
        self.assertEqual(403, self.client.get(path, headers=headers).status_code)
        self.assertEqual(before, self.contact())

    def test_authenticated_session_contact_persists(self):
        path = "/api/client/v1/session"
        # Device contact is genuine even when the student credentials are rejected.
        body = canonical_json({"device_id": self.device_id, "student_id": "missing", "password": "bad"})
        response = self.client.post(path, content=body, headers={
            **self._headers("POST", path, body), "Content-Type": "application/json"})
        self.assertEqual(401, response.status_code, response.text)
        self.assertIsNotNone(self.contact())

    def request(self):
        path = "/api/client/v1/releases"
        headers = self._headers("GET", path)
        async def receive():
            return {"type": "http.request", "body": b""}
        return Request({"type": "http", "method": "GET", "path": path,
                        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}, receive)

    def test_contact_releases_write_lock_before_downstream_transaction(self):
        with app.db() as connection:
            asyncio.run(routes._verified_device(self.request(), connection))
            self.assertIsNotNone(self.contact())
            self.assertFalse(connection.in_transaction)
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()

    def test_contact_does_not_commit_an_existing_callers_transaction(self):
        with app.db() as connection:
            connection.execute("UPDATE devices SET label='uncommitted'")
            asyncio.run(routes._verified_device(self.request(), connection))
            self.assertTrue(connection.in_transaction)
            connection.rollback()
            self.assertEqual("LAB-A", connection.execute("SELECT label FROM devices").fetchone()[0])
        self.assertIsNone(self.contact())

    def test_revocation_between_verification_and_contact_does_not_update(self):
        original = routes.verify_device_request
        def revoke(connection, **kwargs):
            device_id = original(connection, **kwargs)
            with app.db() as other:
                other.execute("UPDATE devices SET status='inactive' WHERE device_id=?", (device_id,))
                other.commit()
            return device_id
        path = "/api/client/v1/releases"
        with patch.object(routes, "verify_device_request", side_effect=revoke):
            response = self.client.get(path, headers=self._headers("GET", path))
        self.assertEqual(403, response.status_code)
        self.assertIsNone(self.contact())


class CoordinatorDashboardContactTests(unittest.TestCase):
    setUp = store_fixtures.ClientUpdateStoreTests.setUp
    tearDown = store_fixtures.ClientUpdateStoreTests.tearDown
    _verified_bundle = store_fixtures.ClientUpdateStoreTests._verified_bundle

    def test_missing_invalid_and_timezone_less_contact_is_unknown(self):
        release = self.store.upload(self.verified)
        for value in (None, "", "not-a-time", "2026-09-24T10:00:00", "0001-01-01T00:00:00+14:00"):
            with self.subTest(value=value):
                with self.connection:
                    self.connection.execute("UPDATE devices SET last_seen_at=? WHERE device_id='device-a'", (value,))
                rows = {r["device_id"]: r for r in self.store.dashboard(release.release_id)}
                self.assertEqual("Unknown", rows["device-a"]["status"])
                self.assertEqual("Offline", rows["device-b"]["status"])

    def test_known_contact_preserves_offline_threshold_with_timezone_offsets(self):
        release = self.store.upload(self.verified)
        for value, expected in (("2026-09-24T15:25:00+05:30", "Waiting"),
                                ("2026-09-24T09:54:59Z", "Offline")):
            with self.subTest(value=value), self.connection:
                self.connection.execute("UPDATE devices SET last_seen_at=? WHERE device_id='device-a'", (value,))
                self.assertEqual(expected, self.store.dashboard(release.release_id)[0]["status"])

    def test_missing_contact_does_not_override_reported_update_outcomes(self):
        release = self.store.upload(self.verified)
        with self.connection:
            self.connection.execute("UPDATE devices SET last_seen_at=NULL")
        attempt = "11111111-1111-4111-8111-111111111111"
        for stage, version, expected in (("downloading", "2.0.0", "Downloading"),
                                         ("healthy", "2.1.0", "Updated"),
                                         ("rolled_back", "2.0.0", "Failed")):
            self.store.record_status(release.release_id, "device-a", stage, version, None, attempt_id=attempt)
            self.assertEqual(expected, self.store.dashboard(release.release_id)[0]["status"])


class CoordinatorReportingUiTests(unittest.TestCase):
    def test_pilot_identity_and_unknown_status_are_rendered(self):
        node = os.environ.get("KSAT_NODE") or shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, str(root / "tests/coordinator_reporting_ui.js"), str(root / "static/app.js")],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
