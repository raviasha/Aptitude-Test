"""Create a V2 chapter config from reviewed logical-reasoning manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ENGINEERING_ROOT = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from logical_reasoning_v2.chapter_config import build_chapter_config  # noqa: E402
from textbook_chapters_v2.config import ChapterConfig  # noqa: E402
from textbook_chapters_v2.source import _shared_context_groups  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _validate_contexts(payload):
    contexts = payload["shared_contexts"]
    roles = {"question", "answer_key", "solution"}
    if contexts.keys() - roles:
        raise ValueError("Unknown shared-context source role")
    config = ChapterConfig.from_dict(payload)
    for role in roles:
        page_start, page_end = getattr(config, role + "_pages")
        for _, _, segments in _shared_context_groups(config, role):
            for segment in segments:
                if not page_start <= segment["page"] <= page_end:
                    raise ValueError("Shared-context page is outside the chapter")
                if not (0 <= segment["left"] < segment["right"] and
                        0 <= segment["top"] < segment["bottom"]):
                    raise ValueError("Shared-context segments need nonnegative origins and positive area")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifests", type=Path, required=True)
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chapter-id", type=int, default=101)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--shared-contexts", type=Path, help="Shared-contexts JSON object from reviewed evidence.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output config.")
    args = parser.parse_args()

    if args.output.exists() and not args.force:
        parser.error("Output already exists; use --force to overwrite it.")
    shared_contexts = None
    if args.shared_contexts is not None:
        try:
            shared_contexts = json.loads(
                args.shared_contexts.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object, parse_constant=_reject_constant,
            )
            if not isinstance(shared_contexts, dict):
                raise ValueError("Expected a JSON object")
        except (OSError, ValueError) as error:
            parser.error(f"Invalid shared-contexts input: {error}")
    paths = sorted(args.manifests.glob("exercise-*.json"))
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    source = args.source_pdf.as_posix()
    payload = build_chapter_config(
        manifests, source_pdf=source, source_pdf_sha256=_sha256(args.source_pdf),
        chapter_id=args.chapter_id, work_root=args.work_root, shared_contexts=shared_contexts,
    )
    try:
        _validate_contexts(payload)
    except ValueError as error:
        parser.error(f"Invalid shared-contexts input: {error}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("w" if args.force else "x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except FileExistsError:
        parser.error("Output already exists; use --force to overwrite it.")
    print(json.dumps({"status": "written", "path": str(args.output), "records": payload["printed_question_count"]}))


if __name__ == "__main__":
    main()
