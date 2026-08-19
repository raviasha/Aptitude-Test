"""Student-only Android and Google Drive extension for the existing engine."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

import app as core


MOBILE_APP_SECRET = os.environ["APTITUDE_MOBILE_APP_SECRET"]
DEFAULT_DRIVE_FOLDER_URL = os.getenv("APTITUDE_DRIVE_FOLDER_URL", "").strip()
DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DOWNLOAD_SAFETY_MARGIN_BYTES = 50 * 1024 * 1024
MOBILE_STUDENT_ID = "ANDROID-LOCAL"
_drive_access_token: Optional[str] = None


class DriveSessionPayload(BaseModel):
    access_token: str = Field(min_length=20)
    email: str = ""
    name: str = ""


class DriveFolderPayload(BaseModel):
    folder_url: str = ""


class ContentProvider(ABC):
    """Boundary between the test engine and a remote content source."""

    @abstractmethod
    def list_banks(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def download_bank(self, file_id: str, destination: Path, maximum_bytes: int) -> int:
        raise NotImplementedError


def folder_id_from_url(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "drive.google.com",
        "www.drive.google.com",
    }:
        return None
    match = re.search(r"/folders/([A-Za-z0-9_-]{10,})", parsed.path)
    if match:
        return match.group(1)
    query_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
    return query_id if re.fullmatch(r"[A-Za-z0-9_-]{10,}", query_id) else None


class GoogleDriveContentProvider(ContentProvider):
    def __init__(self, access_token: str, folder_url: str) -> None:
        folder_id = folder_id_from_url(folder_url)
        if not folder_id:
            raise HTTPException(400, "Enter a valid Google Drive folder link or folder ID.")
        self.access_token = access_token
        self.folder_id = folder_id

    def _open(self, url: str):
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        try:
            return urllib.request.urlopen(request, timeout=45)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise HTTPException(401, "Google authorization expired. Connect your account again.") from error
            if error.code == 403:
                raise HTTPException(403, "This Google account cannot access the configured Drive folder.") from error
            if error.code == 404:
                raise HTTPException(
                    404,
                    "The configured Drive content was not found or this Google account does not have access.",
                ) from error
            raise HTTPException(502, f"Google Drive returned HTTP {error.code}.") from error
        except urllib.error.URLError as error:
            raise HTTPException(503, "Google Drive is unavailable. Check the internet connection and retry.") from error

    def list_banks(self) -> List[Dict[str, Any]]:
        folder_url = (
            "https://www.googleapis.com/drive/v3/files/"
            + urllib.parse.quote(self.folder_id)
            + "?fields=id,name,mimeType&supportsAllDrives=true"
        )
        with self._open(folder_url) as response:
            folder = json.loads(response.read().decode("utf-8"))
        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            raise HTTPException(400, "The configured Drive link does not point to a folder.")

        files: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        query = f"'{self.folder_id}' in parents and trashed = false"
        while True:
            parameters = {
                "q": query,
                "spaces": "drive",
                "pageSize": "100",
                "orderBy": "name",
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum)",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            }
            if page_token:
                parameters["pageToken"] = page_token
            url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(parameters)
            with self._open(url) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for item in payload.get("files", []):
                if str(item.get("name", "")).lower().endswith(".zip"):
                    files.append(
                        {
                            "file_id": item["id"],
                            "name": item["name"],
                            "size": int(item.get("size") or 0),
                            "modified_time": item.get("modifiedTime", ""),
                            "checksum": item.get("md5Checksum", ""),
                        }
                    )
            page_token = payload.get("nextPageToken")
            if not page_token:
                return files

    def download_bank(self, file_id: str, destination: Path, maximum_bytes: int) -> int:
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", file_id):
            raise HTTPException(400, "Invalid Google Drive file ID.")
        url = f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id)}?alt=media&supportsAllDrives=true"
        downloaded = 0
        with self._open(url) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > maximum_bytes:
                    raise HTTPException(413, "The question-bank package exceeds the supported size limit.")
                output.write(chunk)
        return downloaded


def require_mobile(request: Request, signed_in: bool = True) -> Dict[str, str]:
    if request.headers.get("X-Aptitude-Mobile") != MOBILE_APP_SECRET:
        raise HTTPException(403, "This endpoint is available only inside the Android app.")
    if signed_in:
        return core.require_user(request, "student")
    return {}


def ensure_mobile_schema() -> None:
    with core.db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS mobile_settings (
              setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mobile_bank_sources (
              bank_id INTEGER PRIMARY KEY, remote_file_id TEXT NOT NULL UNIQUE,
              remote_modified_time TEXT NOT NULL DEFAULT '', remote_size INTEGER NOT NULL DEFAULT 0,
              storage_mode TEXT NOT NULL, installed_at TEXT NOT NULL,
              FOREIGN KEY(bank_id) REFERENCES question_banks(bank_id)
            );
            CREATE TABLE IF NOT EXISTS mobile_result_snapshots (
              attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, bank_name TEXT NOT NULL,
              test_name TEXT NOT NULL, submitted_at TEXT NOT NULL, score INTEGER NOT NULL,
              total_questions INTEGER NOT NULL, percentage REAL NOT NULL, result_json TEXT NOT NULL
            );
            """
        )
        if DEFAULT_DRIVE_FOLDER_URL:
            connection.execute(
                "INSERT OR IGNORE INTO mobile_settings VALUES ('drive_folder_url', ?)",
                (DEFAULT_DRIVE_FOLDER_URL,),
            )


