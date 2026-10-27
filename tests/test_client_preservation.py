"""Real authenticated storage across the guarded installer/restart boundary."""
import base64
import hashlib
import json
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import client_install_guard as helper
from ksat.crypto import generate_ed25519_keypair
from ksat.protocol import canonical_json, SubmissionReceipt
from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
from ksat.client.store import ClientStore
from scripts.build_client_update import build_client_update
from tests.test_client_install_guard import ClientInstallGuardTests, FakeService
from tests.test_lab_installer import MemoryTrust


class PreservationTests(ClientInstallGuardTests):
    def pending_with_older_snapshot(self):
        import sqlite3
        original = ClientStore.start_or_resume_attempt
        older = {}
        def start(store, *args, **kwargs):
            backup = self.program_data / "older.sqlite3"
            with closing(sqlite3.connect(backup)) as destination:
                store.connection.backup(destination)
            older["state/client.sqlite3"] = backup.read_bytes()
            older["identity/state-anchor.json"] = (self.root / "identity/state-anchor.json").read_bytes()
            return original(store, *args, **kwargs)
        with patch.object(ClientStore, "start_or_resume_attempt", new=start):
            self.prepare_attempt(submit=True)
        return older

    def test_receipt_rejects_older_coherent_database_and_anchor_without_lost_identity(self):
        from ksat.client.preservation import authenticated_store_ready, verify_receipt, verify_rollback_source
        older = self.pending_with_older_snapshot()
        stage, binding = self.managed_stage()
        self.assertEqual(0, self.run_managed(stage))
        for name, data in older.items():
            (self.root / name).write_bytes(data)
        self.assertTrue(authenticated_store_ready(self.root), "Regression requires independently valid older state")
        digest = hashlib.sha256(b"signed candidate").hexdigest()
        with self.assertRaises(ValueError):
            verify_receipt(self.root, binding, helper.TARGET_VERSION, digest)
        with self.assertRaises(ValueError):
            verify_rollback_source(self.root, binding, digest)

    def test_receipt_rejects_divergent_authenticated_history_with_larger_sequence(self):
        from ksat.client.preservation import authenticated_store_ready, verify_receipt
        older = self.pending_with_older_snapshot()
        stage, binding = self.managed_stage()
        self.assertEqual(0, self.run_managed(stage))
        baseline_sequence = json.loads((self.root / "identity/state-anchor.json").read_bytes())["sequence"]
        for name, data in older.items():
            (self.root / name).write_bytes(data)
        with self.open_store() as store:
            for _ in range(baseline_sequence + 1):
                with store._transaction():
                    pass
        self.assertTrue(authenticated_store_ready(self.root))
        self.assertGreater(json.loads((self.root / "identity/state-anchor.json").read_bytes())["sequence"], baseline_sequence)
        with self.assertRaises(ValueError):
            verify_receipt(self.root, binding, helper.TARGET_VERSION, hashlib.sha256(b"signed candidate").hexdigest())

    def test_receipt_rejects_lost_attempt_even_with_valid_descendant_journal(self):
        from ksat.client.preservation import authenticated_store_ready, verify_receipt
        self.prepare_attempt(submit=True)
        stage, binding = self.managed_stage()
        self.assertEqual(0, self.run_managed(stage))
        with self.open_store() as store:
            with store._transaction() as connection:
                connection.execute("DELETE FROM submission_outbox")
                connection.execute("DELETE FROM local_responses")
                connection.execute("DELETE FROM local_integrity_events")
                connection.execute("DELETE FROM local_attempts")
        self.assertTrue(authenticated_store_ready(self.root))
        with self.assertRaises(ValueError):
            verify_receipt(self.root, binding, helper.TARGET_VERSION, hashlib.sha256(b"signed candidate").hexdigest())

    def test_receipt_allows_authenticated_retry_and_intervention_without_losing_answers(self):
        from ksat.client.preservation import verify_receipt
        self.prepare_attempt(submit=True)
        stage, binding = self.managed_stage()
        self.assertEqual(0, self.run_managed(stage))
        with self.open_store() as store:
            pending = store.pending_submissions()[0]
            store.record_retry(pending.attempt_id, next_attempt_at=datetime.now(timezone.utc), last_error="offline")
            store.require_faculty_intervention(pending.attempt_id, last_error="attempt_identity_mismatch")
        verify_receipt(self.root, binding, helper.TARGET_VERSION, hashlib.sha256(b"signed candidate").hexdigest())

    def test_receipt_without_continuity_proof_cannot_be_used_as_capture(self):
        from ksat.client.preservation import verify_receipt
        self.prepare_attempt(submit=True)
        stage, binding = self.managed_stage()
        self.assertEqual(0, self.run_managed(stage))
        path = self.root / "updates/preservation-receipt.json"
        receipt = json.loads(path.read_bytes())
        receipt["continuity"] = None
        path.write_bytes(canonical_json(receipt))
        with self.assertRaises(ValueError):
            verify_receipt(self.root, binding, helper.TARGET_VERSION, hashlib.sha256(b"signed candidate").hexdigest())

    def managed_stage(self):
        updates = self.root / "updates"
        updates.mkdir(exist_ok=True)
        installer = updates / "candidate-installer.exe"
        installer.write_bytes(b"signed candidate")
        private, public = generate_ed25519_keypair()
        key = self.program_data / "signing-key"
        key.write_bytes(base64.b64decode(private))
        bundle = updates / "bundle.ksat-client-update"
        release = str(uuid.uuid4())
        build_client_update(installer=installer, version=helper.TARGET_VERSION,
            minimum_source_version="2.1.1", publisher="CN=KSIT", private_key_file=key,
            output=bundle, release_notes="Preservation", release_id=release,
            published_at=datetime.now(timezone.utc), authenticode_verifier=lambda *_: None)
        request = dict(format_version=1, release_id=release, attempt_id=str(uuid.uuid4()),
                       target_version=helper.TARGET_VERSION, bundle_path=str(bundle))
        (updates / "install-request.json").write_bytes(canonical_json(request))
        binding = {k: request[k] for k in ("release_id", "attempt_id", "target_version")}
        binding["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
        (updates / "update-journal.json").write_bytes(canonical_json(dict(
            binding, format_version=2, stage="installing", installed_version=None)))
        stage = self.program_data / "managed-stage"
        stage.mkdir()
        (stage / "install-context.json").write_bytes(canonical_json(dict(
            installer=str(installer), interactive_install=False)))
        protection = patch("client_install_guard.require_protected_directory")
        protection.start()
        self.addCleanup(protection.stop)
        # External publisher trust is replaced; signed bundle parsing remains real.
        self.public = public
        return stage, binding

    def run_managed(self, stage, *, mutate=None, service=None):
        def parent_alive():
            status = json.loads((stage / "status.json").read_bytes()) if (stage / "status.json").exists() else {}
            if status.get("state") == "configured":
                if mutate:
                    mutate()
                action = "commit"
            else:
                action = "configure"
            (stage / "control.json").write_bytes(canonical_json({"action": action}))
            return True
        with patch("client_install_guard.WindowsTrustStore", return_value=MemoryTrust()), \
             patch("ksat.update_protocol.load_update_public_key", return_value=self.public), \
             patch("ksat.windows_authenticode.verify_authenticode", return_value=None):
            return helper.run_guard_session(self.program_data, stage, parent_alive,
                service=service or FakeService(running=True), profile=self.profile)

    def test_managed_pending_install_emits_bound_receipt_without_state_writes(self):
        self.prepare_attempt(submit=True)
        stage, binding = self.managed_stage()
        protected = {p: (self.root / p).read_bytes() for p in
                     ("identity/device-key.bin", "identity/state-anchor.json", "state/client.sqlite3", "client-config.json")}
        self.assertEqual(0, self.run_managed(stage))
        for path, data in protected.items():
            self.assertEqual(data, (self.root / path).read_bytes())
        receipt = json.loads((self.root / "updates/preservation-receipt.json").read_bytes())
        self.assertEqual("verified", receipt["stage"])
        for key, value in binding.items():
            self.assertEqual(value, receipt[key])

    def test_installer_change_to_protected_bytes_cannot_commit(self):
        self.prepare_attempt(submit=True)
        stage, _ = self.managed_stage()
        service = FakeService(running=True)
        def mutate():
            (self.root / "coordinator-url.json").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.run_managed(stage, mutate=mutate, service=service)
        self.assertTrue(service.running)
        receipt = json.loads((self.root / "updates/preservation-receipt.json").read_bytes())
        self.assertEqual("baseline", receipt["stage"])

    def test_legacy_unattended_upgrade_requires_interactive_bootstrap(self):
        self.install()
        stage, _ = self.managed_stage()
        (self.root / "updates/update-journal.json").write_bytes(b'{"stage":"installing"}')
        service = FakeService(running=True)
        self.assertEqual(1, self.run_managed(stage, service=service))
        self.assertEqual([], service.actions)
        status = json.loads((stage / "status.json").read_bytes())
        self.assertEqual("interactive_bootstrap_required", status["diagnostic_code"])

    def open_store(self):
        identity = DeviceIdentityStore(self.root / "identity").load_existing()
        return ClientStore(self.root / "state/client.sqlite3", integrity_key=derive_state_integrity_key(identity),
                           integrity_anchor_path=self.root / "identity/state-anchor.json")

    def test_baseline_is_taken_after_service_quiesces_and_holds_lifecycle_lock(self):
        from ksat.client.preservation import fingerprints
        from client_app import ClientProcessLock
        from ksat.coordinator.process_lock import CoordinatorLockHeld
        self.prepare_attempt(submit=True)
        stage, _ = self.managed_stage()
        before = fingerprints(self.root)
        test = self
        class WritingService(FakeService):
            def stop(self):
                with test.open_store() as store:
                    pending = store.pending_submissions()[0]
                    store.record_retry(pending.attempt_id, next_attempt_at=datetime.now(timezone.utc), last_error="offline")
                super().stop()
        configure = helper.configure_installation
        def checked_configure(*args, **kwargs):
            with self.assertRaises(CoordinatorLockHeld):
                with ClientProcessLock(self.root / "state"):
                    pass
            return configure(*args, **kwargs)
        with patch.object(helper, "configure_installation", side_effect=checked_configure):
            self.assertEqual(0, self.run_managed(stage, service=WritingService(running=True)))
        receipt = json.loads((self.root / "updates/preservation-receipt.json").read_bytes())
        self.assertNotEqual(before, receipt["files"])
        self.assertEqual(fingerprints(self.root), receipt["files"])

    def test_real_restart_acknowledgement_passes_health_but_bad_binding_and_tamper_fail(self):
        from ksat.client.updater import HealthVerifier
        from ksat.client.preservation import authenticated_store_ready
        self.prepare_attempt(submit=True)
        stage, binding = self.managed_stage()
        self.assertEqual(0, self.run_managed(stage))
        health = HealthVerifier(lambda: {"version": helper.TARGET_VERSION}, lambda: authenticated_store_ready(self.root))
        digest = hashlib.sha256(b"signed candidate").hexdigest()
        self.assertTrue(health.verify_preserved(helper.TARGET_VERSION, self.root, binding, digest))
        anchor = (self.root / "identity/state-anchor.json").read_bytes()
        with self.open_store() as store:
            pending = store.pending_submissions()[0]
            record = store.load_attempt(pending.attempt_id)
            receipt = SubmissionReceipt(attempt_id=pending.attempt_id, accepted_at=datetime.now(timezone.utc),
                score=0, total_questions=len(record.question_order), attempted=0, percentage=0.0, violations=0)
            store.acknowledge(pending.attempt_id, receipt)
            self.assertEqual([], store.pending_submissions())
        self.assertNotEqual(anchor, (self.root / "identity/state-anchor.json").read_bytes())
        self.assertTrue(health.verify_preserved(helper.TARGET_VERSION, self.root, binding, digest))
        for key in ("release_id", "attempt_id", "target_version", "bundle_sha256"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                health.verify_preserved(helper.TARGET_VERSION, self.root, dict(binding, **{key: "wrong"}), digest)
        with self.assertRaises(ValueError):
            health.verify_preserved(helper.TARGET_VERSION, self.root, binding, "a" * 64)
        (self.root / "identity/state-anchor.json").write_bytes(b"{}")
        with self.assertRaises(ValueError):
            health.verify_preserved(helper.TARGET_VERSION, self.root, binding, digest)

    def test_managed_guard_blocks_active_work_and_wrong_installer_before_stop(self):
        self.prepare_attempt()
        stage, _ = self.managed_stage()
        service = FakeService(running=True)
        self.assertEqual(1, self.run_managed(stage, service=service))
        self.assertEqual("active_attempt", json.loads((stage / "status.json").read_bytes())["diagnostic_code"])
        (self.root / "updates/candidate-installer.exe").write_bytes(b"different signed installer")
        self.assertEqual(1, self.run_managed(stage, service=service))
        self.assertEqual("interactive_bootstrap_required", json.loads((stage / "status.json").read_bytes())["diagnostic_code"])
        self.assertEqual([], service.actions)

    def test_complete_updater_uses_guard_receipt_and_never_accepts_missing_receipt(self):
        from ksat.client.updater import Updater, HealthVerifier
        from ksat.client.preservation import authenticated_store_ready
        self.prepare_attempt(submit=True)
        stage, _ = self.managed_stage()
        updates = self.root / "updates"
        (updates / "update-journal.json").unlink()
        health = HealthVerifier(lambda: {"version": helper.TARGET_VERSION}, lambda: authenticated_store_ready(self.root))
        test = self
        class Installer:
            calls = 0
            def run(self, path, args, timeout):
                self.calls += 1
                test.assertEqual(0, test.run_managed(stage))
        installer = Installer()
        updater = Updater(updates, self.public, lambda *_: None, None, installer, health,
                          installed_version="2.1.1", data_root=self.root)
        self.assertTrue(updater.run(updates / "install-request.json").success)
        self.assertEqual("healthy", json.loads((updates / "update-journal.json").read_bytes())["stage"])
        request = json.loads((updates / "install-request.json").read_bytes())
        request["attempt_id"] = str(uuid.uuid4())
        (updates / "install-request.json").write_bytes(canonical_json(request))
        installer.run = lambda *_: None
        updater = Updater(updates, self.public, lambda *_: None, None, installer, health,
                          installed_version="2.1.1", data_root=self.root)
        with patch("ksat.client.updater.time.monotonic", side_effect=[0, 61]):
            self.assertFalse(updater.run(updates / "install-request.json").success)
        self.assertEqual("rollback_failed", json.loads((updates / "update-journal.json").read_bytes())["stage"])

    def test_rollback_cannot_rebaseline_state_changed_by_failed_installer(self):
        self.prepare_attempt(submit=True)
        stage, binding = self.managed_stage()
        def mutate():
            with self.open_store() as store:
                pending = store.pending_submissions()[0]
                store.record_retry(pending.attempt_id, next_attempt_at=datetime.now(timezone.utc), last_error="changed by installer")
        with self.assertRaises(ValueError):
            self.run_managed(stage, mutate=mutate)
        updates = self.root / "updates"
        cached = updates / "last-known-good/KSATClientSetup-2.1.1.exe"
        cached.parent.mkdir()
        cached.write_bytes(b"signed rollback")
        (stage / "install-context.json").write_bytes(canonical_json(dict(installer=str(cached), interactive_install=False)))
        (stage / "status.json").unlink()
        (updates / "update-journal.json").write_bytes(canonical_json(dict(binding,
            format_version=2, stage="rollback_installing", rollback_version="2.1.1",
            rollback_sha256=hashlib.sha256(cached.read_bytes()).hexdigest(), installed_version=None)))
        service = FakeService(running=True)
        with patch.object(helper, "TARGET_VERSION", "2.1.1"):
            self.assertEqual(1, self.run_managed(stage, service=service))
        self.assertEqual([], service.actions)

    def test_quiescent_wal_is_preserved_and_cannot_be_removed_at_commit(self):
        self.prepare_attempt(submit=True)
        # Materialize a valid crash-style database + WAL + anchor without a live writer.
        with self.open_store() as store:
            store.connection.execute("PRAGMA journal_mode=WAL")
            pending = store.pending_submissions()[0]
            store.record_retry(pending.attempt_id, next_attempt_at=datetime.now(timezone.utc), last_error="offline")
            snapshot = {name: (self.root / name).read_bytes() for name in
                ("state/client.sqlite3", "state/client.sqlite3-wal", "identity/state-anchor.json")}
        for name, data in snapshot.items():
            (self.root / name).write_bytes(data)
        stage, _ = self.managed_stage()
        with self.assertRaises(ValueError):
            self.run_managed(stage, mutate=lambda: (self.root / "state/client.sqlite3-wal").unlink())
        receipt = json.loads((self.root / "updates/preservation-receipt.json").read_bytes())
        self.assertEqual(hashlib.sha256(snapshot["state/client.sqlite3-wal"]).hexdigest(), receipt["files"]["state/client.sqlite3-wal"])
        self.assertEqual("baseline", receipt["stage"])

