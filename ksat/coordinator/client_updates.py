"""Coordinator persistence and state transitions for managed client updates."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ksat.protocol import canonical_json
from ksat.update_protocol import ClientUpdateManifest, VerifiedClientUpdate, compare_versions


class UpdateStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClientUpdateRelease:
    release_id: str
    client_version: str
    state: str
    manifest: ClientUpdateManifest
    bundle_filename: str
    bundle_sha256: str
    bundle_size: int
    pilot_device_id: str | None
    created_at: str
    published_at: str | None
    withdrawn_at: str | None


_STAGES = frozenset({
    "waiting", "downloading", "verifying", "installing", "restarting",
    "healthy", "failed", "rolled_back",
})
_STAGE_ORDER = {
    "waiting": 0, "downloading": 1, "verifying": 2, "installing": 3,
    "restarting": 4, "healthy": 5, "failed": 5, "rolled_back": 6,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Update timestamp must include a time zone.")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _uuid(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{label} is invalid.") from error
    if str(parsed) != value:
        raise ValueError(f"{label} is invalid.")
    return value


class ClientUpdateStore:
    def __init__(
        self,
        connection: sqlite3.Connection,
        storage_root: Path,
        *,
        now: Callable[[], datetime] = _utc_now,
        offline_after: timedelta = timedelta(minutes=5),
    ) -> None:
        self.connection = connection
        self.storage_root = Path(storage_root).resolve()
        self.now = now
        self.offline_after = offline_after

    def _release_from_row(self, row: sqlite3.Row) -> ClientUpdateRelease:
        try:
            manifest = ClientUpdateManifest.model_validate_json(row["manifest_json"], strict=True)
        except Exception as error:
            raise ValueError("Stored client update manifest is invalid.") from error
        return ClientUpdateRelease(
            release_id=row["release_id"], client_version=row["client_version"],
            state=row["state"], manifest=manifest, bundle_filename=row["bundle_filename"],
            bundle_sha256=row["bundle_sha256"], bundle_size=row["bundle_size"],
            pilot_device_id=row["pilot_device_id"], created_at=row["created_at"],
            published_at=row["published_at"], withdrawn_at=row["withdrawn_at"],
        )

    def release(self, release_id: str) -> ClientUpdateRelease:
        _uuid(release_id, "Client update release identifier")
        row = self.connection.execute(
            "SELECT * FROM client_update_releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown client update release: {release_id}")
        return self._release_from_row(row)

    def bundle_path(self, release_id: str) -> Path:
        release = self.release(release_id)
        candidate = (self.storage_root / release.bundle_filename).resolve()
        if not candidate.is_relative_to(self.storage_root) or candidate.name != "bundle.ksat-client-update":
            raise ValueError("Stored client update path is invalid.")
        return candidate

    def upload(self, verified: VerifiedClientUpdate) -> ClientUpdateRelease:
        source = Path(verified.bundle_path)
        try:
            source_bytes = source.read_bytes()
        except OSError as error:
            raise ValueError("Verified client update bundle is unavailable.") from error
        if (
            len(source_bytes) != verified.bundle_size
            or hashlib.sha256(source_bytes).hexdigest() != verified.bundle_sha256
        ):
            raise ValueError("Verified client update bundle changed before storage.")
        release_id = verified.manifest.release_id
        _uuid(release_id, "Client update release identifier")
        existing = self.connection.execute(
            "SELECT * FROM client_update_releases WHERE release_id=? OR client_version=?",
            (release_id, verified.manifest.client_version),
        ).fetchone()
        if existing is not None:
            same = (
                existing["release_id"] == release_id
                and existing["client_version"] == verified.manifest.client_version
                and existing["bundle_sha256"] == verified.bundle_sha256
                and existing["manifest_json"] == canonical_json(verified.manifest).decode("utf-8")
            )
            if same:
                return self._release_from_row(existing)
            raise UpdateStateError("Client update release identity or version already exists.")

        self.storage_root.mkdir(parents=True, exist_ok=True)
        final_directory = self.storage_root / release_id
        if final_directory.exists():
            raise UpdateStateError("Client update storage already exists for this release.")
        staging = Path(tempfile.mkdtemp(prefix=f".{release_id}.", dir=self.storage_root))
        try:
            staged_bundle = staging / "bundle.ksat-client-update"
            with staged_bundle.open("xb") as output:
                output.write(source_bytes)
                output.flush()
                os.fsync(output.fileno())
            if hashlib.sha256(staged_bundle.read_bytes()).hexdigest() != verified.bundle_sha256:
                raise OSError("Staged client update verification failed.")
            os.replace(staging, final_directory)
            with self.connection:
                self.connection.execute(
                    """INSERT INTO client_update_releases
                       (release_id,client_version,state,manifest_json,bundle_filename,
                        bundle_sha256,bundle_size,pilot_device_id,created_at,published_at,withdrawn_at)
                       VALUES (?,?,'uploaded',?,?,?,?,NULL,?,NULL,NULL)""",
                    (
                        release_id, verified.manifest.client_version,
                        canonical_json(verified.manifest).decode("utf-8"),
                        f"{release_id}/bundle.ksat-client-update", verified.bundle_sha256,
                        verified.bundle_size, _iso(self.now()),
                    ),
                )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            if final_directory.exists() and self.connection.execute(
                "SELECT 1 FROM client_update_releases WHERE release_id=?", (release_id,)
            ).fetchone() is None:
                shutil.rmtree(final_directory, ignore_errors=True)
            raise
        return self.release(release_id)

    def select_pilot(self, release_id: str, device_id: str) -> ClientUpdateRelease:
        release = self.release(release_id)
        if release.state != "uploaded":
            raise UpdateStateError("Only an uploaded release can enter pilot state.")
        device = self.connection.execute(
            "SELECT status FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if device is None or device["status"] != "active":
            raise UpdateStateError("Pilot device must be actively enrolled.")
        with self.connection:
            self.connection.execute(
                "UPDATE client_update_releases SET state='pilot',pilot_device_id=? WHERE release_id=? AND state='uploaded'",
                (device_id, release_id),
            )
        return self.release(release_id)

    def publish(self, release_id: str) -> ClientUpdateRelease:
        release = self.release(release_id)
        if release.state != "pilot" or release.pilot_device_id is None:
            raise UpdateStateError("Client update must complete its pilot before publication.")
        status = self.connection.execute(
            """SELECT stage,installed_version FROM client_update_device_status
               WHERE release_id=? AND device_id=?""",
            (release_id, release.pilot_device_id),
        ).fetchone()
        if status is None or status["stage"] != "healthy" or status["installed_version"] != release.client_version:
            raise UpdateStateError("The selected pilot has not passed the exact release health check.")
        with self.connection:
            self.connection.execute(
                "UPDATE client_update_releases SET state='published',published_at=? WHERE release_id=? AND state='pilot'",
                (_iso(self.now()), release_id),
            )
        return self.release(release_id)

    def withdraw(self, release_id: str) -> ClientUpdateRelease:
        release = self.release(release_id)
        if release.state == "withdrawn":
            return release
        with self.connection:
            self.connection.execute(
                "UPDATE client_update_releases SET state='withdrawn',withdrawn_at=? WHERE release_id=?",
                (_iso(self.now()), release_id),
            )
        return self.release(release_id)

    def record_status(
        self,
        release_id: str,
        device_id: str,
        stage: str,
        installed_version: str,
        diagnostic_code: str | None,
        *,
        attempt_id: str,
    ) -> dict:
        release = self.release(release_id)
        _uuid(attempt_id, "Client update attempt identifier")
        if stage not in _STAGES:
            raise ValueError("Client update stage is invalid.")
        compare_versions(installed_version, installed_version)
        device = self.connection.execute(
            "SELECT status FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if device is None or device["status"] != "active":
            raise UpdateStateError("Client update status device is not active.")
        existing = self.connection.execute(
            "SELECT * FROM client_update_device_status WHERE release_id=? AND device_id=?",
            (release_id, device_id),
        ).fetchone()
        if existing is not None and existing["attempt_id"] != attempt_id and existing["stage"] not in {"failed", "rolled_back"}:
            raise UpdateStateError("Client update status belongs to a stale attempt.")
        if existing is not None and existing["attempt_id"] == attempt_id:
            if _STAGE_ORDER[stage] < _STAGE_ORDER[existing["stage"]]:
                raise UpdateStateError("Client update stage cannot move backwards.")
            if (
                stage == existing["stage"]
                and installed_version == existing["installed_version"]
                and diagnostic_code == existing["diagnostic_code"]
            ):
                return dict(existing)
        if stage == "healthy" and installed_version != release.client_version:
            raise UpdateStateError("Healthy status must report the exact target version.")
        with self.connection:
            self.connection.execute(
                """INSERT INTO client_update_device_status
                   (release_id,device_id,stage,installed_version,diagnostic_code,attempt_id,reported_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(release_id,device_id) DO UPDATE SET
                     stage=excluded.stage,installed_version=excluded.installed_version,
                     diagnostic_code=excluded.diagnostic_code,attempt_id=excluded.attempt_id,
                     reported_at=excluded.reported_at""",
                (release_id, device_id, stage, installed_version, diagnostic_code, attempt_id, _iso(self.now())),
            )
        return dict(self.connection.execute(
            "SELECT * FROM client_update_device_status WHERE release_id=? AND device_id=?",
            (release_id, device_id),
        ).fetchone())

    def policy_for_device(self, device_id: str, installed_version: str) -> dict | None:
        compare_versions(installed_version, installed_version)
        rows = self.connection.execute(
            """SELECT * FROM client_update_releases
               WHERE state='published' OR (state='pilot' AND pilot_device_id=?)""",
            (device_id,),
        ).fetchall()
        eligible = [self._release_from_row(row) for row in rows]
        eligible = [item for item in eligible if compare_versions(item.client_version, installed_version) > 0]
        if not eligible:
            return None
        release = max(eligible, key=lambda item: tuple(int(part) for part in item.client_version.split(".")))
        return {
            "release_id": release.release_id,
            "client_version": release.client_version,
            "minimum_source_version": release.manifest.minimum_source_version,
            "bundle_sha256": release.bundle_sha256,
            "bundle_size": release.bundle_size,
            "manifest": release.manifest.model_dump(mode="json"),
            "state": release.state,
        }

    def dashboard(self, release_id: str) -> list[dict]:
        release = self.release(release_id)
        rows = self.connection.execute(
            """SELECT d.device_id,d.label,d.last_seen_at,s.stage,s.installed_version,
                      s.diagnostic_code,s.attempt_id,s.reported_at
               FROM devices d LEFT JOIN client_update_device_status s
                 ON s.device_id=d.device_id AND s.release_id=?
               WHERE d.status='active' ORDER BY d.label,d.device_id""",
            (release_id,),
        ).fetchall()
        cutoff = self.now().astimezone(timezone.utc) - self.offline_after
        result = []
        for row in rows:
            last_seen = datetime.fromisoformat(row["last_seen_at"]) if row["last_seen_at"] else None
            offline = last_seen is None or last_seen.astimezone(timezone.utc) < cutoff
            if row["stage"] == "healthy" and row["installed_version"] == release.client_version:
                label = "Updated"
            elif row["stage"] in {"downloading", "verifying", "installing", "restarting"}:
                label = "Downloading"
            elif row["stage"] in {"failed", "rolled_back"}:
                label = "Failed"
            elif offline:
                label = "Offline"
            else:
                label = "Waiting"
            result.append({**dict(row), "status": label})
        return result


__all__ = ["ClientUpdateRelease", "ClientUpdateStore", "UpdateStateError"]
