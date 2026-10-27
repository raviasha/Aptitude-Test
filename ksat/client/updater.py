"""Narrow privileged installer runner with verified rollback."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ksat.client.install_guard import reject_links, version_tuple
from ksat.coordinator.process_lock import CoordinatorProcessLock, CoordinatorLockHeld
from ksat.client.preservation import (atomic_json, load_install_request, read_json,
                                      request_binding, verify_receipt, verify_rollback_source)


_INSTALL_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
_SERVICE = "KSATLabClientAuthority"


class UpdaterLock(CoordinatorProcessLock):
    lock_filename = ".updater.lock"
    lock_held_message = "A client updater is already running."


@dataclass(frozen=True, slots=True)
class UpdateResult:
    success: bool
    stage: str
    diagnostic_code: str | None
    rolled_back: bool


class HealthVerifier:
    def __init__(self, version_probe, store_probe) -> None:
        self.version_probe = version_probe
        self.store_probe = store_probe

    def verify(
        self, expected_version: str,
        identity_digest: str, config_digest: str, state_digest: str,
        current_identity_digest: str, current_config_digest: str, current_state_digest: str,
    ) -> bool:
        value = self.version_probe()
        if value != {"version": expected_version}:
            raise ValueError("Client health version does not match the installed update.")
        if (identity_digest, config_digest, state_digest) != (
            current_identity_digest, current_config_digest, current_state_digest
        ):
            raise ValueError("Client update changed protected device data.")
        if self.store_probe() is not True:
            raise ValueError("Client authenticated store is unreadable.")
        return True

    def verify_preserved(self, expected_version: str, root: Path, binding: dict,
                         installer_sha256: str) -> bool:
        if self.version_probe() != {"version": expected_version}:
            raise ValueError("Client health version does not match the installed update.")
        verify_receipt(root, binding, expected_version, installer_sha256)
        if self.store_probe() is not True:
            raise ValueError("Client authenticated store is unreadable.")
        return True


class Updater:
    def __init__(
        self, protected_root: Path, public_key_b64: str, authenticode_verifier,
        services, installers, health,
        *, installed_version: str, identity_digest: str = "", config_digest: str = "", state_digest: str = "",
        current_digest_probe=None, data_root: Path | None = None,
    ) -> None:
        self.root = Path(protected_root).resolve(strict=True)
        self.public_key_b64 = public_key_b64
        self.authenticode_verifier = authenticode_verifier
        self.services = services
        self.installers = installers
        self.health = health
        self.installed_version = installed_version
        self.data_root = Path(data_root).resolve(strict=True) if data_root is not None else None
        if self.data_root is None and not all((identity_digest, config_digest, state_digest)):
            raise ValueError("Client health requires a preservation root or explicit legacy digests.")
        if self.data_root is not None and self.root != self.data_root / "updates":
            raise ValueError("Preservation and updater storage must belong to the same client.")
        self.binding = None
        self.attempt_path = None
        self.installer_sha256 = None
        self.health_timeout = 60
        self.identity_digest = identity_digest
        self.config_digest = config_digest
        self.state_digest = state_digest
        self.current_digest_probe = current_digest_probe or (
            lambda: (self.identity_digest, self.config_digest, self.state_digest)
        )
        self.journal_path = self.root / "update-journal.json"
        self.last_known_good_path = self.root / "last-known-good" / f"KSATClientSetup-{installed_version}.exe"
        self.predecessor_path = self.root / "last-known-good" / "predecessor.exe"

    def _journal(self, stage: str, diagnostic_code: str | None = None, **context) -> None:
        if self.binding is None:
            raise ValueError("A verified request is required for an update outcome.")
        data = {**self.binding, "format_version": 2, "stage": stage,
                "diagnostic_code": diagnostic_code, "installed_version": None, **context}
        if stage == "rollback_failed" and "installed_version" not in context:
            # Failure is not health. Report a version only if the local endpoint
            # actually answered; never guess source/target when it is unavailable.
            try:
                observed = self.health.version_probe()
                if isinstance(observed, dict) and set(observed) == {"version"}:
                    version_tuple(observed["version"])
                    data["installed_version"] = observed["version"]
            except Exception:
                pass
        # Attempt ledger is durable before the shared latest-outcome pointer.
        if self.attempt_path is not None:
            atomic_json(self.attempt_path, data)
        atomic_json(self.journal_path, data)

    def _load_request(self, request_path: Path):
        try:
            return load_install_request(self.root, request_path, self.public_key_b64, self.authenticode_verifier)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise ValueError("Client update install request is invalid.") from error

    def _extract_installer(self, verified) -> Path:
        destination=self.root / "candidate-installer.exe"
        reject_links(destination)
        with zipfile.ZipFile(verified.bundle_path) as archive:
            data=archive.read(verified.manifest.installer_filename)
        if (len(data) != verified.manifest.installer_size
                or hashlib.sha256(data).hexdigest() != verified.manifest.installer_sha256):
            raise ValueError("Candidate installer changed after bundle verification.")
        descriptor,name=tempfile.mkstemp(prefix=".candidate-",suffix=".exe",dir=self.root)
        temporary=Path(name)
        try:
            with os.fdopen(descriptor,"wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,destination)
        finally:
            temporary.unlink(missing_ok=True)
        self.authenticode_verifier(destination,verified.manifest.authenticode_publisher)
        return destination

    def _verify_health(self, version: str) -> None:
        deadline = time.monotonic() + self.health_timeout
        while True:
            try:
                self._verify_health_once(version)
                return
            except Exception:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(1.0, remaining))

    def _verify_health_once(self, version: str) -> None:
        if self.data_root is not None:
            if self.health.verify_preserved(version, self.data_root, self.binding, self.installer_sha256) is not True:
                raise ValueError("Client health was not verified.")
            return
        current=self.current_digest_probe()
        if self.health.verify(version,self.identity_digest,self.config_digest,self.state_digest,*current) is not True:
            raise ValueError("Client health was not verified.")

    def _rollback(self, publisher: str) -> bool:
        if not self.last_known_good_path.is_file():
            self._journal("rollback_failed", "rollback_unavailable")
            return False
        try:
            if self.data_root is not None:
                verify_rollback_source(self.data_root, self.binding, self.installer_sha256)
            reject_links(self.last_known_good_path)
            self.authenticode_verifier(self.last_known_good_path,publisher)
            self.installer_sha256 = hashlib.sha256(self.last_known_good_path.read_bytes()).hexdigest()
            self._journal("rollback_installing", rollback_version=self.installed_version,
                rollback_sha256=self.installer_sha256)
            self.installers.run(self.last_known_good_path,_INSTALL_ARGS,300)
            self._verify_health(self.installed_version)
            self._journal("rolled_back", "update_install_failed", installed_version=self.installed_version)
            return True
        except Exception:
            self._journal("rollback_failed","rollback_failed")
            return False

    def run(self, request_path: Path) -> UpdateResult:
        reject_links(self.root / UpdaterLock.lock_filename)
        try:
            with UpdaterLock(self.root):
                return self._run_locked(request_path)
        except CoordinatorLockHeld:
            return UpdateResult(False, "updater_busy", "updater_busy", False)

    def _run_locked(self, request_path: Path) -> UpdateResult:
        self.binding = None
        self.attempt_path = None
        try:
            value, verified = self._load_request(request_path)
        except ValueError:
            atomic_json(self.root / "invalid-request-journal.json", dict(
                format_version=2, stage="request_invalid", diagnostic_code="invalid_install_request",
                release_id=None, attempt_id=None, target_version=None, bundle_sha256=None, installed_version=None))
            raise
        self.binding = request_binding(value, verified)
        self.health_timeout = verified.manifest.health_check_timeout_seconds
        attempts = self.root / "attempts"
        reject_links(attempts)
        attempts.mkdir(exist_ok=True)
        self.attempt_path = attempts / f"{value['attempt_id']}.json"
        # Never replay completed OR interrupted attempts; a retry needs a new ID.
        for path in (self.attempt_path, self.journal_path):
            if path.exists():
                previous = read_json(path)
                if previous.get("attempt_id") == value["attempt_id"]:
                    return UpdateResult(False, "request_replayed", "request_replayed", False)
                if path == self.journal_path and previous.get("stage") in {"preparing", "installing", "health_check", "rollback_installing"}:
                    return UpdateResult(False, "unfinished_update", "unfinished_update", False)
        self._journal("preparing")
        try:
            installer = self._extract_installer(verified)
            self.installer_sha256 = verified.manifest.installer_sha256
        except Exception:
            self._journal("rollback_failed", "candidate_verification_failed")
            return UpdateResult(False, "rollback_failed", "candidate_verification_failed", False)
        try:
            # The signed installer's guard owns stop/start, after a race-safe
            # active-work check. Never quiesce the service ahead of that check.
            self._journal("installing")
            self.installers.run(installer,_INSTALL_ARGS,300)
            self._journal("health_check")
            self._verify_health(value["target_version"])
            reject_links(self.last_known_good_path.parent)
            self.last_known_good_path.parent.mkdir(parents=True,exist_ok=True)
            promoted = self.last_known_good_path.parent / f"KSATClientSetup-{value['target_version']}.exe"
            reject_links(promoted)
            descriptor, name = tempfile.mkstemp(prefix=".promoted-", suffix=".exe", dir=promoted.parent)
            os.close(descriptor)
            temporary = Path(name)
            try:
                shutil.copy2(installer, temporary)
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                if self.last_known_good_path.is_file():
                    reject_links(self.predecessor_path)
                    shutil.copy2(self.last_known_good_path, self.predecessor_path)
                os.replace(temporary, promoted)
            finally:
                temporary.unlink(missing_ok=True)
            self._journal("healthy", installed_version=value["target_version"])
            self.last_known_good_path = promoted
            return UpdateResult(True,"healthy",None,False)
        except Exception:
            diagnostic="update_install_failed"
            rolled_back=self._rollback(verified.manifest.authenticode_publisher)
            return UpdateResult(False,"rolled_back" if rolled_back else "rollback_failed",diagnostic,rolled_back)


__all__=["HealthVerifier","Updater","UpdateResult"]
