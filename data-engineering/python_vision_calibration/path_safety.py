"""Fail-closed descendant path checks for calibration artifacts."""

from __future__ import annotations

import os
from pathlib import Path


def is_reparse(path: Path) -> bool:
    try:
        stat = Path(path).lstat()
    except OSError:
        return False
    return Path(path).is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def safe_descendant(root: Path, path: Path, label: str) -> Path:
    """Return a lexical descendant only when no existing component is a reparse point."""
    root_path = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root_path)
    except ValueError as error:
        raise ValueError(f"{label} must remain below its authoritative root.") from error
    current = root_path
    if os.path.lexists(current) and is_reparse(current):
        raise ValueError(f"{label} must not cross a symlink or reparse point.")
    for component in relative.parts:
        current = current / component
        if os.path.lexists(current) and is_reparse(current):
            raise ValueError(f"{label} must not cross a symlink or reparse point.")
    return candidate


def safe_directory(root: Path, path: Path, label: str) -> Path:
    """Create a descendant directory and recheck its complete component chain."""
    directory = safe_descendant(root, path, label)
    directory.mkdir(parents=True, exist_ok=True)
    directory = safe_descendant(root, directory, label)
    if not directory.is_dir():
        raise ValueError(f"{label} must be a directory.")
    return directory


def safe_tree(root: Path, label: str) -> Path:
    """Reject every existing descendant reparse point without following it."""
    root_path = safe_descendant(root, root, label)
    if not root_path.exists():
        return root_path
    if not root_path.is_dir():
        raise ValueError(f"{label} must be a directory.")
    pending = [root_path]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            if is_reparse(entry):
                raise ValueError(f"{label} must not contain a symlink or reparse point: {entry}")
            if entry.is_dir():
                pending.append(entry)
    return root_path


__all__ = ["is_reparse", "safe_descendant", "safe_directory", "safe_tree"]