def drive_folder_url() -> str:
    with core.db() as connection:
        row = connection.execute(
            "SELECT setting_value FROM mobile_settings WHERE setting_key = 'drive_folder_url'"
        ).fetchone()
    return row["setting_value"] if row else DEFAULT_DRIVE_FOLDER_URL


def provider() -> GoogleDriveContentProvider:
    if not _drive_access_token:
        raise HTTPException(401, "Connect a Google account to access remote question banks.")
    return GoogleDriveContentProvider(_drive_access_token, drive_folder_url())


def snapshot_attempts(connection: sqlite3.Connection, bank_id: int) -> None:
    attempts = connection.execute(
        """SELECT a.attempt_id, a.student_id, a.submitted_at, a.score, a.total_questions,
                  a.percentage, t.test_name, b.bank_name
             FROM attempts a
             JOIN tests t ON t.test_id = a.test_id
             JOIN question_banks b ON b.bank_id = t.bank_id
            WHERE t.bank_id = ? AND a.status = 'submitted'""",
        (bank_id,),
    ).fetchall()
    for attempt in attempts:
        result = core.result_for_attempt(connection, attempt["attempt_id"])
        connection.execute(
            """INSERT OR REPLACE INTO mobile_result_snapshots
               (attempt_id, student_id, bank_name, test_name, submitted_at, score,
                total_questions, percentage, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt["attempt_id"], attempt["student_id"], attempt["bank_name"],
                attempt["test_name"], attempt["submitted_at"], attempt["score"],
                attempt["total_questions"], attempt["percentage"], json.dumps(result),
            ),
        )


def purge_bank(bank_id: int) -> Dict[str, Any]:
    with core.db() as connection:
        bank = connection.execute(
            "SELECT bank_name FROM question_banks WHERE bank_id = ?", (bank_id,)
        ).fetchone()
        if not bank:
            raise HTTPException(404, "Question bank not found.")
        active = connection.execute(
            """SELECT COUNT(*) AS count FROM attempts a JOIN tests t ON t.test_id = a.test_id
                WHERE t.bank_id = ? AND a.status = 'in_progress'""",
            (bank_id,),
        ).fetchone()["count"]
        if active:
            raise HTTPException(409, "Finish or submit the active practice session before deleting this bank.")
        snapshot_attempts(connection, bank_id)
        attempt_ids = [
            row["attempt_id"]
            for row in connection.execute(
                "SELECT a.attempt_id FROM attempts a JOIN tests t ON t.test_id = a.test_id WHERE t.bank_id = ?",
                (bank_id,),
            ).fetchall()
        ]
        for attempt_id in attempt_ids:
            connection.execute("DELETE FROM exam_violations WHERE attempt_id = ?", (attempt_id,))
            connection.execute("DELETE FROM responses WHERE attempt_id = ?", (attempt_id,))
            connection.execute("DELETE FROM attempts WHERE attempt_id = ?", (attempt_id,))
        connection.execute("DELETE FROM tests WHERE bank_id = ?", (bank_id,))
        connection.execute("DELETE FROM stimuli WHERE bank_id = ?", (bank_id,))
        connection.execute("DELETE FROM questions WHERE bank_id = ?", (bank_id,))
        connection.execute("DELETE FROM mobile_bank_sources WHERE bank_id = ?", (bank_id,))
        connection.execute("DELETE FROM question_banks WHERE bank_id = ?", (bank_id,))
    asset_directory = core.question_assets_dir() / str(bank_id)
    if asset_directory.is_dir():
        shutil.rmtree(asset_directory)
    return {"deleted": True, "bank_name": bank["bank_name"]}


def catalog_with_status() -> List[Dict[str, Any]]:
    remote = provider().list_banks()
    with core.db() as connection:
        local_rows = connection.execute(
            "SELECT bank_id, remote_file_id, remote_modified_time, storage_mode FROM mobile_bank_sources"
        ).fetchall()
    local = {row["remote_file_id"]: dict(row) for row in local_rows}
    for item in remote:
        installed = local.get(item["file_id"])
        item["bank_id"] = installed["bank_id"] if installed else None
        item["storage_mode"] = installed["storage_mode"] if installed else None
        item["downloaded"] = bool(installed and installed["storage_mode"] == "permanent")
        item["update_available"] = bool(
            installed
            and item["modified_time"]
            and item["modified_time"] != installed["remote_modified_time"]
        )
    return remote


def import_remote_bank(file_id: str, storage_mode: str) -> Dict[str, Any]:
    remote_provider = provider()
    available = {item["file_id"]: item for item in remote_provider.list_banks()}
    metadata = available.get(file_id)
    if not metadata:
        raise HTTPException(404, "That question bank is not available in the configured Drive folder.")
    expected_size = metadata["size"] or core.MAX_PACKAGE_BYTES
    if storage_mode == "permanent":
        required_space = max(expected_size * 3, expected_size + DOWNLOAD_SAFETY_MARGIN_BYTES)
    else:
        required_space = max(expected_size * 2, expected_size + 10 * 1024 * 1024)
    free_space = shutil.disk_usage(core.DATA_DIR).free
    if free_space < required_space:
        raise HTTPException(
            507,
            f"Not enough storage for this {storage_mode} copy. Bank size is about "
            f"{expected_size} bytes and {free_space} bytes are free.",
        )

    download_dir = core.DATA_DIR / "mobile-downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    partial = download_dir / f"{uuid.uuid4().hex}.part"
    try:
        downloaded = remote_provider.download_bank(file_id, partial, core.MAX_PACKAGE_BYTES)
        if metadata["size"] and downloaded != metadata["size"]:
            raise HTTPException(502, "The Drive download was incomplete. Retry the download.")
        bank_name, questions, stimuli = core.parse_v2_package(partial)
        with core.db() as connection:
            existing = connection.execute(
                "SELECT bank_id FROM mobile_bank_sources WHERE remote_file_id = ?", (file_id,)
            ).fetchone()
            name_taken = connection.execute(
                "SELECT 1 FROM question_banks WHERE bank_name = ?", (bank_name,)
            ).fetchone()
        import_name = bank_name
        if existing or name_taken:
            import_name = f"{bank_name} (Drive {uuid.uuid4().hex[:6]})"
        result = core.save_v2_question_bank(import_name, questions, stimuli, metadata["name"])
        new_bank_id = result["bank_id"]
        try:
            if existing:
                purge_bank(existing["bank_id"])
            with core.db() as connection:
                connection.execute(
                    "UPDATE question_banks SET bank_name = ? WHERE bank_id = ?",
                    (bank_name, new_bank_id),
                )
                connection.execute(
                    """INSERT INTO mobile_bank_sources
                       (bank_id, remote_file_id, remote_modified_time, remote_size, storage_mode, installed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        new_bank_id, file_id, metadata["modified_time"], downloaded,
                        storage_mode, core.now(),
                    ),
                )
        except Exception:
            purge_bank(new_bank_id)
            raise
        result.update(
            {
                "bank_name": bank_name,
                "storage_mode": storage_mode,
                "remote_file_id": file_id,
            }
        )
        return result
    finally:
        partial.unlink(missing_ok=True)


