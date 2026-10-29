"""Administrator recovery of explicitly confirmed obsolete practice attempts.

No constructor that initializes, migrates or repairs a ClientStore is used here.
A prepared archive is evidence, never permission to automatically roll back live
state. A crash between SQLite commit and anchor publication requires inspection.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
from ksat.client.install_guard import MaintenanceBusy, MaintenanceGate, WindowsClientService, reject_links
from ksat.client.store import ClientStore, _AUTHENTICATED_SCHEMA_VERSION
from ksat.client.version import CLIENT_VERSION
from ksat.coordinator.process_lock import CoordinatorLockHeld, CoordinatorProcessLock
from ksat.public_trust import canonical_json, normalize_coordinator_base_url, strict_json, validate_ca


PROBLEM_GUIDANCE = {
    'attempt_identity_mismatch': 'Only confirmed old practice attempts can be archived. They will not upload or appear as graded results.',
    'faculty_intervention_required': 'Ask faculty to diagnose this submission. No automatic repair is supported for this cause.',
    'enrollment_revoked': 'Ask the Coordinator administrator to review enrollment. Do not reset the device identity.',
    'identity_invalid': 'Protected identity is missing or unreadable. Preserve the installation and contact the administrator.',
    'configuration_invalid': 'Have the administrator inspect the installed Coordinator configuration.',
    'time_or_trust_invalid': 'Check the computer clock and installed Coordinator certificate with the administrator.',
    'authenticated_state_invalid': 'Preserve local state and recovery archives for administrator inspection. Do not reset or overwrite the anchor.',
    'unsupported_schema': 'This store schema needs a supported administrator upgrade, not practice-attempt recovery.',
    'installation_missing': 'An existing client installation is required.',
    'active_attempt': 'Finish the active test before running recovery.',
    'paths_unprotected': 'Recovery requires administrator-protected directories without links or reparse points.',
    'maintenance_busy': 'Another maintenance operation or test start is in progress. Retry when it finishes.',
    'client_busy': 'Close the client desktop and wait for it to finish before retrying.',
    'selection_changed': 'The selection has changed or was already archived. Refresh and select again.',
    'confirmation_required': 'Select attempts and explicitly confirm they are obsolete practice work.',
    'archive_failed': 'The archive could not be verified. Live attempt removal was not started; retain any archive for inspection.',
    'mutation_requires_inspection': 'Recovery was interrupted. Retain the archive and inspect authenticated live state; do not restore it automatically.',
    'service_control_failed': 'The client service could not be stopped safely. Ask the administrator to check its status.',
    'service_restart_failed': 'The client service could not be restarted. Ask the administrator to restore service availability.',
    'recovery_failed': 'Recovery could not finish. Preserve local state and any archive for administrator inspection.',
}


def safe_problem_code(last_error: str | None) -> str:
    """Map a bounded protocol error prefix; never return raw exception text."""
    code = last_error.split(':', 1)[0] if isinstance(last_error, str) else ''
    if code == 'attempt_identity_mismatch':
        return code
    if code in {'device_revoked', 'device_disabled', 'device_inactive', 'enrollment_revoked', 'device_not_enrolled'}:
        return 'enrollment_revoked'
    if code in {'clock_skew', 'timestamp_invalid', 'certificate_invalid', 'trust_invalid'}:
        return 'time_or_trust_invalid'
    return 'faculty_intervention_required'


class RecoveryError(RuntimeError):
    def __init__(self, code: str, *, archive_path: Path | None = None):
        self.code = code if code in PROBLEM_GUIDANCE else 'recovery_failed'
        self.archive_path = archive_path
        super().__init__(self.code)


@dataclass(frozen=True)
class RecoveryCandidate:
    attempt_id: str
    sealed_at: str
    selection_token: str
    problem_code: str = 'attempt_identity_mismatch'


@dataclass(frozen=True)
class RecoveryPreview:
    candidates: tuple[RecoveryCandidate, ...] = ()
    problem_codes: tuple[str, ...] = ()
    client_version: str = CLIENT_VERSION


@dataclass(frozen=True)
class RecoveryResult:
    archive_path: Path
    removed_attempt_ids: tuple[str, ...]


class ClientRecoveryLock(CoordinatorProcessLock):
    """Same lock as the client authority, without importing its web application."""
    lock_filename = '.client.lock'
    lock_held_message = 'Client authority is still running.'


def _require_protected_directory(path: Path, *, public_read=False) -> None:
    if os.name != 'nt':
        raise RecoveryError('paths_unprotected')
    if public_read:
        # The installation parent intentionally grants Users read/execute, but
        # must not grant replacement/deletion rights over protected children.
        reject_links(path)
        script = r"""
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$acl=Get-Acl -LiteralPath $env:KSAT_RECOVERY_PARENT
$allowed=@('S-1-5-18','S-1-5-32-544')
if($allowed -notcontains $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value){throw 'unprotected owner'}
if(-not $acl.AreAccessRulesProtected){throw 'inherited access'}
$rights=[Security.AccessControl.FileSystemRights]
$writes=$rights::Write -bor $rights::Delete -bor $rights::DeleteSubdirectoriesAndFiles -bor $rights::ChangePermissions -bor $rights::TakeOwnership
foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
 if($rule.AccessControlType -eq 'Allow' -and $allowed -notcontains $rule.IdentityReference.Value -and ($rule.FileSystemRights -band $writes)){
  throw 'unprotected parent access'
 }
}
"""
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            env=dict(os.environ, KSAT_RECOVERY_PARENT=str(path)), shell=False,
            capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RecoveryError('paths_unprotected')
        return
    from client_install_guard import require_protected_directory
    require_protected_directory(path, allow_service=True)


def _create_protected_directory(path: Path) -> None:
    if os.name != 'nt':
        raise RecoveryError('paths_unprotected')
    from client_install_guard import secure_fresh_directory, require_protected_directory
    reject_links(path)
    if not path.exists():
        secure_fresh_directory(path)
    require_protected_directory(path)


def _check_tree(path: Path) -> None:
    reject_links(path)
    if path.is_dir():
        for child in path.iterdir():
            reject_links(child)
            if child.is_dir():
                _check_tree(child)
            elif not child.is_file() or child.stat().st_nlink != 1:
                raise RecoveryError('paths_unprotected')
    elif path.exists() and path.stat().st_nlink != 1:
        raise RecoveryError('paths_unprotected')


def _read(path: Path, limit: int) -> bytes:
    _check_tree(path)
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Resource exceeds supported size.')
    return data


def _hash(path: Path) -> str:
    _check_tree(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _durable_write(path: Path, data: bytes) -> None:
    reject_links(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def _existing_store(root: Path, *, writable=False, immutable=False):
    """Attach only to an existing schema, with strict matching anchor validation.

    ClientStore.__init__ deliberately repairs one-behind anchors and migrates
    schemas. Recovery must not do either, so attach its transaction/validation
    machinery explicitly, as inspect_replacement_safety does.
    """
    identity = DeviceIdentityStore(root / 'identity').load_existing()
    store = ClientStore.__new__(ClientStore)
    store.database_path = root / 'state/client.sqlite3'
    store._integrity_key = derive_state_integrity_key(identity)
    store._integrity_anchor_path = root / 'identity/state-anchor.json'
    store._lock = threading.RLock()
    store._closed = False
    store._anchor_recovery_required = False
    mode = 'rw' if writable else 'ro'
    store.connection = sqlite3.connect(store.database_path.as_uri() + '?mode=' + mode + ('&immutable=1' if immutable else ''), uri=True, timeout=2)
    store.connection.row_factory = sqlite3.Row
    try:
        store.connection.execute('PRAGMA foreign_keys=ON')
        store.connection.execute('PRAGMA synchronous=FULL')
        if not writable:
            store.connection.execute('PRAGMA query_only=ON')
        with store._read_transaction() as connection:
            if connection.execute('PRAGMA user_version').fetchone()[0] != _AUTHENTICATED_SCHEMA_VERSION:
                raise RecoveryError('unsupported_schema')
            store._validated_journal_chain(connection)
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RecoveryError('authenticated_state_invalid')
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RecoveryError('authenticated_state_invalid')
            for row in connection.execute('SELECT attempt_id FROM local_attempts').fetchall():
                store._validated_attempt_snapshot(connection, row[0], 'Invalid recovery state.')
        yield store
    finally:
        store.close()


@contextmanager
def _inspection_store(root: Path):
    # A read-only SQLite connection can still update shared-memory sidecars.
    # Copy a stable generation into the protected state directory first.
    sources = ('state/client.sqlite3', 'state/client.sqlite3-wal',
               'identity/device-key.bin', 'identity/state-anchor.json')
    def fingerprints():
        return {name: _hash(root / name) if (root / name).exists() else None for name in sources}
    before = fingerprints()
    with tempfile.TemporaryDirectory(prefix='.recovery-inspection-', dir=root / 'state') as directory:
        snapshot = Path(directory)
        (snapshot / 'state').mkdir()
        (snapshot / 'identity').mkdir()
        for name, digest in before.items():
            if digest is not None:
                shutil.copyfile(root / name, snapshot / name)
                if _hash(snapshot / name) != digest:
                    raise RecoveryError('authenticated_state_invalid')
        if fingerprints() != before:
            raise RecoveryError('authenticated_state_invalid')
        with _existing_store(snapshot) as store:
            yield store
        if fingerprints() != before:
            raise RecoveryError('authenticated_state_invalid')


def _preview_store(store: ClientStore) -> RecoveryPreview:
    candidates, problems = [], set()
    with store._read_transaction() as connection:
        if connection.execute("SELECT 1 FROM local_attempts WHERE state='in_progress'").fetchone():
            problems.add('active_attempt')
        for row in connection.execute('SELECT * FROM submission_outbox ORDER BY attempt_id').fetchall():
            pending = store._validated_outbox_row(connection, row, 'Invalid recovery state.')
            if pending.status != 'faculty_intervention_required':
                continue
            problem = safe_problem_code(pending.last_error)
            problems.add(problem)
            if problem != 'attempt_identity_mismatch':
                continue
            attempt = connection.execute('SELECT * FROM local_attempts WHERE attempt_id=?', (pending.attempt_id,)).fetchone()
            # The HMAC binds the displayed selection to all attempt and queue data,
            # without exposing the answer contents or the private integrity key.
            token = hmac.new(store._integrity_key, canonical_json([list(attempt), list(row)]), hashlib.sha256).hexdigest()
            candidates.append(RecoveryCandidate(pending.attempt_id, attempt['sealed_at'], token))
    return RecoveryPreview(tuple(candidates), tuple(sorted(problems)))


def _validate_selection(preview: RecoveryPreview, selected: tuple[RecoveryCandidate, ...]) -> None:
    if 'active_attempt' in preview.problem_codes:
        raise RecoveryError('active_attempt')
    available = {c.attempt_id: c for c in preview.candidates}
    if any(available.get(c.attempt_id) != c for c in selected):
        raise RecoveryError('selection_changed')


def _retained_rows(connection, selected_ids):
    """Check preservation inside the same transaction, including journal history."""
    state = {}
    for table in ('cached_content_packs', 'authenticated_state_journal', 'local_attempts',
                  'submission_outbox', 'local_responses', 'local_integrity_events'):
        rows = connection.execute(f'SELECT * FROM {table}').fetchall()
        if table not in {'cached_content_packs', 'authenticated_state_journal'}:
            rows = [row for row in rows if row['attempt_id'] not in selected_ids]
        state[table] = sorted((list(row) for row in rows), key=canonical_json)
    return state


def verify_archive(archive_path: Path) -> dict:
    """Verify hashes, identity decryption and authenticated snapshot readability.

    Does not restore files or repair an archive anchor. Call on this computer,
    where the original machine-bound DPAPI identity can be decrypted.
    """
    archive = Path(archive_path).absolute()
    try:
        _check_tree(archive)
        for path in (archive, archive / 'identity', archive / 'state'):
            _require_protected_directory(path)
        manifest = strict_json(_read(archive / 'manifest.json', 1024 * 1024), 1024 * 1024)
        expected_files = {'state/client.sqlite3', 'identity/device-key.bin', 'identity/state-anchor.json'}
        if (manifest['format_version'] != 1 or manifest['disposition'] != 'practice_discard_from_live'
                or set(manifest['sha256']) != expected_files):
            raise ValueError('Invalid manifest.')
        for name, digest in manifest['sha256'].items():
            if not hmac.compare_digest(_hash(archive / name), digest):
                raise ValueError('Archive hash mismatch.')
        identity = DeviceIdentityStore(archive / 'identity').load_existing()
        body = {k: v for k, v in manifest.items() if k != 'manifest_mac'}
        expected_mac = hmac.new(derive_state_integrity_key(identity), canonical_json(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_mac, manifest['manifest_mac']):
            raise ValueError('Archive manifest mismatch.')
        with _existing_store(archive, immutable=True) as store:
            preview = _preview_store(store)
            actual = {c.attempt_id: c.selection_token for c in preview.candidates}
            if (not manifest['selected_attempt_ids'] or
                    manifest['selection_tokens'] != {i: actual[i] for i in manifest['selected_attempt_ids']}):
                raise ValueError('Archive selection mismatch.')
        return manifest
    except Exception:
        raise RecoveryError('archive_failed', archive_path=archive) from None


def _remove_archived(store, selected, archive):
    """Remove only the exact selection already saved in a verified archive."""
    with store._transaction() as connection:
        store._verify_current_authenticated_state(connection)
        if connection.execute("SELECT 1 FROM local_attempts WHERE state='in_progress'").fetchone():
            raise RecoveryError('active_attempt', archive_path=archive)
        for candidate in selected:
            row = connection.execute('SELECT * FROM submission_outbox WHERE attempt_id=?', (candidate.attempt_id,)).fetchone()
            attempt = connection.execute('SELECT * FROM local_attempts WHERE attempt_id=?', (candidate.attempt_id,)).fetchone()
            if row is None or attempt is None:
                raise RecoveryError('selection_changed', archive_path=archive)
            token = hmac.new(store._integrity_key, canonical_json([list(attempt), list(row)]), hashlib.sha256).hexdigest()
            if (attempt['state'] != 'sealed_pending' or row['status'] != 'faculty_intervention_required'
                    or safe_problem_code(row['last_error']) != 'attempt_identity_mismatch'
                    or not hmac.compare_digest(candidate.selection_token, token)):
                raise RecoveryError('selection_changed', archive_path=archive)
        selected_ids = {candidate.attempt_id for candidate in selected}
        retained = _retained_rows(connection, selected_ids)
        for candidate in selected:
            for table in ('submission_outbox', 'local_responses', 'local_integrity_events', 'local_attempts'):
                connection.execute(f'DELETE FROM {table} WHERE attempt_id=?', (candidate.attempt_id,))
        if retained != _retained_rows(connection, selected_ids):
            raise RecoveryError('mutation_requires_inspection', archive_path=archive)
        for candidate in selected:
            if connection.execute('SELECT 1 FROM local_attempts WHERE attempt_id=?', (candidate.attempt_id,)).fetchone():
                raise RecoveryError('mutation_requires_inspection', archive_path=archive)


def archive_obsolete_attempts(data_dir: Path, store: ClientStore) -> RecoveryResult | None:
    """Apply the lab owner's obsolete-mismatch policy inside the running authority.

    Caller owns the maintenance gate and runtime lock. The store lock spans
    selection, snapshot and removal so uploads cannot change the archived rows.
    No SCM operation, database reopening or active-test mutation occurs here.
    """
    root = Path(data_dir).absolute()
    if Path(store.database_path).absolute() != root / 'state/client.sqlite3':
        raise RecoveryError('installation_missing')
    with store._lock:
        preview = _preview_store(store)
        if not preview.candidates or 'active_attempt' in preview.problem_codes:
            return None
        # The previous updater verifies that every baseline attempt survived.
        # Wait for that verification before applying this new retention policy.
        journal_path = root / 'updates/update-journal.json'
        if journal_path.exists():
            try:
                journal = strict_json(_read(journal_path, 16384), 16384)
                if journal.get('stage') not in {'healthy', 'rolled_back'}:
                    return None
            except (ValueError, OSError, AttributeError):
                return None
        backend = ClientRecovery(root)
        backend._validate_installation()
        archive = root / 'recovery' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex)
        backend._archive(store, archive, preview.candidates, automatic=True)
        _remove_archived(store, preview.candidates, archive)
        _durable_write(archive / 'outcome.json', canonical_json({'status': 'completed'}))
        return RecoveryResult(archive, tuple(c.attempt_id for c in preview.candidates))


class ClientRecovery:
    """Backend rooted at existing ProgramData/KSAT Client; never initializes it."""
    def __init__(self, data_dir: Path, *, service=None):
        self.root = Path(data_dir).absolute()
        self.service = service if service is not None else WindowsClientService()

    def _validate_installation(self) -> None:
        if not self.root.is_dir():
            raise RecoveryError('installation_missing')
        try:
            reject_links(self.root)
            _require_protected_directory(self.root, public_read=True)
            for name in ('state', 'identity'):
                path = self.root / name
                _check_tree(path)
                _require_protected_directory(path)
            for name in ('client-config.json', 'coordinator-url.json', 'recovery'):
                _check_tree(self.root / name)
        except Exception:
            raise RecoveryError('paths_unprotected') from None
        try:
            config = strict_json(_read(self.root / 'client-config.json', 65536), 65536)
            expected = {'coordinator_base_url', 'trusted_ca_path', 'coordinator_signing_public_key_b64'}
            if set(config) != expected or any(not isinstance(config[k], str) or not config[k] for k in expected):
                raise ValueError('Invalid configuration.')
            normalize_coordinator_base_url(config['coordinator_base_url'])
            runtime_path = self.root / 'coordinator-url.json'
            if runtime_path.exists():
                raw = _read(runtime_path, 65536)
                runtime = strict_json(raw, 65536)
                if set(runtime) != {'coordinator_base_url'}:
                    raise ValueError('Invalid configuration.')
                url = normalize_coordinator_base_url(runtime['coordinator_base_url'])
                if raw != canonical_json({'coordinator_base_url': url}):
                    raise ValueError('Invalid configuration.')
            ca = _read(Path(config['trusted_ca_path']), 256 * 1024)
        except Exception:
            raise RecoveryError('configuration_invalid') from None
        try:
            validate_ca(ca, config['coordinator_signing_public_key_b64'], now=datetime.now(timezone.utc))
        except Exception:
            raise RecoveryError('time_or_trust_invalid') from None
        try:
            identity = DeviceIdentityStore(self.root / 'identity').load_existing()
            if identity.device_id and identity.coordinator_public_key_b64 != config['coordinator_signing_public_key_b64']:
                raise ValueError('Identity trust mismatch.')
        except Exception:
            raise RecoveryError('identity_invalid') from None

    def _inspect(self) -> RecoveryPreview:
        self._validate_installation()
        try:
            with _inspection_store(self.root) as store:
                return _preview_store(store)
        except RecoveryError:
            raise
        except Exception:
            raise RecoveryError('authenticated_state_invalid') from None

    def preview(self) -> RecoveryPreview:
        try:
            return self._inspect()
        except RecoveryError as error:
            return RecoveryPreview(problem_codes=(error.code,))

    def _archive(self, store: ClientStore, archive: Path, selected: tuple[RecoveryCandidate, ...], *, automatic=False) -> None:
        for directory in (archive.parent, archive, archive / 'state', archive / 'identity'):
            _create_protected_directory(directory)
        with store._read_transaction():
            target = archive / 'state/client.sqlite3'
            with closing(sqlite3.connect(target)) as destination:
                store.connection.backup(destination)
            with target.open('rb+') as stream:
                os.fsync(stream.fileno())
            for name in ('device-key.bin', 'state-anchor.json'):
                _durable_write(archive / 'identity' / name, _read(self.root / 'identity' / name, 65536))
        files = ('state/client.sqlite3', 'identity/device-key.bin', 'identity/state-anchor.json')
        body = {'format_version': 1, 'client_version': CLIENT_VERSION,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'disposition': 'practice_discard_from_live',
                'selected_attempt_ids': [c.attempt_id for c in selected],
                'selection_tokens': {c.attempt_id: c.selection_token for c in selected},
                'sha256': {name: _hash(archive / name) for name in files}}
        if automatic:
            body['authorization'] = 'lab_policy_obsolete_identity_mismatch'
        body['manifest_mac'] = hmac.new(store._integrity_key, canonical_json(body), hashlib.sha256).hexdigest()
        _durable_write(archive / 'manifest.json', canonical_json(body))
        verify_archive(archive)
        _durable_write(archive / 'outcome.json', canonical_json({'status': 'prepared'}))

    def archive_practice(self, selected, *, confirmed_practice: bool = False) -> RecoveryResult:
        selected = tuple(selected)
        if (confirmed_practice is not True or not selected
                or any(not isinstance(c, RecoveryCandidate) for c in selected)
                or len({c.attempt_id for c in selected}) != len(selected)):
            raise RecoveryError('confirmation_required')
        archive, stopped, phase = None, False, 'service_control_failed'
        self._validate_installation()
        try:
            with MaintenanceGate(self.root):
                _validate_selection(self._inspect(), selected)  # before SCM stop
                try:
                    if self.service.is_running():
                        stopped = True  # also restore after a stop timeout
                        self.service.stop()
                    with ClientRecoveryLock(self.root / 'state'):
                        _validate_selection(self._inspect(), selected)  # after lifecycle lock
                        with _existing_store(self.root, writable=True) as store:
                            archive = self.root / 'recovery' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex)
                            phase = 'archive_failed'
                            self._archive(store, archive, selected)
                            phase = 'mutation_requires_inspection'
                            _remove_archived(store, selected, archive)
                            with _existing_store(self.root):
                                pass
                            _durable_write(archive / 'outcome.json', canonical_json({'status': 'completed'}))
                except BaseException:
                    # Do not reopen via the repairing constructor or copy a backup
                    # over live data. Even an uncertain commit keeps its evidence.
                    if archive is not None and (archive / 'manifest.json').exists():
                        try:
                            _durable_write(archive / 'outcome.json', canonical_json({'status': 'inspection_required', 'problem_code': phase}))
                        except OSError:
                            pass
                    raise
        except MaintenanceBusy:
            raise RecoveryError('maintenance_busy', archive_path=archive) from None
        except CoordinatorLockHeld:
            raise RecoveryError('client_busy', archive_path=archive) from None
        except RecoveryError:
            raise
        except Exception:
            raise RecoveryError(phase, archive_path=archive) from None
        finally:
            # Both locks must be released before starting the authority again.
            if stopped:
                try:
                    self.service.start()
                except Exception:
                    raise RecoveryError('service_restart_failed', archive_path=archive) from None
        return RecoveryResult(archive, tuple(c.attempt_id for c in selected))
