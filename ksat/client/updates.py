"""Durable, fail-closed client update download and staging."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from ksat.protocol import canonical_json
from ksat.coordinator.process_lock import CoordinatorProcessLock
from ksat.update_protocol import parse_client_update


UpdateStage = Literal[
    "current", "required", "downloading", "verifying", "ready_to_install",
    "deferred_active_attempt", "deferred_pending_submission", "failed",
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

    def _report(self, state: dict, stage: str, diagnostic_code: str | None = None) -> None:
        if not state.get("release_id"):
            return
        try:
            self.coordinator.report_update_status(
                state["release_id"], stage=stage, installed_version=self.installed_version,
                diagnostic_code=diagnostic_code,
                attempt_id=state["attempt_id"] or str(uuid.uuid4()),
            )
        except Exception:
            pass

    def check(self, *, active_attempt: bool = False, pending_submission: bool = False) -> UpdateSnapshot:
        with self._locked():
            current = self._read_state()
            if active_attempt:
                current["stage"] = "deferred_active_attempt"; current["diagnostic_code"] = None
                self._write_state(current); return self._snapshot(current)
            if pending_submission:
                current["stage"] = "deferred_pending_submission"; current["diagnostic_code"] = None
                self._write_state(current); return self._snapshot(current)
            try:
                policy = self.coordinator.update_policy(self.installed_version)
            except Exception:
                if current["stage"] not in {"required","downloading","verifying","ready_to_install"}:
                    current = self._empty_state("coordinator_unavailable")
                else:
                    current["diagnostic_code"] = "coordinator_unavailable"
                self._write_state(current); return self._snapshot(current)
            if policy is None:
                self.partial_path.unlink(missing_ok=True)
                current = self._empty_state(); self._write_state(current); return self._snapshot(current)
            release_id = policy.get("release_id")
            if current.get("release_id") != release_id:
                self.partial_path.unlink(missing_ok=True); self.bundle_path.unlink(missing_ok=True); self.install_request_path.unlink(missing_ok=True)
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
                "attempt_id": str(uuid.uuid4()),
            }
            descriptor, name = tempfile.mkstemp(prefix=".install-request-", dir=self.root)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor,"wb") as stream:
                    stream.write(canonical_json(request)); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, self.install_request_path)
            finally:
                temporary.unlink(missing_ok=True)
            return self.install_request_path

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