@core.app.on_event("startup")
def mobile_startup() -> None:
    ensure_mobile_schema()
    with core.db() as connection:
        temporary_ids = [
            row["bank_id"]
            for row in connection.execute(
                """SELECT m.bank_id FROM mobile_bank_sources m
                   WHERE m.storage_mode = 'temporary'
                     AND NOT EXISTS (
                       SELECT 1 FROM attempts a JOIN tests t ON t.test_id = a.test_id
                       WHERE t.bank_id = m.bank_id AND a.status = 'in_progress'
                     )"""
            ).fetchall()
        ]
    for bank_id in temporary_ids:
        purge_bank(bank_id)


@core.app.get("/api/mobile/config")
def mobile_config() -> Dict[str, Any]:
    value = drive_folder_url()
    return {
        "mobile": True,
        "drive_folder_url": value,
        "drive_configured": bool(folder_id_from_url(value)),
        "maximum_package_bytes": core.MAX_PACKAGE_BYTES,
    }


@core.app.post("/api/mobile/bootstrap")
def mobile_bootstrap(request: Request) -> Dict[str, Any]:
    require_mobile(request, signed_in=False)
    with core.db() as connection:
        connection.execute(
            """INSERT OR IGNORE INTO students
               (student_id, name, password_hash, class, section, created_at)
               VALUES (?, 'Student', 'google-managed-mobile-account', 'Mobile', '', ?)""",
            (MOBILE_STUDENT_ID, core.now()),
        )
        connection.execute("DELETE FROM student_sessions WHERE student_id = ?", (MOBILE_STUDENT_ID,))
        login_token = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO student_sessions VALUES (?, ?, ?)",
            (MOBILE_STUDENT_ID, login_token, core.now()),
        )
    user = {"role": "student", "id": MOBILE_STUDENT_ID, "name": "Student", "login_token": login_token}
    request.session["user"] = user
    return {"user": user}


