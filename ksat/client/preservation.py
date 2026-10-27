"""Quiescent, same-schema installer preservation and protected durable receipts.

The installer must hold maintenance AND lifecycle exclusion for capture/finish.
It never opens the live database for writing. After restart the authenticated
store may advance (including a submission acknowledgement); only the encrypted
identity and configuration remain byte-immutable. Receipts live under the same
administrator/service ACL as install requests, never in caller-selected Temp.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from ksat.client.install_guard import reject_links
from ksat.protocol import canonical_json
from ksat.update_protocol import parse_client_update


BINDING_FIELDS = ("release_id", "attempt_id", "target_version", "bundle_sha256")
IMMUTABLE_FILES = ("identity/device-key.bin", "client-config.json",
                   "coordinator-url.json", "trust/coordinator-ca.pem")
PRESERVED_FILES = (*IMMUTABLE_FILES, "identity/state-anchor.json",
                   "state/client.sqlite3", "state/client.sqlite3-wal")


def atomic_json(path: Path, value: dict) -> None:
    reject_links(path)
    descriptor, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, *, limit: int = 65536) -> dict:
    reject_links(path)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Protected update record is too large.")
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ValueError("Protected update record is invalid.")
    return value


def load_install_request(root: Path, request_path: Path, public_key: str, authenticode):
    root = Path(root).resolve(strict=True)
    reject_links(request_path)
    path = Path(request_path).resolve(strict=True)
    if path != root / "install-request.json":
        raise ValueError("Client update request is outside protected storage.")
    value = read_json(path)
    required = {"format_version", "release_id", "target_version", "bundle_path", "attempt_id"}
    if set(value) != required or type(value["format_version"]) is not int or value["format_version"] != 1:
        raise ValueError("Client update install request is invalid.")
    if not isinstance(value["attempt_id"], str) or str(uuid.UUID(value["attempt_id"])) != value["attempt_id"]:
        raise ValueError("Client update attempt is invalid.")
    reject_links(Path(value["bundle_path"]))
    bundle = Path(value["bundle_path"]).resolve(strict=True)
    if not bundle.is_relative_to(root) or bundle.name != "bundle.ksat-client-update":
        raise ValueError("Client update bundle is outside protected storage.")
    verified = parse_client_update(bundle, public_key, authenticode)
    if (verified.manifest.release_id != value["release_id"]
            or verified.manifest.client_version != value["target_version"]):
        raise ValueError("Client update install request does not match its bundle.")
    return value, verified


def request_binding(request, verified) -> dict:
    return {"release_id": request["release_id"], "attempt_id": request["attempt_id"],
            "target_version": request["target_version"], "bundle_sha256": verified.bundle_sha256}


def fingerprints(root: Path, names=PRESERVED_FILES) -> dict:
    result = {}
    for name in names:
        path = root / name
        reject_links(path)
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


def authenticated_store_ready(root: Path) -> bool:
    from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
    from ksat.client.store import ClientStore
    identity = DeviceIdentityStore(root / "identity").load_existing()
    return ClientStore.inspect_replacement_safety(root / "state/client.sqlite3",
        integrity_key=derive_state_integrity_key(identity),
        integrity_anchor_path=root / "identity/state-anchor.json",
        allow_pending_submissions=True) is None


def _attempt_evidence(reader, connection) -> dict:
    """Only identifiers/digests leave the protected disposable database copy."""
    evidence = {}
    for row in connection.execute("SELECT * FROM local_attempts ORDER BY attempt_id"):
        attempt_id = row["attempt_id"]
        # Validate sealed answers, response/event correspondence, outbox and any
        # acknowledgement using the store's own semantic validation, not just MACs.
        reader._validated_attempt_snapshot(connection, attempt_id, "Invalid preserved attempt.")
        immutable = {key: row[key] for key in row.keys() if key not in {"state", "receipt_json"}}
        content = {"attempt": immutable}
        for table, order in (("local_responses", "question_id"), ("local_integrity_events", "event_id")):
            content[table] = [dict(item) for item in connection.execute(
                f"SELECT * FROM {table} WHERE attempt_id=? ORDER BY {order}", (attempt_id,))]
        outbox = connection.execute("SELECT * FROM submission_outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
        pending = None
        if outbox is not None:
            reader._validated_outbox_row(connection, outbox, "Invalid preserved submission.")
            pending = {
                "payload_sha256": hashlib.sha256(canonical_json(
                    [outbox["bundle_json"], outbox["created_at"]])).hexdigest(),
                "retry_count": outbox["retry_count"], "status": outbox["status"],
            }
        evidence[attempt_id] = {
            "state": row["state"], "content_sha256": hashlib.sha256(canonical_json(content)).hexdigest(),
            "receipt_sha256": hashlib.sha256(row["receipt_json"].encode()).hexdigest() if row["receipt_json"] else None,
            "outbox": pending,
        }
    return evidence


def _verify_attempt_continuity(before: dict, after: dict) -> None:
    if not isinstance(before, dict):
        raise ValueError("Preservation attempt evidence is missing.")
    for attempt_id, old in before.items():
        current = after.get(attempt_id)
        if (not isinstance(old, dict) or current is None or set(old) != set(current)
                or old["content_sha256"] != current["content_sha256"]):
            raise ValueError("A preserved attempt or its sealed answers were lost or changed.")
        if old["state"] == "acknowledged":
            if current != old:
                raise ValueError("A preserved acknowledgement changed.")
        elif old["state"] == "sealed_pending":
            if current["state"] == "acknowledged":
                if current["receipt_sha256"] is None or current["outbox"] is not None:
                    raise ValueError("Preserved submission has no valid acknowledgement.")
            elif current["state"] == "sealed_pending":
                prior, pending = old["outbox"], current["outbox"]
                if (not isinstance(prior, dict) or not isinstance(pending, dict)
                        or set(prior) != set(pending)
                        or prior["payload_sha256"] != pending["payload_sha256"]
                        or type(prior["retry_count"]) is not int
                        or pending["retry_count"] < prior["retry_count"]
                        or (prior["status"] == "faculty_intervention_required"
                            and pending["status"] != prior["status"])):
                    raise ValueError("A preserved pending submission lost its retry/intervention state.")
            else:
                raise ValueError("A preserved submission regressed.")
        else:
            raise ValueError("Preservation evidence contains active or unknown state.")


def continuity_evidence(root: Path, expected: dict | None = None) -> dict:
    """Authenticate one stable DB/WAL/anchor snapshot and prove baseline ancestry.

