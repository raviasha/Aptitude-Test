"""Crash-safe quarantine for coordinator-owned deletion artifacts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ksat.sqlite import connect_sqlite


_QUARANTINE_DIRECTORY = ".artifact-quarantine"
_MANIFEST_NAME = "manifest.json"
_MANIFEST_VERSION = 1
_MAX_ARTIFACTS = 10_000
_OWNER_QUERIES = {
    "test": "SELECT 1 FROM tests WHERE test_id=?",
    "question_bank": "SELECT 1 FROM question_banks WHERE bank_id=?",
    "load_test": "SELECT 1 FROM load_test_runs WHERE namespace=?",
}


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse_point(value: os.stat_result) -> bool:
    return bool(
        getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class ArtifactQuarantine:
    """Stage artifacts on the data volume before their owner transaction commits."""

    def __init__(
        self,
        data_dir: Path,
        *,
        owner_kind: str,
        owner_id: int | str,
        artifacts: Iterable[Path],
        _operation_id: str | None = None,
    ) -> None:
        if owner_kind not in _OWNER_QUERIES:
            raise ValueError("Artifact quarantine owner is invalid.")
        if type(owner_id) not in {int, str} or (isinstance(owner_id, str) and not owner_id):
            raise ValueError("Artifact quarantine owner identifier is invalid.")
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / _QUARANTINE_DIRECTORY
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.operation_id = _operation_id or uuid.uuid4().hex
        if len(self.operation_id) != 32 or any(
            character not in "0123456789abcdef" for character in self.operation_id
        ):
            raise ValueError("Artifact quarantine operation identifier is invalid.")
        self.operation_directory = self.root / self.operation_id
        self.staged_directory = self.operation_directory / "artifacts"
        relative_paths: list[Path] = []
        for raw_path in artifacts:
            candidate = _lexical_absolute(Path(raw_path))
            try:
                relative = candidate.relative_to(self.data_dir)
            except ValueError as error:
                raise ValueError("Deletion artifact is outside the coordinator data directory.") from error
            if not relative.parts or relative.parts[0] == _QUARANTINE_DIRECTORY:
                raise ValueError("Deletion artifact path is invalid.")
            if candidate.parent.resolve(strict=False).is_relative_to(self.data_dir) is False:
                raise ValueError("Deletion artifact parent is outside the coordinator data directory.")
            if os.path.lexists(candidate):
                metadata = os.lstat(candidate)
                if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                    raise ValueError("Deletion artifact cannot be a link or reparse point.")
            if relative not in relative_paths:
                relative_paths.append(relative)
        if len(relative_paths) > _MAX_ARTIFACTS:
            raise ValueError("Too many deletion artifacts were supplied.")
        for index, left in enumerate(relative_paths):
            for right in relative_paths[index + 1 :]:
                if left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError("Deletion artifact paths cannot overlap.")
        self._items = tuple(
            (relative, Path("artifacts") / f"{index:04d}")
            for index, relative in enumerate(relative_paths)
        )

    @staticmethod
    def _replace_artifact(source: Path, destination: Path) -> None:
        os.replace(source, destination)

    @staticmethod
    def _purge_directory(path: Path) -> None:
        shutil.rmtree(path)

    def _manifest(self) -> dict[str, Any]:
        return {
            "version": _MANIFEST_VERSION,
            "operation_id": self.operation_id,
            "owner": {"kind": self.owner_kind, "id": self.owner_id},
            "artifacts": [
                {"original": original.as_posix(), "staged": staged.as_posix()}
                for original, staged in self._items
            ],
        }

    def _write_manifest(self) -> None:
        content = json.dumps(
            self._manifest(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        temporary = self.operation_directory / f".{_MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.operation_directory / _MANIFEST_NAME)
            _fsync_directory(self.operation_directory)
        finally:
            temporary.unlink(missing_ok=True)

    def stage(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.operation_directory.mkdir(parents=False, exist_ok=False)
        self.staged_directory.mkdir()
        try:
            self._write_manifest()
            for original_relative, staged_relative in self._items:
                original = self.data_dir / original_relative
                if not os.path.lexists(original):
                    continue
                staged = self.operation_directory / staged_relative
                self._replace_artifact(original, staged)
            _fsync_directory(self.data_dir)
            _fsync_directory(self.staged_directory)
        except BaseException:
            try:
                self.restore()
            except BaseException:
                # The durable manifest remains for startup recovery.
                pass
            raise

    def restore(self) -> None:
        if not self.operation_directory.exists():
            return
        for original_relative, staged_relative in reversed(self._items):
            staged = self.operation_directory / staged_relative
            if not os.path.lexists(staged):
                continue
            original = self.data_dir / original_relative
            if os.path.lexists(original):
                raise RuntimeError("Cannot restore a quarantined artifact over an existing path.")
            original.parent.mkdir(parents=True, exist_ok=True)
            self._replace_artifact(staged, original)
            _fsync_directory(original.parent)
        self._purge_directory(self.operation_directory)
        _fsync_directory(self.root)

    def purge(self) -> bool:
        if not self.operation_directory.exists():
            return True
        try:
            self._purge_directory(self.operation_directory)
            _fsync_directory(self.root)
        except OSError:
            return False
        return True

    @classmethod
    def from_manifest(cls, data_dir: Path, operation_directory: Path) -> ArtifactQuarantine:
        manifest_path = operation_directory / _MANIFEST_NAME
        raw = manifest_path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("Artifact quarantine manifest is too large.")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Artifact quarantine manifest is invalid.") from error
        if not isinstance(value, dict) or set(value) != {
            "version", "operation_id", "owner", "artifacts"
        }:
            raise ValueError("Artifact quarantine manifest is invalid.")
        owner = value["owner"]
        artifacts = value["artifacts"]
        if (
            value["version"] != _MANIFEST_VERSION
            or value["operation_id"] != operation_directory.name
            or not isinstance(owner, dict)
            or set(owner) != {"kind", "id"}
            or owner["kind"] not in _OWNER_QUERIES
            or not isinstance(artifacts, list)
            or len(artifacts) > _MAX_ARTIFACTS
        ):
            raise ValueError("Artifact quarantine manifest is invalid.")
        originals: list[Path] = []
        for index, item in enumerate(artifacts):
            if not isinstance(item, dict) or set(item) != {"original", "staged"}:
                raise ValueError("Artifact quarantine manifest is invalid.")
            original = PurePosixPath(item["original"])
            expected_staged = PurePosixPath("artifacts") / f"{index:04d}"
            if (
                not isinstance(item["original"], str)
                or not isinstance(item["staged"], str)
                or original.is_absolute()
                or not original.parts
                or ".." in original.parts
                or item["staged"] != expected_staged.as_posix()
            ):
                raise ValueError("Artifact quarantine manifest is invalid.")
            originals.append(Path(*original.parts))
        instance = cls(
            data_dir,
            owner_kind=owner["kind"],
            owner_id=owner["id"],
            artifacts=[Path(data_dir) / item for item in originals],
            _operation_id=value["operation_id"],
        )
        if instance.operation_directory.resolve() != operation_directory.resolve():
            raise ValueError("Artifact quarantine operation path is invalid.")
        return instance


def recover_artifact_quarantine(data_dir: Path, db_path: Path) -> dict[str, int]:
    """Restore pre-commit operations and purge committed deletions idempotently."""

    root = Path(data_dir).resolve() / _QUARANTINE_DIRECTORY
    if not root.exists():
        return {"restored": 0, "purged": 0, "pending": 0}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Artifact quarantine directory is invalid.")
    restored = 0
    purged = 0
    pending = 0
    connection = connect_sqlite(Path(db_path))
    try:
        for operation_directory in sorted(root.iterdir()):
            metadata = os.lstat(operation_directory)
            if (
                not operation_directory.is_dir()
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(metadata)
            ):
                raise ValueError("Artifact quarantine entry is invalid.")
            operation = ArtifactQuarantine.from_manifest(data_dir, operation_directory)
            owner_exists = connection.execute(
                _OWNER_QUERIES[operation.owner_kind], (operation.owner_id,)
            ).fetchone() is not None
            if owner_exists:
                operation.restore()
                restored += 1
            elif operation.purge():
                purged += 1
            else:
                pending += 1
    finally:
        connection.close()
    return {"restored": restored, "purged": purged, "pending": pending}


__all__ = ["ArtifactQuarantine", "recover_artifact_quarantine"]