@core.app.put("/api/mobile/settings/drive-folder")
def save_drive_folder(payload: DriveFolderPayload, request: Request) -> Dict[str, Any]:
    require_mobile(request)
    value = payload.folder_url.strip()
    if value and not folder_id_from_url(value):
        raise HTTPException(400, "Enter a valid Google Drive folder link or folder ID.")
    with core.db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO mobile_settings VALUES ('drive_folder_url', ?)", (value,)
        )
    return {"saved": True, "drive_folder_url": value, "drive_configured": bool(value)}


@core.app.post("/api/mobile/drive/session")
def save_drive_session(payload: DriveSessionPayload, request: Request) -> Dict[str, Any]:
    global _drive_access_token
    require_mobile(request)
    _drive_access_token = payload.access_token
    display_name = payload.name.strip() or payload.email.strip() or "Student"
    with core.db() as connection:
        connection.execute(
            "UPDATE students SET name = ? WHERE student_id = ?", (display_name, MOBILE_STUDENT_ID)
        )
    request.session["user"]["name"] = display_name
    return {"connected": True, "email": payload.email, "name": display_name}


@core.app.delete("/api/mobile/drive/session")
def clear_drive_session(request: Request) -> Dict[str, bool]:
    global _drive_access_token
    require_mobile(request)
    _drive_access_token = None
    return {"connected": False}


@core.app.get("/api/mobile/catalog")
def mobile_catalog(request: Request) -> Dict[str, Any]:
    require_mobile(request)
    return {"banks": catalog_with_status()}


@core.app.post("/api/mobile/catalog/{file_id}/download")
def download_remote_bank(file_id: str, request: Request) -> Dict[str, Any]:
    require_mobile(request)
    return import_remote_bank(file_id, "permanent")


@core.app.post("/api/mobile/catalog/{file_id}/use-now")
def use_remote_bank(file_id: str, request: Request) -> Dict[str, Any]:
    require_mobile(request)
    return import_remote_bank(file_id, "temporary")


@core.app.get("/api/mobile/overview")
def mobile_overview(request: Request) -> Dict[str, Any]:
    user = require_mobile(request)
    with core.db() as connection:
        banks = connection.execute(
            """SELECT b.bank_id, b.bank_name, b.imported_at, b.format_version,
                      m.storage_mode, m.remote_size, m.remote_modified_time,
                      COUNT(q.question_id) AS question_count
                 FROM question_banks b
                 JOIN mobile_bank_sources m ON m.bank_id = b.bank_id
                 LEFT JOIN questions q ON q.bank_id = b.bank_id
                GROUP BY b.bank_id ORDER BY b.imported_at DESC"""
        ).fetchall()
        live_history = [
            {**dict(row), "snapshot": False}
            for row in connection.execute(
                """SELECT a.attempt_id, t.test_name, a.submitted_at, a.score,
                          a.total_questions, a.percentage
                     FROM attempts a JOIN tests t ON t.test_id = a.test_id
                    WHERE a.student_id = ? AND a.status = 'submitted'""",
                (user["id"],),
            ).fetchall()
        ]
        snapshots = [
            {**dict(row), "snapshot": True}
            for row in connection.execute(
                """SELECT attempt_id, test_name, submitted_at, score, total_questions, percentage
                     FROM mobile_result_snapshots WHERE student_id = ?""",
                (user["id"],),
            ).fetchall()
        ]
    history = sorted(live_history + snapshots, key=lambda item: item["submitted_at"], reverse=True)
    return {"banks": core.rows(banks), "history": history, "student": user}


@core.app.delete("/api/mobile/banks/{bank_id}")
def delete_mobile_bank(bank_id: int, request: Request) -> Dict[str, Any]:
    require_mobile(request)
    with core.db() as connection:
        owned = connection.execute(
            "SELECT 1 FROM mobile_bank_sources WHERE bank_id = ?", (bank_id,)
        ).fetchone()
    if not owned:
        raise HTTPException(404, "Downloaded question bank not found.")
    return purge_bank(bank_id)


@core.app.get("/api/mobile/results/{attempt_id}")
def mobile_result(attempt_id: str, request: Request) -> Dict[str, Any]:
    user = require_mobile(request)
    with core.db() as connection:
        snapshot = connection.execute(
            "SELECT result_json FROM mobile_result_snapshots WHERE attempt_id = ? AND student_id = ?",
            (attempt_id, user["id"]),
        ).fetchone()
    if not snapshot:
        raise HTTPException(404, "Saved result not found.")
    return json.loads(snapshot["result_json"])