Never open the live database or use ClientStore's writable constructor. A
snapshot race fails closed and the updater's bounded health loop may retry it.
"""
    from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
    from ksat.client.store import ClientStore
    before = fingerprints(root)
    identity = DeviceIdentityStore(root / "identity").load_existing()
    integrity_key = derive_state_integrity_key(identity)
    with tempfile.TemporaryDirectory(prefix=".preservation-", dir=root / "state") as directory:
        snapshot = Path(directory)
        for relative in ("state/client.sqlite3", "state/client.sqlite3-wal", "identity/state-anchor.json"):
            if before[relative] is not None:
                destination = snapshot / Path(relative).name
                shutil.copyfile(root / relative, destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != before[relative]:
                    raise ValueError("Client state changed during preservation inspection.")
        database = snapshot / "client.sqlite3"
        anchor = snapshot / "state-anchor.json"
        if ClientStore.inspect_replacement_safety(database, integrity_key=integrity_key,
                integrity_anchor_path=anchor, allow_pending_submissions=True) is not None:
            raise ValueError("Client authenticated state is not safe to preserve.")
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            # These validation methods are read-only; deliberately bypass schema
            # migration and torn-anchor recovery in ClientStore.__init__.
            reader = ClientStore.__new__(ClientStore)
            reader._integrity_key = integrity_key
            reader._integrity_anchor_path = anchor
            reader._validated_journal_chain(connection)
            reader._verify_current_authenticated_state(connection)
            tail = dict(connection.execute("SELECT sequence, state_digest, entry_mac FROM authenticated_state_journal ORDER BY sequence DESC LIMIT 1").fetchone())
            attempts = _attempt_evidence(reader, connection)
            if expected is not None:
                if not isinstance(expected, dict) or set(expected) != {"journal", "attempts"}:
                    raise ValueError("Preservation continuity evidence is missing.")
                journal = expected["journal"]
                if (not isinstance(journal, dict) or set(journal) != set(tail)
                        or type(journal["sequence"]) is not int or journal["sequence"] < 1):
                    raise ValueError("Preservation journal evidence is invalid.")
                ancestor = connection.execute("SELECT sequence, state_digest, entry_mac FROM authenticated_state_journal WHERE sequence=?",
                                              (journal["sequence"],)).fetchone()
                if ancestor is None or dict(ancestor) != journal:
                    raise ValueError("Client state does not descend from the preserved authenticated baseline.")
                _verify_attempt_continuity(expected["attempts"], attempts)
    if fingerprints(root) != before:
        raise ValueError("Client state changed during preservation inspection.")
    return {"journal": tail, "attempts": attempts}


class PreservationSession:
    def __init__(self, root: Path, binding: dict, installed_version: str, installer_sha256: str,
                 *, rollback_from_installer_sha256: str | None = None):
        self.root = root
        self.path = root / "updates/preservation-receipt.json"
        self.context = dict(binding, format_version=2, installed_version=installed_version,
                            installer_sha256=installer_sha256)
        self.baseline = None
        self.binding = binding
        self.rollback_from_installer_sha256 = rollback_from_installer_sha256

    def capture(self) -> None:
        if self.rollback_from_installer_sha256 is not None:
            verify_rollback_source(self.root, self.binding, self.rollback_from_installer_sha256)
        if not authenticated_store_ready(self.root):
            raise ValueError("Client authenticated state is not safe to preserve.")
        self.baseline = fingerprints(self.root)
        self.continuity = continuity_evidence(self.root)
        if fingerprints(self.root) != self.baseline:
            raise ValueError("Client state changed during preservation capture.")
        atomic_json(self.path, dict(self.context, stage="baseline", files=self.baseline, continuity=self.continuity))

    def finish(self) -> None:
        if self.baseline is None or fingerprints(self.root) != self.baseline:
            raise ValueError("The installer changed protected client data.")
        if not authenticated_store_ready(self.root) or fingerprints(self.root) != self.baseline:
            raise ValueError("The installer changed authenticated client state.")
        atomic_json(self.path, dict(self.context, stage="verified", files=self.baseline, continuity=self.continuity))


def verify_receipt(root: Path, binding: dict, installed_version: str, installer_sha256: str) -> None:
    receipt = read_json(root / "updates/preservation-receipt.json", limit=16 * 1024 * 1024)
    expected = dict(binding, format_version=2, stage="verified",
                    installed_version=installed_version, installer_sha256=installer_sha256)
    if (set(receipt) != {*expected, "files", "continuity"}
            or not isinstance(receipt["continuity"], dict)
            or any(receipt.get(k) != v for k, v in expected.items())):
        raise ValueError("Client preservation receipt does not match this installation.")
    files = receipt["files"]
    if not isinstance(files, dict) or set(files) != set(PRESERVED_FILES):
        raise ValueError("Client preservation receipt is incomplete.")
    for name, digest in fingerprints(root, IMMUTABLE_FILES).items():
        if files[name] != digest:
            raise ValueError("Client update changed immutable device data.")
    continuity_evidence(root, receipt["continuity"])


def verify_rollback_source(root: Path, binding: dict, installer_sha256: str) -> None:
    """Do not turn a failed installer's altered data into a new valid baseline."""
    receipt = read_json(root / "updates/preservation-receipt.json", limit=16 * 1024 * 1024)
    if receipt.get("stage") == "verified":
        # The forward installer preserved every byte. Authenticated service
        # transitions since its commit (e.g. acknowledgement) remain legitimate.
        verify_receipt(root, binding, binding["target_version"], installer_sha256)
        return
    expected = dict(binding, format_version=2, stage="baseline",
                    installed_version=binding["target_version"], installer_sha256=installer_sha256)
    if (set(receipt) != {*expected, "files", "continuity"}
            or not isinstance(receipt["continuity"], dict)
            or any(receipt.get(k) != v for k, v in expected.items())
            or receipt["files"] != fingerprints(root)):
        raise ValueError("Failed installation did not preserve its original baseline.")
    if not authenticated_store_ready(root):
        raise ValueError("Client state is not safe for rollback.")
    continuity_evidence(root, receipt["continuity"])
