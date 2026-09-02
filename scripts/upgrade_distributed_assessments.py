"""Safe compatibility upgrade for the distributed lab-assessment coordinator."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ksat.coordinator.process_lock import CoordinatorLockHeld, CoordinatorProcessLock


def _resolved_regular_file(path: Path) -> Path:
    source = Path(path)
    if source.is_symlink():
        raise ValueError("The live database path must not be a symbolic link.")
    resolved = source.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("The live database path must be a regular file.")
    return resolved


def _resolved_directory(path: Path) -> Path:
    source = Path(path)
    if source.is_symlink():
        raise ValueError("The coordinator data path must not be a symbolic link.")
    resolved = source.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("The coordinator data path must be a directory.")
    return resolved


def _integrity_check(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError("SQLite integrity check failed.")
    finally:
        connection.close()


def _sqlite_backup(source_path: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
        _integrity_check(temporary)
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        with destination.open("rb+") as stream:
            os.fsync(stream.fileno())
        try:
            parent_descriptor = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            parent_descriptor = None
        if parent_descriptor is not None:
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _assert_idle(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "tests" not in tables:
            return
        columns = _table_columns(connection, "tests")
        predicates = []
        if "launched" in columns:
            predicates.append("COALESCE(launched,0)=1")
        if "status" in columns:
            predicates.append("status='in_progress'")
        mode = "AND mode='faculty'" if "mode" in columns else ""
        if predicates and connection.execute(
            f"SELECT 1 FROM tests WHERE ({' OR '.join(predicates)}) {mode} LIMIT 1"
        ).fetchone():
            raise RuntimeError("A faculty assessment is active or in progress; close it before upgrade.")
        attempt_mode = "AND t.mode='faculty'" if "mode" in columns else ""
        if "attempts" in tables and connection.execute(
            f"""SELECT 1 FROM attempts a JOIN tests t ON t.test_id=a.test_id
               WHERE a.status='in_progress' {attempt_mode} LIMIT 1"""
        ).fetchone():
            raise RuntimeError("A faculty assessment is active or in progress; close it before upgrade.")
    finally:
        connection.close()


def _counts(db_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        def count(name: str) -> int:
            return connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] if name in tables else 0
        return {
            "preserved_students": count("students"),
            "preserved_attempts": count("attempts"),
            "preserved_responses": count("responses"),
            "preserved_violations": count("exam_violations"),
            "devices": count("devices"),
        }
    finally:
        connection.close()


def _run_additive_upgrade_in_worker(db_path: Path, data_dir: Path) -> dict[str, int]:
    import app as coordinator_app

    originals = (
        coordinator_app.DATA_DIR,
        coordinator_app.DB_PATH,
        coordinator_app.BACKUP_DIR,
        coordinator_app.QUESTION_BANKS_DIR,
        coordinator_app.app.state.coordinator_config,
    )
    prepared = 0
    created_artifacts: list[Path] = []
    try:
        coordinator_app.DATA_DIR = data_dir
        coordinator_app.DB_PATH = db_path
        coordinator_app.BACKUP_DIR = data_dir / "backups"
        coordinator_app.QUESTION_BANKS_DIR = data_dir / "Question Banks"
        coordinator_app.ensure_schema()
        coordinator_app.configure_coordinator_state(coordinator_app.app)
        with coordinator_app.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            eligible = connection.execute(
                """SELECT t.* FROM tests t
                   WHERE t.mode='faculty' AND t.active=1 AND COALESCE(t.launched,0)=0
                     AND t.release_id IS NULL AND t.bank_id IS NOT NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM attempts a
                       WHERE a.test_id=t.test_id
                         AND (a.status='submitted' OR a.submitted_at IS NOT NULL)
                     )
                   ORDER BY t.test_id"""
            ).fetchall()
            for test in eligible:
                coordinator_app.prepare_faculty_release(
                    connection, test, _created_artifact_paths=created_artifacts
                )
                prepared += 1
        result = _counts(db_path)
        result["prepared_releases"] = prepared
        return result
    except Exception:
        for artifact in created_artifacts:
            artifact.unlink(missing_ok=True)
        raise
    finally:
        (
            coordinator_app.DATA_DIR,
            coordinator_app.DB_PATH,
            coordinator_app.BACKUP_DIR,
            coordinator_app.QUESTION_BANKS_DIR,
            coordinator_app.app.state.coordinator_config,
        ) = originals


def _run_additive_upgrade(
    db_path: Path, data_dir: Path, *, isolated_secrets: bool = False
) -> dict[str, Any]:
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    environment["KSAT_DATA_DIR"] = str(data_dir)
    if isolated_secrets:
        for name in (
            "KSAT_DEVICE_ENROLLMENT_CODE", "KSAT_SESSION_SECRET", "SESSION_SECRET"
        ):
            environment.pop(name, None)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--isolated-worker", str(db_path), str(data_dir)],
        cwd=source_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "The isolated upgrade worker failed without modifying the coordinator process."
        ) from RuntimeError(completed.stderr[-2000:])
    marker = "KSAT_UPGRADE_RESULT="
    line = next((item for item in completed.stdout.splitlines() if item.startswith(marker)), None)
    if line is None:
        raise RuntimeError("The isolated upgrade worker returned no result.")
    return json.loads(line[len(marker):])


def upgrade(db_path: Path, data_dir: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Back up, migrate additively, and prepare only safe legacy assessments."""

    live_db = _resolved_regular_file(Path(db_path))
    live_data = _resolved_directory(Path(data_dir))
    if live_db == live_data or live_db.is_relative_to(live_data / "backups"):
        raise ValueError("Live database, backup, and data output paths must not overlap.")
    if any(path.is_symlink() for path in live_data.rglob("*")):
        raise ValueError("Coordinator data contains a symbolic-link alias and cannot be upgraded safely.")
    try:
        lock = CoordinatorProcessLock(live_data).acquire()
    except CoordinatorLockHeld as error:
        raise RuntimeError(str(error)) from error
    try:
        for candidate in live_data.rglob("*"):
            if not candidate.is_file() or candidate.resolve() == live_db:
                continue
            try:
                if os.path.samefile(candidate, live_db):
                    raise ValueError("The live database has a hard-link alias inside coordinator data.")
            except FileNotFoundError:
                continue
        _assert_idle(live_db)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        if dry_run:
            root = Path(tempfile.mkdtemp(prefix="ksat-upgrade-dry-run-"))
            copied_data = root / "data"
            ignored = {"secrets", "backups", "Assessment Releases", ".coordinator.lock"}
            if live_db.parent == live_data:
                ignored.add(live_db.name)
            shutil.copytree(
                live_data, copied_data, symlinks=False,
                ignore=lambda _path, names: [name for name in names if name in ignored],
            )
            copied_db = _sqlite_backup(live_db, copied_data / "aptitude.db")
            result = _run_additive_upgrade(copied_db, copied_data, isolated_secrets=True)
            result["planned_prepared_releases"] = result["prepared_releases"]
            result["backup_path"] = None
            result["dry_run_workspace"] = str(root)
            return result

        backup = live_data / "backups" / f"aptitude-pre-distributed-{stamp}.db"
        if not backup.parent.resolve(strict=False).is_relative_to(live_data):
            raise ValueError("The backup path must remain inside coordinator data.")
        _sqlite_backup(live_db, backup)
        try:
            result = _run_additive_upgrade(live_db, live_data)
            _integrity_check(live_db)
        except Exception as error:
            raise RuntimeError(f"Upgrade failed; recover from backup: {backup}") from error
        result["backup_path"] = str(backup)
        return result
    finally:
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    result = upgrade(arguments.db_path, arguments.data_dir, dry_run=arguments.dry_run)
    for key in sorted(result):
        print(f"{key}: {result[key]}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--isolated-worker":
        worker_result = _run_additive_upgrade_in_worker(Path(sys.argv[2]), Path(sys.argv[3]))
        print("KSAT_UPGRADE_RESULT=" + json.dumps(worker_result, separators=(",", ":")))
    else:
        raise SystemExit(main())
