"""Durable, fail-closed client update download and staging."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from ksat.protocol import canonical_json
from ksat.coordinator.process_lock import CoordinatorProcessLock, CoordinatorLockHeld
from ksat.update_protocol import parse_client_update, compare_versions


UpdateStage = Literal[
    "current", "required", "downloading", "verifying", "ready_to_install",
    "deferred_active_attempt", "deferred_pending_submission", "failed",
    "installing", "unavailable", "recovery_required",
]


@dataclass(frozen=True, slots=True)
class UpdateSnapshot:
    release_id: str | None
    target_version: str | None
    stage: UpdateStage
    downloaded_bytes: int
    diagnostic_code: str | None


class _UpdateProcessLock(CoordinatorProcessLock):
    lock_filename = ".update.lock"
    lock_held_message = "The client update directory is already in use."


class ClientUpdateManager:
    def __init__(
        self,
        root: Path,
        coordinator,
        installed_version: str,
        update_public_key_b64: str,
        authenticode_verifier,
        *,
        disk_space_probe: Callable[[Path], int] | None = None,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.coordinator = coordinator
        self.installed_version = installed_version
        self.update_public_key_b64 = update_public_key_b64
        self.authenticode_verifier = authenticode_verifier
        self.disk_space_probe = disk_space_probe or (lambda path: shutil.disk_usage(path).free)
        self.random_source = random_source
        self.state_path = self.root / "client_update_state.json"
        self.partial_path = self.root / "bundle.partial"
        self.bundle_path = self.root / "bundle.ksat-client-update"
        self.install_request_path = self.root / "install-request.json"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _empty_state(diagnostic_code=None) -> dict:
        return {
            "release_id": None, "target_version": None, "stage": "current",
            "downloaded_bytes": 0, "diagnostic_code": diagnostic_code,
            "bundle_sha256": None, "bundle_size": 0, "attempt_id": None,
        }

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            with _UpdateProcessLock(self.root):
                yield

    def _read_state(self) -> dict:
        if not self.state_path.is_file():
            return self._empty_state()
        try:
            value = json.loads(self.state_path.read_bytes())
            required = {"release_id","target_version","stage","downloaded_bytes","diagnostic_code","bundle_sha256","bundle_size","attempt_id"}
            if not isinstance(value, dict) or set(value) != required:
                raise ValueError
            return value
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return self._empty_state("state_invalid")

    def _write_state(self, value: dict) -> None:
        data = canonical_json(value)
        descriptor, name = tempfile.mkstemp(prefix=".update-state-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _snapshot(value: dict) -> UpdateSnapshot:
        return UpdateSnapshot(
            release_id=value["release_id"], target_version=value["target_version"],
            stage=value["stage"], downloaded_bytes=int(value["downloaded_bytes"]),
            diagnostic_code=value["diagnostic_code"],
        )

    def snapshot(self) -> UpdateSnapshot:
        with self._locked():
            return self._snapshot(self._read_state())

    def _report(self, state: dict, stage: str, diagnostic_code: str | None = None) -> bool:
        if not state.get("release_id"):
            return False
        try:
            self.coordinator.report_update_status(
                state["release_id"], stage=stage, installed_version=self.installed_version,
                diagnostic_code=diagnostic_code,
                attempt_id=state["attempt_id"] or str(uuid.uuid4()),
            )
            return True
        except Exception:
            return False

    def _install_progress(self, current: dict) -> UpdateSnapshot:
        from ksat.client.updater import UpdaterLock
        try:
            with UpdaterLock(self.root):
                age = time.time() - self.install_request_path.stat().st_mtime
                if age > 120:
                    return self._snapshot({**current, 'stage': 'recovery_required',
                        'diagnostic_code': 'update_interrupted'})
        except CoordinatorLockHeld:
            pass
        except OSError:
            return self._snapshot({**current, 'stage': 'recovery_required',
                'diagnostic_code': 'update_interrupted'})
        return self._snapshot({**current, 'stage': 'installing'})

    def _archive_failed_attempt(self, current: dict) -> dict:
        from ksat.client.updater import UpdaterLock
        with UpdaterLock(self.root):
            archive = self.root / 'previous-attempts' / str(uuid.uuid4())
            archive.mkdir(parents=True)
            for path in (self.state_path, self.install_request_path, self.root/'update-journal.json'):
                if path.is_file():
                    data = path.read_bytes()
                    (archive/path.name).write_bytes(data)
                    if (archive/path.name).read_bytes() != data:
                        raise ValueError('Update evidence copy failed')
            (self.root/'update-journal.json').unlink(missing_ok=True)
            self.install_request_path.unlink(missing_ok=True)
            current.update(stage='required', attempt_id=str(uuid.uuid4()), diagnostic_code=None)
            self._write_state(current)
            return current

    def _outcome(self, current: dict) -> UpdateSnapshot | None:
        """Relay only a verified, release-bound helper outcome, never version alone."""
        journal_path = self.root / 'update-journal.json'
        if not current.get('release_id') or not journal_path.is_file():
            return None
        try:
            if journal_path.stat().st_size > 16384:
                raise ValueError('Oversized update receipt')
            journal = json.loads(journal_path.read_bytes())
            if not isinstance(journal, dict):
                raise ValueError('Invalid update receipt')
            if journal.get('format_version') != 2:
                # Old helpers cannot prove preservation or release/attempt binding.
                # Retain their evidence and allow normal client use after an
                # interactive upgrade. Never turn this into a healthy report.
                if compare_versions(self.installed_version, current['target_version']) < 0:
                    return self._snapshot({**current, 'stage': 'recovery_required',
                        'diagnostic_code': 'legacy_update_receipt_unavailable'})
                archive = self.root / 'legacy-evidence' / str(uuid.uuid4())
                archive.mkdir(parents=True)
                for path in (self.bundle_path, self.state_path, self.install_request_path, journal_path):
                    if path.is_file():
                        shutil.copyfile(path, archive / path.name)
                        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256((archive / path.name).read_bytes()).digest():
                            raise ValueError('Legacy evidence copy failed')
                self._write_state(self._empty_state('legacy_update_receipt_unavailable'))
                return None
            for key in ('release_id', 'attempt_id', 'target_version', 'bundle_sha256'):
                if not current.get(key) or journal.get(key) != current[key]:
                    raise ValueError('Update receipt identity mismatch')
            stage = journal.get('stage')
            if stage not in {'healthy', 'rolled_back', 'rollback_failed'}:
                return self._install_progress(current)
            verified = parse_client_update(self.bundle_path, self.update_public_key_b64, self.authenticode_verifier)
            if (verified.bundle_sha256 != current['bundle_sha256']
                    or verified.manifest.release_id != current['release_id']
                    or verified.manifest.client_version != current['target_version']
                    or (stage != 'rollback_failed' and journal.get('installed_version') != self.installed_version)
                    or (stage == 'healthy' and self.installed_version != current['target_version'])):
                raise ValueError('Update receipt does not match installed release')
            report_stage = 'failed' if stage == 'rollback_failed' else stage
            diagnostic = None if stage == 'healthy' else 'update_install_failed'
            if not self._report(current, report_stage, diagnostic):
                return self._snapshot({**current, 'stage': 'current' if stage == 'healthy' else 'failed',
                    'diagnostic_code': 'outcome_report_pending'})
            if stage == 'healthy':
                self._write_state(self._empty_state())
                return None
            current.update(stage='recovery_required' if stage == 'rollback_failed' else 'failed', diagnostic_code=diagnostic)
            self._write_state(current)
            return self._snapshot(current)
        except (ValueError, OSError, KeyError, TypeError):
            return self._snapshot({**current, 'stage': 'recovery_required',
                'diagnostic_code': 'update_receipt_invalid'})

    def check(self, *, active_attempt: bool = False, pending_submission: bool = False,
              retry: bool = False) -> UpdateSnapshot:
        with self._locked():
            current = self._read_state()
            outcome = self._outcome(current)
            if outcome is not None:
                if retry and outcome.stage == 'failed' and outcome.diagnostic_code == 'update_install_failed':
                    try:
                        self._archive_failed_attempt(self._read_state())
                    except (OSError, ValueError, CoordinatorLockHeld):
                        return outcome
                else:
                    return outcome
            current = self._read_state()
            if current['stage'] == 'installing':
                return self._install_progress(current)
            if active_attempt:
                current["stage"] = "deferred_active_attempt"; current["diagnostic_code"] = None
                self._write_state(current); return self._snapshot(current)
            # Completed submissions remain in authenticated storage through update.
            # Only an actual active test prevents replacement.
            try:
                policy = self.coordinator.update_policy(self.installed_version)
            except Exception:
                if current["stage"] not in {"required","downloading","verifying","ready_to_install"}:
                    current = self._empty_state("coordinator_unavailable")
                else:
                    current["diagnostic_code"] = "coordinator_unavailable"
                current['stage'] = 'unavailable'
                self._write_state(current); return self._snapshot(current)
            if policy is None:
                self.partial_path.unlink(missing_ok=True)
                notice = current.get('diagnostic_code') if current.get('diagnostic_code') == 'legacy_update_receipt_unavailable' else None
                current = self._empty_state(notice); self._write_state(current); return self._snapshot(current)
            release_id = policy.get("release_id")
            if current.get('release_id') == release_id and current['stage'] == 'failed' and not retry:
                return self._snapshot(current)
            if current.get("release_id") != release_id:
                self.partial_path.unlink(missing_ok=True); self.bundle_path.unlink(missing_ok=True); self.install_request_path.unlink(missing_ok=True)
                (self.root / 'update-journal.json').unlink(missing_ok=True)
                current = self._empty_state()
            current.update({
                "release_id": release_id, "target_version": policy.get("client_version"),
                "stage": "required" if current["stage"] not in {"downloading","verifying","ready_to_install"} else current["stage"],
                "downloaded_bytes": self.partial_path.stat().st_size if self.partial_path.is_file() else 0,
                "diagnostic_code": None, "bundle_sha256": policy.get("bundle_sha256"),
                "bundle_size": int(policy.get("bundle_size", 0)),
                "attempt_id": current.get("attempt_id") or str(uuid.uuid4()),
            })
            self._write_state(current); return self._snapshot(current)

    def _fail(self, state: dict, code: str, *, remove_download: bool = True) -> UpdateSnapshot:
        if remove_download:
            self.partial_path.unlink(missing_ok=True); self.bundle_path.unlink(missing_ok=True)
        state.update(stage="failed", diagnostic_code=code, downloaded_bytes=0)
        self._write_state(state); self._report(state, "failed", code)
        return self._snapshot(state)

    def download(self) -> UpdateSnapshot:
        with self._locked():
            state = self._read_state()
            if state["stage"] not in {"required","downloading","verifying","failed"} or not state["release_id"]:
                raise ValueError("No client update is ready to download.")
            expected_size = int(state["bundle_size"])
            offset = self.partial_path.stat().st_size if self.partial_path.is_file() else 0
            if offset > expected_size:
                self.partial_path.unlink(missing_ok=True); offset = 0
            if self.disk_space_probe(self.root) < max(0, expected_size - offset):
                return self._fail(state, "disk_full")
            state.update(stage="downloading", downloaded_bytes=offset, diagnostic_code=None); self._write_state(state); self._report(state,"downloading")
            try:
                downloaded, total = self.coordinator.download_update_range(state["release_id"], offset, self.partial_path)
                actual = self.partial_path.stat().st_size
                if total != expected_size or downloaded != actual or actual != expected_size:
                    return self._fail(state, "download_range_invalid")
                state.update(stage="verifying", downloaded_bytes=actual); self._write_state(state); self._report(state,"verifying")
                if hashlib.sha256(self.partial_path.read_bytes()).hexdigest() != state["bundle_sha256"]:
                    return self._fail(state, "bundle_hash_mismatch")
                os.replace(self.partial_path, self.bundle_path)
                verified = parse_client_update(self.bundle_path, self.update_public_key_b64, self.authenticode_verifier)
                if verified.manifest.release_id != state["release_id"] or verified.manifest.client_version != state["target_version"]:
                    return self._fail(state, "bundle_identity_mismatch")
            except OSError as error:
                return self._fail(state, "disk_full" if getattr(error,"errno",None) == 28 else "download_failed")
            except Exception:
                return self._fail(state, "bundle_verification_failed")
            state.update(stage="ready_to_install", downloaded_bytes=expected_size, diagnostic_code=None)
            self._write_state(state); return self._snapshot(state)

    def prepare_install(self) -> Path:
        with self._locked():
            state = self._read_state()
            if state["stage"] != "ready_to_install":
                raise ValueError("Client update is not ready to install.")
            verified = parse_client_update(self.bundle_path, self.update_public_key_b64, self.authenticode_verifier)
            if verified.bundle_sha256 != state["bundle_sha256"] or verified.manifest.release_id != state["release_id"]:
                self._fail(state, "bundle_verification_failed")
                raise ValueError("Client update verification failed.")
            request = {
                "format_version": 1, "release_id": state["release_id"],
                "target_version": state["target_version"], "bundle_path": str(self.bundle_path),
                "attempt_id": state['attempt_id'],
            }
            descriptor, name = tempfile.mkstemp(prefix=".install-request-", dir=self.root)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor,"wb") as stream:
                    stream.write(canonical_json(request)); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, self.install_request_path)
            finally:
                temporary.unlink(missing_ok=True)
            state['stage'] = 'installing'
            self._write_state(state)
            return self.install_request_path

    def launch_failed(self) -> UpdateSnapshot:
        with self._locked():
            state = self._read_state()
            state.update(stage='failed', diagnostic_code='updater_launch_failed')
            self._write_state(state)
            self._report(state, 'failed', state['diagnostic_code'])
            return self._snapshot(state)

    def recover(self) -> UpdateSnapshot:
        with self._locked():
            state = self._read_state()
            if state["stage"] == "ready_to_install":
                try:
                    verified = parse_client_update(
                        self.bundle_path, self.update_public_key_b64, self.authenticode_verifier
                    )
                    if (
                        verified.bundle_sha256 != state["bundle_sha256"]
                        or verified.manifest.release_id != state["release_id"]
                        or verified.manifest.client_version != state["target_version"]
                    ):
                        raise ValueError("Recovered client update identity does not match.")
                except Exception:
                    return self._fail(state, "bundle_verification_failed")
            return self._snapshot(state)

    def next_check_delay(self, base_seconds: float, window_seconds: float) -> float:
        if base_seconds < 0 or window_seconds < 0:
            raise ValueError("Update polling interval is invalid.")
        return base_seconds - window_seconds + (2 * window_seconds * float(self.random_source()))


__all__ = ["ClientUpdateManager", "UpdateSnapshot"]
