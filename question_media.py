from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_MEDIA_BYTES = 15_000_000
MAX_MEDIA_DIMENSION = 10_000
OPTION_KEYS = frozenset("ABCDE")
_MEDIA_FIELDS = frozenset(("question", "options", "solution"))


def png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or content[:8] != PNG_SIGNATURE or content[12:16] != b"IHDR":
        raise ValueError("Display media must be a valid PNG image.")
    width, height = struct.unpack(">II", content[16:24])
    if not 0 < width <= MAX_MEDIA_DIMENSION or not 0 < height <= MAX_MEDIA_DIMENSION:
        raise ValueError("Display-media dimensions are outside the supported range.")
    return width, height


def validate_posix_asset_path(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("Display-media assets must use safe forward-slash paths.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise ValueError("Display-media assets must use safe forward-slash paths.")
    if path.suffix.lower() != ".png":
        raise ValueError("Display media must use PNG assets.")
    return path.as_posix()


def validate_media_item(
    raw: Any, *, archive: Any, members: Mapping[str, Any], question_key: str
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{question_key} display media must be an object.")
    asset = validate_posix_asset_path(str(raw.get("asset", "")))
    alt_text = str(raw.get("alt_text", "")).strip()
    if not alt_text:
        raise ValueError(f"{question_key} display media requires alt_text.")
    member = members.get(asset)
    if not member or member.file_size > MAX_MEDIA_BYTES:
        raise ValueError(f"{question_key} display-media asset is missing or too large: {asset}")
    content = archive.read(member)
    if len(content) > MAX_MEDIA_BYTES:
        raise ValueError(f"{question_key} display-media asset is missing or too large: {asset}")
    width, height = png_dimensions(content)
    digest = hashlib.sha256(content).hexdigest()
    if str(raw.get("sha256", "")).lower() != digest:
        raise ValueError(f"{question_key} display-media hash does not match: {asset}")
    return {
        "asset": asset,
        "alt_text": alt_text,
        "sha256": digest,
        "width": width,
        "height": height,
        "content": content,
    }


def parse_display_media(
    raw: Any, *, archive: Any, members: Mapping[str, Any], question_key: str
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{question_key} display_media must be an object.")
    unknown = set(raw) - _MEDIA_FIELDS
    if unknown:
        raise ValueError(f"{question_key} display_media has unknown fields: {', '.join(sorted(unknown))}")
    parsed: dict[str, Any] = {}
    if "question" in raw:
        parsed["question"] = validate_media_item(
            raw["question"], archive=archive, members=members, question_key=question_key
        )
    if "options" in raw:
        options = raw["options"]
        if not isinstance(options, dict):
            raise ValueError(f"{question_key} display_media options must be an object.")
        invalid = set(options) - OPTION_KEYS
        if invalid:
            raise ValueError(f"{question_key} display_media options must use option keys A-E.")
        parsed["options"] = {
            option: validate_media_item(item, archive=archive, members=members, question_key=question_key)
            for option, item in options.items()
        }
    if "solution" in raw:
        solution = raw["solution"]
        if not isinstance(solution, list):
            raise ValueError(f"{question_key} display_media solution must be a list.")
        parsed["solution"] = [
            validate_media_item(item, archive=archive, members=members, question_key=question_key)
            for item in solution
        ]
    return parsed


def _store_item(item: Mapping[str, Any], asset_dir: Path) -> dict[str, Any]:
    digest = str(item["sha256"])
    filename = digest + ".png"
    (asset_dir / filename).write_bytes(bytes(item["content"]))
    return {
        "asset_filename": filename,
        "alt_text": str(item["alt_text"]),
        "sha256": digest,
        "width": int(item["width"]),
        "height": int(item["height"]),
    }


def store_display_media(media: Mapping[str, Any], *, asset_dir: Path) -> dict[str, Any]:
    stored: dict[str, Any] = {}
    if "question" in media:
        stored["question"] = _store_item(media["question"], asset_dir)
    if "options" in media:
        stored["options"] = {
            option: _store_item(item, asset_dir) for option, item in media["options"].items()
        }
    if "solution" in media:
        stored["solution"] = [_store_item(item, asset_dir) for item in media["solution"]]
    return stored


def _public_item(item: Mapping[str, Any], bank_id: int) -> dict[str, Any]:
    filename = str(item["asset_filename"])
    return {
        "asset_url": f"/api/question-assets/{bank_id}/{filename}",
        "alt_text": str(item["alt_text"]),
        "width": int(item["width"]),
        "height": int(item["height"]),
    }


def public_display_media(
    stored: Mapping[str, Any], *, bank_id: int, include_solution: bool
) -> dict[str, Any]:
    public: dict[str, Any] = {}
    if "question" in stored:
        public["question"] = _public_item(stored["question"], bank_id)
    if "options" in stored:
        public["options"] = {
            option: _public_item(item, bank_id) for option, item in stored["options"].items()
        }
    if include_solution and "solution" in stored:
        public["solution"] = [_public_item(item, bank_id) for item in stored["solution"]]
    return public


def media_owns_filename(stored_json: str, filename: str) -> bool:
    if not filename or Path(filename).name != filename:
        return False
    try:
        stored = json.loads(stored_json)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(stored, dict):
        return False

    def owns(item: Any) -> bool:
        return isinstance(item, dict) and item.get("asset_filename") == filename

    if owns(stored.get("question")):
        return True
    options = stored.get("options")
    if isinstance(options, dict) and any(owns(item) for item in options.values()):
        return True
    solution = stored.get("solution")
    return isinstance(solution, list) and any(owns(item) for item in solution)
