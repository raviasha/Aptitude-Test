# Distributed Lab Assessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-answer central-server assessment flow with a managed Windows lab client that runs faculty assessments locally and submits one centrally scored, durable response bundle.

**Architecture:** Keep `app.py` as the faculty coordinator and add focused `ksat` modules for shared protocol, cryptography, coordinator release/ticket/submission services, and the installed client. Faculty assessments use signed encrypted content packs, per-student attempt tickets, local SQLite autosave, and an idempotent submission outbox; existing question-bank administration, history, and personal-practice behavior remain available.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic, SQLite/WAL, `cryptography` (Ed25519 and AES-GCM), `httpx`, `psutil`, vanilla HTML/CSS/JavaScript, PyInstaller, Inno Setup, `unittest`

**Spec:** `docs/superpowers/specs/2026-08-31-distributed-lab-assessment-design.md`

## Global Constraints

- Support at most 100 concurrent students on institution-controlled Windows lab computers connected to the institutional network.
- Faculty launch opens a 10-minute start window; each student's duration begins when that student's attempt ticket is issued.
- Every student receives the same question identifiers in a different deterministic question order; answer choices are never shuffled.
- Correct answers, correct-answer flags, explanations, and solutions must never appear in client content packs or attempt tickets.
- Answer selection, navigation, timer handling, autosave, and exam-integrity recording must not make coordinator requests.
- In-progress recovery is supported only on the same computer and must retain the original deadline.
- A completed attempt is sealed locally, survives restart, and retries until the coordinator acknowledges it.
- The coordinator calculates the authoritative score and the client displays it immediately after acknowledgment.
- A 100-client simultaneous-submission test without loss, duplication, SQLite lock errors, or corruption is a release gate.
- The first release retains coordinator SQLite and must not require PostgreSQL, Redis, a broker, autoscaling, or paid cloud infrastructure.
- Existing historical attempts, results, question banks, student records, and personal-practice behavior must remain readable.
- A distributed assessment release is launched once; after any attempt ticket is issued, running the assessment again requires duplicating it into a new release with a new content key.
- Implementation uses TDD, preserves unrelated working-tree changes, and commits only the files named by each task.

## File and responsibility map

- `ksat/protocol.py`: versioned wire models, canonical JSON, deterministic question ordering, and typed error payloads.
- `ksat/crypto.py`: Ed25519 signing, AES-GCM pack encryption, hashing, and coordinator key loading.
- `ksat/sqlite.py`: consistent SQLite connection configuration for coordinator and client databases.
- `ksat/coordinator/schema.py`: additive coordinator migrations for devices, releases, distributed attempts, submissions, and audit events.
- `ksat/coordinator/auth.py`: device enrollment and stateless student access tokens bound to a device.
- `ksat/coordinator/releases.py`: immutable public-question snapshots and encrypted signed content packs.
- `ksat/coordinator/attempts.py`: launch-window enforcement and per-student attempt-ticket issuance.
- `ksat/coordinator/submissions.py`: validation, central scoring, idempotency, and single-writer persistence.
- `ksat/coordinator/routes.py`: `/api/client/v1` coordinator API used only by installed clients.
- `ksat/client/identity.py`: registered device key material protected with Windows DPAPI.
- `ksat/client/store.py`: local content cache, active attempt, responses, integrity events, and submission outbox.
- `ksat/client/runtime.py`: local lifecycle, timer, deterministic order, autosave, recovery, and sealing.
- `ksat/client/coordinator.py`: HTTPS transport, typed errors, content download, login, start, and submission.
- `ksat/client/outbox.py`: retry worker with idempotency and randomized exponential backoff.
- `client_app.py`: loopback-only FastAPI process for the installed lab client.
- `static/client/index.html`, `static/client/app.js`, `static/client/styles.css`: student client interface.
- `scripts/load_distributed_assessment.py`: realistic 100-client start and submission load gate.
- `installer/KSATCoordinator.iss`, `installer/KSATClient.iss`: separate Windows installers and permissions.
- `tests/`: focused unit, API, recovery, security, and concurrency tests for the new architecture.

---

### Task 1: Shared protocol and cryptographic primitives

**Files:**
- Create: `ksat/__init__.py`
- Create: `ksat/protocol.py`
- Create: `ksat/crypto.py`
- Create: `tests/__init__.py`
- Create: `tests/test_protocol.py`
- Create: `tests/test_crypto.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: standard `datetime`, `hashlib`, `json`, and `cryptography` Ed25519/AES-GCM APIs.
- Produces: `ReleaseManifest`, `AttemptTicket`, `SignedAttemptTicket`, `ResponseEntry`, `IntegrityEvent`, `ResponseBundle`, `SignedResponseBundle`, `SubmissionReceipt`, `ApiProblem`, `CoordinatorKeyring`, `canonical_json()`, `deterministic_question_order()`, `device_request_bytes()`, `sign_json()`, `verify_json()`, `encrypt_pack()`, `decrypt_pack()`, `sha256_hex()`, and `load_or_create_coordinator_keyring()`.

- [ ] **Step 1: Add the cryptographic and HTTP client dependencies**

Append exact runtime dependencies to `requirements.txt`:

```text
cryptography>=42.0
httpx>=0.27,<1
psutil>=5.9
```

- [ ] **Step 2: Write failing protocol-model and shuffle tests**

Create `tests/test_protocol.py`:

```python
import base64
import unittest
from datetime import datetime, timezone

from ksat.protocol import (
    AttemptTicket,
    ReleaseManifest,
    canonical_json,
    deterministic_question_order,
)


class ProtocolTests(unittest.TestCase):
    def test_canonical_json_is_stable(self):
        left = canonical_json({"b": 2, "a": "é"})
        right = canonical_json({"a": "é", "b": 2})
        self.assertEqual(left, right)
        self.assertEqual(left, b'{"a":"\xc3\xa9","b":2}')

    def test_shuffle_is_reproducible_and_preserves_membership(self):
        question_ids = [11, 12, 13, 14, 15]
        seed = base64.b64encode(bytes(range(32))).decode("ascii")
        first = deterministic_question_order(question_ids, seed)
        second = deterministic_question_order(question_ids, seed)
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(question_ids))
        self.assertNotEqual(first, question_ids)

    def test_ticket_never_contains_an_answer_key(self):
        ticket = AttemptTicket(
            attempt_id="attempt-1",
            student_id="S1",
            device_id="device-1",
            release_id="release-1",
            content_hash="a" * 64,
            started_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            deadline=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
            order_seed_b64=base64.b64encode(b"x" * 32).decode("ascii"),
            content_key_b64=base64.b64encode(b"k" * 32).decode("ascii"),
        )
        serialized = canonical_json(ticket).lower()
        self.assertNotIn(b"correct", serialized)
        self.assertNotIn(b"solution", serialized)
```

- [ ] **Step 3: Run the protocol tests and verify they fail**

Run: `python -m unittest tests.test_protocol -v`

Expected: `ModuleNotFoundError: No module named 'ksat.protocol'`.

- [ ] **Step 4: Implement the versioned protocol models and deterministic ordering**

Create `ksat/protocol.py` with the exact public constants and signatures:

```python
PROTOCOL_VERSION = 1
PACK_FORMAT_VERSION = 1
SHUFFLE_ALGORITHM = "sha256-rank-v1"


def canonical_json(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else (
        value.dict() if isinstance(value, BaseModel) else value
    )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deterministic_question_order(
    question_ids: Sequence[int], seed_b64: str, algorithm: str = SHUFFLE_ALGORITHM
) -> list[int]:
    if algorithm != SHUFFLE_ALGORITHM:
        raise ValueError(f"Unsupported shuffle algorithm: {algorithm}")
    seed = base64.b64decode(seed_b64, validate=True)
    if len(seed) != 32 or len(set(question_ids)) != len(question_ids):
        raise ValueError("A 32-byte seed and unique question IDs are required.")
    return sorted(question_ids, key=lambda item: hashlib.sha256(seed + str(item).encode("ascii")).digest())


def device_request_bytes(method: str, path: str, body: bytes, timestamp: str, nonce: str) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{body_hash}\n{timestamp}\n{nonce}".encode("utf-8")
```

Define Pydantic models with `extra="forbid"` behavior and these fields:

```python
class ReleaseManifest(BaseModel):
    protocol_version: int = PROTOCOL_VERSION
    pack_format_version: int = PACK_FORMAT_VERSION
    release_id: str
    test_id: int
    test_name: str
    duration_seconds: int
    canonical_question_ids: list[int]
    asset_names: list[str] = Field(default_factory=list)


class AttemptTicket(BaseModel):
    protocol_version: int = PROTOCOL_VERSION
    attempt_id: str
    student_id: str
    device_id: str
    release_id: str
    content_hash: str
    started_at: datetime
    deadline: datetime
    order_seed_b64: str
    content_key_b64: str
    shuffle_algorithm: str = SHUFFLE_ALGORITHM


class SignedAttemptTicket(BaseModel):
    ticket: AttemptTicket
    signature_b64: str


class ResponseEntry(BaseModel):
    question_id: int
    selected_answer: str | None


class IntegrityEvent(BaseModel):
    event_type: str
    occurred_at: datetime


class ResponseBundle(BaseModel):
    protocol_version: int = PROTOCOL_VERSION
    ticket: SignedAttemptTicket
    content_hash: str
    sealed_at: datetime
    responses: list[ResponseEntry]
    integrity_events: list[IntegrityEvent] = Field(default_factory=list)


class SignedResponseBundle(BaseModel):
    bundle: ResponseBundle
    device_signature_b64: str


class SubmissionReceipt(BaseModel):
    attempt_id: str
    accepted_at: datetime
    score: int
    total_questions: int
    attempted: int
    percentage: float
    violations: int


class ApiProblem(BaseModel):
    code: str
    message: str
    retryable: bool = False
```

- [ ] **Step 5: Write failing signing, encryption, and tamper tests**

Create `tests/test_crypto.py`:

```python
import os
import unittest

from ksat.crypto import (
    decrypt_pack,
    encrypt_pack,
    generate_ed25519_keypair,
    sha256_hex,
    sign_json,
    verify_json,
)


class CryptoTests(unittest.TestCase):
    def test_signatures_and_encryption_detect_tampering(self):
        private_b64, public_b64 = generate_ed25519_keypair()
        payload = {"release_id": "r1", "questions": [1, 2]}
        signature = sign_json(private_b64, payload)
        verify_json(public_b64, payload, signature)
        with self.assertRaises(ValueError):
            verify_json(public_b64, {"release_id": "r2"}, signature)

        key = os.urandom(32)
        encrypted = encrypt_pack(key, "r1", b"question content")
        self.assertEqual(decrypt_pack(key, "r1", encrypted), b"question content")
        damaged = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
        with self.assertRaises(ValueError):
            decrypt_pack(key, "r1", damaged)
        self.assertEqual(len(sha256_hex(encrypted)), 64)

    def test_coordinator_keyring_is_stable_and_refuses_corruption(self):
        first = load_or_create_coordinator_keyring(self.secrets_dir)
        second = load_or_create_coordinator_keyring(self.secrets_dir)
        self.assertEqual(first, second)
        (self.secrets_dir / "protocol-signing.key").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            load_or_create_coordinator_keyring(self.secrets_dir)
```

- [ ] **Step 6: Implement cryptographic helpers**

Create `ksat/crypto.py` using raw Ed25519 key serialization and an envelope of `12-byte nonce + AESGCM ciphertext`:

```text
generate_ed25519_keypair() -> tuple[str, str]
sign_json(private_key_b64, value) -> str
verify_json(public_key_b64, value, signature_b64) -> None
encrypt_pack(key: bytes, release_id: str, plaintext: bytes) -> bytes
decrypt_pack(key: bytes, release_id: str, envelope: bytes) -> bytes
sha256_hex(value: bytes) -> str
```

`generate_ed25519_keypair()` serializes private and public keys in raw 32-byte form before Base64 encoding. `sign_json()` signs `canonical_json(value)`. `verify_json()` returns normally only for a valid signature and otherwise raises `ValueError`. `encrypt_pack()` generates a fresh 12-byte nonce and uses `release_id.encode("utf-8")` as AES-GCM associated data; `decrypt_pack()` applies the same associated data. `sha256_hex()` returns `hashlib.sha256(value).hexdigest()`.

Add stable coordinator protocol secrets:

```text
@dataclass(frozen=True)
class CoordinatorKeyring:
    signing_private_key_b64: str
    signing_public_key_b64: str
    pack_master_key: bytes
    enrollment_code: str

load_or_create_coordinator_keyring(secrets_dir: Path) -> CoordinatorKeyring
```

The implementation writes a raw protocol-signing key, 32-byte pack master key, 24-character URL-safe enrollment code, and public metadata through temporary files plus `os.replace()`. If any private file exists but is malformed, raise `ValueError` and never silently rotate it. Installer permissions are applied in Task 12.

Convert `InvalidSignature`, `InvalidTag`, invalid Base64, and invalid key lengths into `ValueError` with stable messages. Never log key material, decrypted packs, passwords, or response bundles.

- [ ] **Step 7: Run the focused tests**

Run: `python -m unittest tests.test_protocol tests.test_crypto -v`

Expected: all protocol and crypto tests pass.

- [ ] **Step 8: Commit the shared foundation**

```bash
git add requirements.txt ksat/__init__.py ksat/protocol.py ksat/crypto.py tests/__init__.py tests/test_protocol.py tests/test_crypto.py
git commit -m "feat: add distributed assessment protocol"
```

---

### Task 2: Coordinator SQLite configuration and additive schema

**Files:**
- Create: `ksat/sqlite.py`
- Create: `ksat/coordinator/__init__.py`
- Create: `ksat/coordinator/schema.py`
- Create: `tests/test_coordinator_schema.py`
- Modify: `app.py:245-255`
- Modify: `app.py:887-990`

**Interfaces:**
- Consumes: `app.DB_PATH`, existing `ensure_column()`, and existing coordinator tables.
- Produces: `connect_sqlite(path: Path, *, wal: bool = True)`, `migrate_distributed_schema(connection)`, and coordinator tables/columns required by later tasks.

- [ ] **Step 1: Write failing connection and migration tests**

Create `tests/test_coordinator_schema.py`:

```python
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ksat.coordinator.schema import migrate_distributed_schema
from ksat.sqlite import connect_sqlite


class CoordinatorSchemaTests(unittest.TestCase):
    def test_connection_enables_wal_busy_timeout_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_sqlite(Path(directory) / "coordinator.db")
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 10_000)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            connection.close()

    def test_migration_is_idempotent_and_additive(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE tests (test_id INTEGER PRIMARY KEY);
            CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, test_id INTEGER, student_id TEXT);
        """)
        migrate_distributed_schema(connection)
        migrate_distributed_schema(connection)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"devices", "assessment_releases", "release_questions", "submissions", "audit_events"} <= tables)
        attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
        self.assertTrue({"release_id", "device_id", "order_seed", "ticket_json", "sealed_at", "submission_hash"} <= attempt_columns)
```

- [ ] **Step 2: Run the schema tests and verify they fail**

Run: `python -m unittest tests.test_coordinator_schema -v`

Expected: imports fail because `ksat.sqlite` and `ksat.coordinator.schema` do not exist.

- [ ] **Step 3: Implement consistent SQLite connection configuration**

Create `ksat/sqlite.py`:

```python
def connect_sqlite(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection
```

Change `app.db()` to call `connect_sqlite(DB_PATH)` and retain its current commit/close behavior. This removes the default zero-wait lock failure without changing callers.

- [ ] **Step 4: Implement the additive distributed schema**

Create `ksat/coordinator/schema.py`. `migrate_distributed_schema()` must create these records with foreign keys and indexes:

```sql
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  public_key_b64 TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  enrolled_at TEXT NOT NULL,
  last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS assessment_releases (
  release_id TEXT PRIMARY KEY,
  test_id INTEGER NOT NULL UNIQUE,
  state TEXT NOT NULL,
  duration_seconds INTEGER NOT NULL,
  manifest_json TEXT NOT NULL,
  content_pack_filename TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content_signature_b64 TEXT NOT NULL,
  wrapped_content_key_b64 TEXT NOT NULL,
  launch_opens_at TEXT,
  launch_closes_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(test_id) REFERENCES tests(test_id)
);
CREATE TABLE IF NOT EXISTS release_questions (
  release_id TEXT NOT NULL,
  question_id INTEGER NOT NULL,
  canonical_order INTEGER NOT NULL,
  PRIMARY KEY(release_id, question_id),
  UNIQUE(release_id, canonical_order),
  FOREIGN KEY(release_id) REFERENCES assessment_releases(release_id)
);
CREATE TABLE IF NOT EXISTS submissions (
  attempt_id TEXT PRIMARY KEY,
  bundle_hash TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  attempt_id TEXT,
  test_id INTEGER,
  details_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
```

Add columns to `tests`: `release_id TEXT` and `launch_closes_at TEXT`. Add columns to `attempts`: `release_id TEXT`, `device_id TEXT`, `order_seed TEXT`, `ticket_json TEXT`, `sealed_at TEXT`, and `submission_hash TEXT`. Add indexes on release state, attempt release/student, and audit attempt/test.

- [ ] **Step 5: Wire the migration into startup**

At the end of the existing `ensure_schema()` transaction in `app.py`, call `migrate_distributed_schema(connection)`. Do not rename or drop existing tables or columns. Add a regression test to `tests/test_coordinator_schema.py` that calls `app.ensure_schema()` twice against a temporary `DB_PATH` and then reads an existing `students` table plus the new `assessment_releases` table.

- [ ] **Step 6: Run focused and existing schema-dependent tests**

Run: `python -m unittest tests.test_coordinator_schema test_registration.StudentRegistrationTests.test_student_can_register_and_duplicate_id_is_rejected -v`

Expected: all tests pass and the second migration is a no-op.

- [ ] **Step 7: Commit the schema foundation**

```bash
git add app.py ksat/sqlite.py ksat/coordinator/__init__.py ksat/coordinator/schema.py tests/test_coordinator_schema.py
git commit -m "feat: add coordinator distributed schema"
```

---

### Task 3: Device enrollment and stateless client authentication

**Files:**
- Create: `ksat/coordinator/auth.py`
- Create: `ksat/coordinator/routes.py`
- Create: `tests/test_client_auth_api.py`
- Modify: `ksat/protocol.py`
- Modify: `app.py:151-245`
- Modify: `app.py:1618-1700`

**Interfaces:**
- Consumes: `devices`, existing `students`, `bcrypt`, coordinator session secret, and Ed25519 public keys.
- Produces: `DeviceEnrollmentRequest`, `DeviceEnrollmentReceipt`, `ClientLoginRequest`, `ClientSession`, `register_device()`, `verify_device_request()`, `issue_student_access_token()`, `verify_student_access_token()`, and `/api/client/v1/devices/enroll`, `/api/client/v1/session`.

- [ ] **Step 1: Add failing API tests for enrollment and device-bound login**

Create `tests/test_client_auth_api.py` using a temporary data directory and FastAPI `TestClient`:

```python
class ClientAuthApiTests(unittest.TestCase):
    def test_enrollment_requires_the_configured_code(self):
        response = self.client.post("/api/client/v1/devices/enroll", json={
            "label": "Lab-01",
            "public_key_b64": self.public_key,
            "enrollment_code": "wrong",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_enrollment_code")

    def test_student_session_is_bound_to_active_device(self):
        enrolled = self.enroll("Lab-01")
        response = self.device_post("/api/client/v1/session", enrolled["device_id"], json={
            "student_id": "S100",
            "password": "student123",
            "device_id": enrolled["device_id"],
        })
        self.assertEqual(response.status_code, 200)
        claims = verify_student_access_token(
            self.session_secret, response.json()["access_token"], max_age_seconds=43_200
        )
        self.assertEqual(claims, {"student_id": "S100", "device_id": enrolled["device_id"]})
```

The fixture must set `KSAT_DEVICE_ENROLLMENT_CODE=lab-enroll-test`, create student `S100`, and restore environment and `app.DB_PATH` in teardown.

- [ ] **Step 2: Run the API tests and verify they fail**

Run: `python -m unittest tests.test_client_auth_api -v`

Expected: both `/api/client/v1` routes return 404.

- [ ] **Step 3: Add exact authentication wire models**

Add to `ksat/protocol.py`:

```python
class DeviceEnrollmentRequest(BaseModel):
    label: str
    public_key_b64: str
    enrollment_code: str


class DeviceEnrollmentReceipt(BaseModel):
    device_id: str
    coordinator_public_key_b64: str


class ClientLoginRequest(BaseModel):
    student_id: str
    password: str
    device_id: str


class ClientSession(BaseModel):
    access_token: str
    student_id: str
    student_name: str
    device_id: str
    expires_in_seconds: int = 43_200
```

- [ ] **Step 4: Implement enrollment and stateless access tokens**

Create `ksat/coordinator/auth.py` with:

```python
TOKEN_SALT = "ksat-client-session-v1"

def issue_student_access_token(secret: str, student_id: str, device_id: str) -> str:
    return URLSafeTimedSerializer(secret, salt=TOKEN_SALT).dumps(
        {"student_id": student_id, "device_id": device_id}
    )
```

The remaining exact public signatures are:

```text
register_device(connection, request, *, expected_enrollment_code,
                coordinator_public_key_b64, now_iso) -> DeviceEnrollmentReceipt
verify_student_access_token(secret, token, *, max_age_seconds=43_200) -> dict[str, str]
```

`verify_student_access_token()` calls `URLSafeTimedSerializer.loads(max_age=max_age_seconds)`, requires exactly nonblank string claims named `student_id` and `device_id`, and converts expired or malformed signatures into the `invalid_client_session` problem code.

Enrollment must validate a constant-time comparison of the configured code, a nonblank label, and a valid 32-byte Ed25519 public key. Login must require an active registered device and valid existing student credentials. The stateless token eliminates `student_sessions` heartbeat writes for the installed client; the legacy browser session behavior remains unchanged for personal practice.

Except for initial enrollment, every client request includes `X-KSAT-Device`, `X-KSAT-Timestamp`, `X-KSAT-Nonce`, and `X-KSAT-Signature`. `verify_device_request()` loads the active device public key and verifies a signature over `device_request_bytes(method, path, body, timestamp, nonce)`. Reject timestamps outside ±5 minutes and reuse of the same nonce within the process-lifetime replay cache. Attempt start additionally requires the student bearer token bound to that device. Submission does not require a live student token because a sealed outbox must upload automatically after restart; its signed ticket, request proof, and device-signed bundle provide authentication.

- [ ] **Step 5: Expose the client authentication router**

Create `ksat/coordinator/routes.py` with an `APIRouter(prefix="/api/client/v1")`. Route dependencies read `request.app.state.coordinator_config`, whose startup value contains `db_path`, `session_secret`, `device_enrollment_code`, and coordinator signing keys. Add `configure_coordinator_state(app)` in `app.py`; it calls `load_or_create_coordinator_keyring(DATA_DIR / "secrets")`, and include the router once.

Define the state boundary in `ksat/coordinator/routes.py`:

```python
@dataclass
class CoordinatorConfig:
    db_path: Path
    data_dir: Path
    session_secret: str
    device_enrollment_code: str
    signing_private_key_b64: str
    signing_public_key_b64: str
    pack_master_key: bytes
    submission_writer: Any | None = None
```

Task 6 replaces the initial `None` writer during application startup; routes that do not submit remain usable before then in focused tests.

Return structured HTTP errors such as `detail={"code": "invalid_device_key", "message": "The device key is invalid.", "retryable": False}`. Use these exact codes: `invalid_enrollment_code`, `invalid_device_key`, `device_inactive`, and `invalid_credentials`.

- [ ] **Step 6: Run authentication and legacy-login regressions**

Run: `python -m unittest tests.test_client_auth_api test_registration.StudentRegistrationTests.test_student_can_register_and_duplicate_id_is_rejected -v`

Expected: new client authentication and existing browser registration/login behavior pass.

- [ ] **Step 7: Commit device enrollment and authentication**

```bash
git add app.py ksat/protocol.py ksat/coordinator/auth.py ksat/coordinator/routes.py tests/test_client_auth_api.py
git commit -m "feat: enroll lab clients and issue device-bound sessions"
```

---

### Task 4: Immutable assessment releases and encrypted content packs

**Files:**
- Create: `ksat/coordinator/releases.py`
- Create: `tests/test_assessment_releases.py`
- Modify: `ksat/protocol.py`
- Modify: `app.py:1480-1568`
- Modify: `app.py:2302-2386`
- Modify: `static/app.js:419-434`

**Interfaces:**
- Consumes: existing question selection rules, `sample_questions()`, stored questions/stimuli/media, coordinator signing key, coordinator pack-master key, and the schema from Task 2.
- Produces: `PublicQuestion`, `ReleaseSummary`, `prepare_release()`, `load_release_manifest()`, `unwrap_release_content_key()`, and an assessment-specific `.ksatpack` file containing no answer material.

- [ ] **Step 1: Write failing release security and immutability tests**

Create `tests/test_assessment_releases.py`:

```python
class AssessmentReleaseTests(unittest.TestCase):
    def test_release_freezes_questions_and_excludes_answers(self):
        release = self.prepare_release_with_two_questions()
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        content_key = unwrap_release_content_key(self.master_key, release.release_id, release.wrapped_content_key_b64)
        payload = decrypt_pack(content_key, release.release_id, encrypted)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            questions = json.loads(archive.read("questions.json"))
            names = set(archive.namelist())
        serialized = json.dumps(questions).lower()
        self.assertEqual([item["question_id"] for item in questions], release.canonical_question_ids)
        self.assertNotIn("correct_answer", serialized)
        self.assertNotIn("solution", serialized)
        self.assertIn("manifest.json", names)

        self.connection.execute("UPDATE questions SET question_text='changed' WHERE question_id=?", (release.canonical_question_ids[0],))
        persisted = load_release_manifest(self.connection, release.release_id)
        self.assertEqual(persisted.content_hash, release.content_hash)
```

Also assert that two student starts later reference the same release question IDs and that the pack signature verifies with the coordinator public key.

- [ ] **Step 2: Run the release tests and verify they fail**

Run: `python -m unittest tests.test_assessment_releases -v`

Expected: `ksat.coordinator.releases` is missing.

- [ ] **Step 3: Define public content models with no answer fields**

Add to `ksat/protocol.py`:

```python
class PublicQuestion(BaseModel):
    question_id: int
    source_key: str
    category: str
    chapter: str
    difficulty: str
    question_text: str
    question_html: str = ""
    options: dict[str, str]
    stimulus: dict[str, Any] | None = None
    display_media: dict[str, Any] = Field(default_factory=dict)


class ReleaseSummary(BaseModel):
    release_id: str
    test_id: int
    state: str
    duration_seconds: int
    canonical_question_ids: list[int]
    content_pack_filename: str
    content_hash: str
    content_signature_b64: str
    wrapped_content_key_b64: str
```

Do not add an answer, explanation, feedback, score, or solution field to `PublicQuestion`.

- [ ] **Step 4: Implement deterministic release packaging**

Create `ksat/coordinator/releases.py` with exact entry points:

```text
prepare_release(connection, *, test_id, selected_questions, assets, pack_dir,
                signing_private_key_b64, pack_master_key, now_iso) -> ReleaseSummary
load_release_manifest(connection, release_id) -> ReleaseSummary
wrap_release_content_key(master_key, release_id, content_key) -> str
unwrap_release_content_key(master_key, release_id, wrapped_b64) -> bytes
```

Build a ZIP in memory with `manifest.json`, `questions.json`, and sanitized `assets/<sha256>.<extension>` entries. Sort question and asset entries, use fixed ZIP timestamps, hash the encrypted envelope, sign `{release_id, content_hash, manifest}`, write `<release_id>.ksatpack` atomically, and insert release and `release_questions` rows in one transaction. If a release already exists for a test, return it without resampling or rewriting.

- [ ] **Step 5: Freeze a test's question selection when the faculty test is created**

Extract a helper in `app.py`:

```text
public_release_material(connection: sqlite3.Connection, selected: list[sqlite3.Row])
    -> tuple[list[PublicQuestion], dict[str, bytes]]
```

It must reuse existing text repair, `question_options()`, stimulus, and display-media logic while omitting `correct_answer`, explanation, solution steps, option explanations, and solution media. Rewrite media references to sanitized embedded asset names rather than coordinator URLs. After `create_test()` inserts a faculty test, sample once, build its release, and store `tests.release_id`. If pack generation fails, roll back the test creation and remove the temporary pack.

For pre-upgrade faculty tests whose `release_id` is null, `launch_test()` must prepare the release once before launch. Historical submitted attempts are not resampled.

- [ ] **Step 6: Show release preparation status in the faculty UI**

Extend `/api/admin/tests` with `release_state`, `release_id`, and `content_hash`. In `static/app.js`, render `Preparing`, `Ready`, or `Preparation failed` beside each faculty test. Disable Launch unless state is `prepared` or an existing release can be prepared successfully by the launch request. Preserve the existing Create/Delete controls.

- [ ] **Step 7: Run release, package, and existing media tests**

Run: `python -m unittest tests.test_assessment_releases test_question_media -v`

Expected: release security tests and all existing question-media tests pass.

- [ ] **Step 8: Commit immutable releases**

```bash
git add app.py static/app.js ksat/protocol.py ksat/coordinator/releases.py tests/test_assessment_releases.py
git commit -m "feat: freeze encrypted assessment releases"
```

---

### Task 5: Content pre-staging, launch window, and per-student attempt tickets

**Files:**
- Create: `ksat/coordinator/attempts.py`
- Create: `tests/test_distributed_attempt_start.py`
- Modify: `ksat/protocol.py`
- Modify: `ksat/coordinator/routes.py`
- Modify: `app.py:2302-2418`
- Modify: `static/app.js:419-434`

**Interfaces:**
- Consumes: `ReleaseSummary`, device-bound student token claims, release content key, existing student/test records, and `SignedAttemptTicket`.
- Produces: `AttemptStartResponse`, `list_prefetchable_releases()`, `issue_attempt_ticket()`, `/api/client/v1/releases`, `/api/client/v1/releases/{release_id}/pack`, `/api/client/v1/assessments`, and `/api/client/v1/attempts/start`.

- [ ] **Step 1: Write failing tests for pre-staging and independent timers**

Create `tests/test_distributed_attempt_start.py` with a fixed UTC clock:

```python
class DistributedAttemptStartTests(unittest.TestCase):
    def test_pack_is_downloadable_before_launch_but_key_is_not_disclosed(self):
        catalog = self.device_get("/api/client/v1/releases")
        item = next(entry for entry in catalog.json()["releases"] if entry["release_id"] == self.release_id)
        self.assertNotIn("content_key_b64", item)
        pack = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(pack.status_code, 200)
        self.assertEqual(hashlib.sha256(pack.content).hexdigest(), item["content_hash"])

    def test_students_get_same_questions_different_orders_and_independent_deadlines(self):
        first = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        second = self.start("S101", "device-b", at="2026-08-31T09:07:00+00:00")
        first_ticket = first.json()["ticket"]["ticket"]
        second_ticket = second.json()["ticket"]["ticket"]
        self.assertNotEqual(first_ticket["order_seed_b64"], second_ticket["order_seed_b64"])
        self.assertEqual(first.json()["canonical_question_ids"], second.json()["canonical_question_ids"])
        self.assertEqual(first_ticket["deadline"], "2026-08-31T09:32:00Z")
        self.assertEqual(second_ticket["deadline"], "2026-08-31T09:37:00Z")

    def test_start_after_ten_minute_window_is_rejected(self):
        response = self.start("S102", "device-c", at="2026-08-31T09:10:01+00:00")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "start_window_closed")
```

Use a 30-minute release duration and launch timestamps from 09:00:00 through 09:10:00 UTC. Starting exactly at 09:10:00 is allowed; 09:10:01 is rejected.

- [ ] **Step 2: Run the attempt-start tests and verify they fail**

Run: `python -m unittest tests.test_distributed_attempt_start -v`

Expected: release catalog and attempt-start routes return 404.

- [ ] **Step 3: Implement launch state without a global exam deadline**

Change `launch_test()` so it sets:

```python
launch_opens_at = current_utc
launch_closes_at = current_utc + timedelta(minutes=10)
state = "launched"
```

Update the release row and `tests.launched/tests.launch_closes_at`; do not set one shared `attempts.expires_at`. Closing a test prevents new starts but does not change already issued attempt deadlines. Replace the faculty UI's global exam timer with a `Start window` countdown.

Reject a second launch after any ticket has been issued for the release with `release_already_used`. A faculty `Duplicate assessment` action in Task 11 creates a new test, release ID, encrypted pack, and content key so previously issued client keys cannot open a future session.

- [ ] **Step 4: Implement pre-stage and student-assessment catalog queries**

In `ksat/coordinator/attempts.py`, add:

```text
list_prefetchable_releases(connection) -> list[dict[str, Any]]
list_launched_assessments(connection, *, student_id, device_id, now_utc)
    -> list[dict[str, Any]]
```

The device-signed pre-stage catalog returns release ID, filename, encrypted content hash, pack signature, byte size, and pack format version, but never a content key. The student catalog returns only launched, unexpired releases for which the student has not submitted an accepted attempt.

Serve pack files with `ETag` equal to the content hash, `Cache-Control: private, immutable`, and support `If-None-Match` with HTTP 304. Resolve filenames from the database and reject path traversal.

- [ ] **Step 5: Implement per-student ticket issuance**

Add to `ksat/coordinator/attempts.py`:

Add this wire model to `ksat/protocol.py`:

```python
class AttemptStartResponse(BaseModel):
    ticket: SignedAttemptTicket
    canonical_question_ids: list[int]
    server_time: datetime
```

Expose the service signature:

```text
issue_attempt_ticket(connection, *, release_id, student_id, device_id,
                     confirmed_content_hash, signing_private_key_b64,
                     pack_master_key, now_utc) -> AttemptStartResponse
```

Within one transaction, verify active device, eligibility, launch window, exact content hash, and absence of a submitted attempt. For an existing in-progress attempt on the same device, return the original stored ticket. Reject resume on a different device with `attempt_bound_to_other_device`. For a new attempt, generate UUID attempt ID and 32 random seed bytes, set `started_at=now_utc`, set `expires_at=now_utc + duration_seconds`, sign the ticket, and store its canonical JSON. Do not insert response rows at start.

- [ ] **Step 6: Expose the content and attempt API**

Add these routes under `/api/client/v1`. All four verify the device request proof; assessment listing and attempt start also verify the student bearer token:

```text
GET  /releases
GET  /releases/{release_id}/pack
GET  /assessments
POST /attempts/start
```

`POST /attempts/start` accepts `release_id` and `confirmed_content_hash`. It derives student and device from the access token rather than trusting request-body identity fields. Return exact typed errors: `content_not_ready`, `content_hash_mismatch`, `assessment_not_launched`, `start_window_closed`, `already_submitted`, and `attempt_bound_to_other_device`.

- [ ] **Step 7: Run start-window and legacy assessment tests**

Run: `python -m unittest tests.test_distributed_attempt_start test_registration.StudentRegistrationTests.test_student_sees_only_launched_test_when_one_exists -v`

Expected: distributed start semantics pass and the legacy browser can still list faculty tests during the migration period.

- [ ] **Step 8: Commit launch and attempt issuance**

```bash
git add app.py static/app.js ksat/protocol.py ksat/coordinator/attempts.py ksat/coordinator/routes.py tests/test_distributed_attempt_start.py
git commit -m "feat: issue per-student distributed attempts"
```

---

### Task 6: Idempotent central scoring and burst-safe submission writer

**Files:**
- Create: `ksat/coordinator/submissions.py`
- Create: `tests/test_distributed_submission.py`
- Create: `tests/test_submission_concurrency.py`
- Modify: `ksat/coordinator/routes.py`
- Modify: `app.py:1428-1451`
- Modify: `app.py:1573-1615`

**Interfaces:**
- Consumes: `SignedResponseBundle`, device public key, signed attempt ticket, release questions, private coordinator answer key, existing result serialization, and `submissions`.
- Produces: `ScoredSubmission`, `validate_and_score()`, `SubmissionWriter.submit()`, and `POST /api/client/v1/submissions`.

- [ ] **Step 1: Write failing validation, scoring, and idempotency tests**

Create `tests/test_distributed_submission.py`:

```python
class DistributedSubmissionTests(unittest.TestCase):
    def test_server_scores_bundle_and_duplicate_returns_same_receipt(self):
        signed_bundle = self.bundle({self.q1: "B", self.q2: None})
        first = self.submit(signed_bundle)
        second = self.submit(signed_bundle)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(first.json()["score"], 1)
        self.assertEqual(first.json()["attempted"], 1)
        self.assertEqual(self.count("submissions"), 1)
        self.assertEqual(self.count("responses"), 2)

    def test_modified_or_foreign_device_bundle_is_rejected(self):
        bundle = self.bundle({self.q1: "B", self.q2: "A"})
        bundle.bundle.responses[0].selected_answer = "A"
        response = self.submit(bundle)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "invalid_bundle_signature")

    def test_late_offline_upload_is_accepted_when_sealed_by_deadline(self):
        response = self.submit(
            self.bundle({self.q1: "B", self.q2: "C"}, sealed_at=self.deadline),
            received_at=self.deadline + timedelta(hours=1),
        )
        self.assertEqual(response.status_code, 200)
```

Add rejection cases for missing/extra question IDs, an option not present in the question, content-hash mismatch, a seal timestamp more than five seconds after deadline, and a ticket whose coordinator signature is invalid.

- [ ] **Step 2: Run the submission tests and verify they fail**

Run: `python -m unittest tests.test_distributed_submission -v`

Expected: submission route returns 404.

- [ ] **Step 3: Implement validation and central scoring outside the write transaction**

Create `ksat/coordinator/submissions.py`:

```python
@dataclass(frozen=True)
class ScoredSubmission:
    attempt_id: str
    student_id: str
    release_id: str
    sealed_at: str
    bundle_hash: str
    bundle_json: str
    responses: tuple[tuple[int, str | None, int, str, str], ...]
    score: int
    attempted: int
    total_questions: int
    percentage: float
    violations: tuple[IntegrityEvent, ...]


validate_and_score(connection, signed_bundle, *, coordinator_public_key_b64,
                   received_at) -> ScoredSubmission | SubmissionReceipt
```

Verify coordinator ticket signature, device signature, ticket/attempt/release/device/student equality, content hash, exact release question set, allowed option keys, and `sealed_at <= deadline + 5 seconds`. Read correct answers only inside this coordinator function. After authenticating the ticket, device signature, student, and attempt identity, return an existing stored receipt before comparing response content so a lost acknowledgment can always be recovered safely by attempt ID.

- [ ] **Step 4: Implement one bounded submission writer**

Implement:

```text
SubmissionWriter(db_path: Path, *, max_pending: int = 200)
SubmissionWriter.start() -> None
SubmissionWriter.stop(timeout_seconds: float = 10.0) -> None
SubmissionWriter.submit(scored, timeout_seconds: float = 15.0) -> SubmissionReceipt
SubmissionWriter.pending_count -> int
```

Use `queue.Queue(maxsize=200)` and one worker thread. Each queued item carries a `concurrent.futures.Future`. The worker opens a configured SQLite connection and stores, in one transaction: all response rows, attempt score/status/submission fields, integrity events, immutable submission JSON/hash, and `submitted_at`. A full queue returns `submission_busy` with HTTP 503 and `Retry-After: 1`. A timed-out caller receives a retryable timeout; its client retains the bundle and retries idempotently.

- [ ] **Step 5: Start and stop the writer with the FastAPI lifespan**

Create one `SubmissionWriter` in coordinator startup state, call `start()` after schema migration, and call `stop()` during shutdown. The submission route performs parsing, signature validation, and scoring in the request thread, then calls the writer only for the short database commit.

- [ ] **Step 6: Write the 100-submission concurrency test**

Create `tests/test_submission_concurrency.py` that prepares 100 students/devices/attempts and uses `ThreadPoolExecutor(max_workers=32)` plus a `threading.Barrier(100)` to submit together. Assert:

```python
self.assertEqual(len(receipts), 100)
self.assertEqual(len({item.attempt_id for item in receipts}), 100)
self.assertEqual(self.count("submissions"), 100)
self.assertEqual(self.count("attempts", "status='submitted'"), 100)
self.assertNotIn("database is locked", " ".join(errors).lower())
```

Then resubmit all 100 bundles and assert counts remain unchanged.

- [ ] **Step 7: Run submission correctness and concurrency tests**

Run: `python -m unittest tests.test_distributed_submission tests.test_submission_concurrency -v`

Expected: all bundles are accepted exactly once with no lock errors.

- [ ] **Step 8: Commit submission scoring and serialization**

```bash
git add app.py ksat/coordinator/submissions.py ksat/coordinator/routes.py tests/test_distributed_submission.py tests/test_submission_concurrency.py
git commit -m "feat: score idempotent distributed submissions"
```

---

### Task 7: Device identity and local client persistence

**Files:**
- Create: `ksat/client/__init__.py`
- Create: `ksat/client/identity.py`
- Create: `ksat/client/store.py`
- Create: `tests/test_client_identity.py`
- Create: `tests/test_client_store.py`

**Interfaces:**
- Consumes: Ed25519 helpers, Windows DPAPI through `ctypes`, client data directory, and `connect_sqlite()`.
- Produces: `SecretProtector`, `WindowsDpapiProtector`, `DeviceIdentityStore`, `DeviceIdentity`, `AttemptSealedError`, `LocalAttemptRecord`, `PendingSubmission`, and `ClientStore` methods for cache, attempts, answers, events, sealing, outbox, and acknowledgment.

- [ ] **Step 1: Write failing device-key protection tests**

Create `tests/test_client_identity.py` with an injected reversible test protector:

```python
class PrefixProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("Protected device key is invalid.")
        return value.removeprefix(b"protected:")


class DeviceIdentityTests(unittest.TestCase):
    def test_identity_is_generated_once_and_private_key_is_not_plaintext(self):
        store = DeviceIdentityStore(self.directory, PrefixProtector())
        first = store.load_or_create()
        second = store.load_or_create()
        self.assertEqual(first.public_key_b64, second.public_key_b64)
        raw_file = (self.directory / "device-key.bin").read_bytes()
        self.assertNotIn(base64.b64decode(first.private_key_b64), raw_file)
```

Also test that `WindowsDpapiProtector` is selected by default on Windows and that corrupt protected material produces a stable `ValueError` without generating a replacement identity.

- [ ] **Step 2: Write failing local-store lifecycle tests**

Create `tests/test_client_store.py`:

```python
class ClientStoreTests(unittest.TestCase):
    def test_answer_save_seal_and_acknowledgment_are_transactional(self):
        self.store.cache_pack(self.release_id, self.hash, self.pack_path, verified=True)
        self.store.create_attempt(self.ticket, [3, 1, 2])
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.assertEqual(self.store.load_attempt(self.attempt_id).responses[3], "B")
        sealed = self.store.seal_attempt(self.attempt_id, sealed_at=self.deadline)
        self.assertEqual(sealed.state, "sealed_pending")
        with self.assertRaises(AttemptSealedError):
            self.store.save_answer(self.attempt_id, 3, "A", saved_at=self.deadline)
        self.assertEqual(len(self.store.pending_submissions()), 1)
        self.store.acknowledge(self.attempt_id, self.receipt)
        self.assertEqual(self.store.load_attempt(self.attempt_id).state, "acknowledged")
        self.assertEqual(self.store.pending_submissions(), [])
```

Add a test that closes and reopens the SQLite file at each state and sees the same data.

- [ ] **Step 3: Run client identity and store tests and verify they fail**

Run: `python -m unittest tests.test_client_identity tests.test_client_store -v`

Expected: `ksat.client` modules are missing.

- [ ] **Step 4: Implement DPAPI-backed device identity**

Create `ksat/client/identity.py` with:

```text
SecretProtector.protect(value: bytes) -> bytes
SecretProtector.unprotect(value: bytes) -> bytes

DeviceIdentity(private_key_b64: str, public_key_b64: str, device_id: str | None)
DeviceIdentityStore(data_dir: Path, protector: SecretProtector)
DeviceIdentityStore.load_or_create() -> DeviceIdentity
DeviceIdentityStore.save_enrollment(device_id, coordinator_public_key_b64) -> DeviceIdentity
```

Implement `WindowsDpapiProtector` with `CryptProtectData`/`CryptUnprotectData` and `CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN`. Write key and enrollment files atomically. Set restrictive file ACLs in the installer task; never silently replace a corrupt identity because that would break device binding and recovery.

- [ ] **Step 5: Implement the local SQLite schema and store**

Create `ksat/client/store.py`. The schema must include:

```sql
cached_content_packs(release_id PRIMARY KEY, content_hash, pack_path, verified, cached_at)
local_attempts(attempt_id PRIMARY KEY, release_id, ticket_json, question_order_json,
               state, deadline, remaining_seconds, last_wall_time, created_at,
               sealed_at, receipt_json)
local_responses(attempt_id, question_id, selected_answer, saved_at,
                PRIMARY KEY(attempt_id, question_id))
local_integrity_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                       attempt_id, event_type, occurred_at)
submission_outbox(attempt_id PRIMARY KEY, bundle_json, retry_count,
                  next_attempt_at, last_error, created_at)
```

Define exact returned records:

```python
class AttemptSealedError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalAttemptRecord:
    attempt_id: str
    release_id: str
    state: str
    deadline: datetime
    question_order: tuple[int, ...]
    responses: dict[int, str | None]
    remaining_seconds: int
    sealed_at: datetime | None
    receipt: SubmissionReceipt | None


@dataclass(frozen=True)
class PendingSubmission:
    attempt_id: str
    bundle: SignedResponseBundle
    retry_count: int
    next_attempt_at: datetime
    last_error: str | None
```

Expose exact methods:

```python
cache_pack(); verified_pack(); create_attempt(); load_attempt(); active_attempt();
save_answer(); record_integrity_event(); update_timer_checkpoint(); seal_attempt();
pending_submissions(); record_retry(); acknowledge()
```

Every state-changing method uses one transaction. `save_answer()` requires `state='in_progress'`; `seal_attempt()` changes state and inserts the outbox record atomically; `acknowledge()` stores the receipt, changes state, and removes the outbox row atomically.

- [ ] **Step 6: Run local persistence tests**

Run: `python -m unittest tests.test_client_identity tests.test_client_store -v`

Expected: identity remains stable and attempt/outbox data survives database reopen.

- [ ] **Step 7: Commit identity and persistence**

```bash
git add ksat/client/__init__.py ksat/client/identity.py ksat/client/store.py tests/test_client_identity.py tests/test_client_store.py
git commit -m "feat: persist local lab client attempts"
```

---

### Task 8: Local assessment runtime, timer, recovery, and sealing

**Files:**
- Create: `ksat/client/runtime.py`
- Create: `tests/test_client_runtime.py`
- Modify: `ksat/client/store.py`

**Interfaces:**
- Consumes: verified encrypted pack, `SignedAttemptTicket`, `deterministic_question_order()`, device private key, `ClientStore`, and injected wall/monotonic clocks.
- Produces: `AssessmentRuntime.prepare()`, `start()`, `answer()`, `record_violation()`, `snapshot()`, `tick()`, `recover()`, and `submit()`.

- [ ] **Step 1: Write failing instant-answer, recovery, and expiry tests**

Create `tests/test_client_runtime.py`:

```python
class ClientRuntimeTests(unittest.TestCase):
    def test_answer_is_local_and_recovery_never_extends_deadline(self):
        runtime = self.runtime_at("2026-08-31T09:00:00+00:00")
        runtime.start(self.start_response)
        runtime.answer(self.q1, "B")
        self.assertEqual(self.transport.calls, [])
        self.clock.advance(minutes=5)
        recovered = self.reopen_runtime().recover()
        self.assertEqual(recovered.responses[self.q1], "B")
        self.assertEqual(recovered.remaining_seconds, 25 * 60)

        self.clock.set_wall_time("2026-08-31T08:00:00+00:00")
        rolled_back = self.reopen_runtime().recover()
        self.assertLessEqual(rolled_back.remaining_seconds, 25 * 60)

    def test_expiry_seals_once_and_creates_signed_bundle(self):
        runtime = self.runtime_at("2026-08-31T09:00:00+00:00")
        runtime.start(self.start_response)
        runtime.answer(self.q1, "B")
        self.clock.advance(minutes=30)
        snapshot = runtime.tick()
        self.assertEqual(snapshot.state, "sealed_pending")
        pending = self.store.pending_submissions()
        self.assertEqual(len(pending), 1)
        verify_json(self.device_public_key, pending[0].bundle.bundle, pending[0].device_signature_b64)
```

Also test option validation, question-order reproduction, no answer/solution fields after pack decryption, manual submit with unanswered questions, and inability to edit after sealing.

- [ ] **Step 2: Run runtime tests and verify they fail**

Run: `python -m unittest tests.test_client_runtime -v`

Expected: `ksat.client.runtime` is missing.

- [ ] **Step 3: Implement a testable trusted clock**

Define:

```text
Clock.utcnow() -> datetime
Clock.monotonic() -> float
```

```python
@dataclass(frozen=True)
class AttemptSnapshot:
    attempt_id: str
    state: str
    question_order: tuple[int, ...]
    responses: dict[int, str | None]
    remaining_seconds: int
    violations: int
```

At start, anchor the ticket's absolute deadline to `monotonic()`. During execution, derive remaining time from monotonic elapsed. Persist `remaining_seconds` and `last_wall_time` at least on every answer, violation, question change, and 10-second timer checkpoint. On recovery calculate `min(persisted_remaining_seconds, max(0, deadline - wall_now))`; a wall-clock rollback therefore cannot increase remaining time. If online server time is later supplied, take the minimum again.

- [ ] **Step 4: Implement pack verification and local start**

`prepare()` verifies encrypted hash and coordinator signature before caching. `start()` verifies the signed ticket, confirms release/hash/device, decrypts the pack, validates question IDs, computes `deterministic_question_order()`, and creates the local attempt before returning the first snapshot. Reject any pack containing keys matching `correct_answer`, `solution`, `solution_steps`, `option_explanations`, or `feedback` recursively.

- [ ] **Step 5: Implement local interactions and sealing**

`answer()` validates the question belongs to the release and the choice is one of its unchanged option keys, then calls only `ClientStore.save_answer()`. `record_violation()` deduplicates identical events within two seconds locally. `submit()` and `tick()` call one private `_seal(at)` method, which builds responses for every canonical question ID, includes the integrity log, signs the canonical `ResponseBundle` with the device key, and atomically writes `sealed_pending` plus the outbox row.

- [ ] **Step 6: Run runtime and persistence tests**

Run: `python -m unittest tests.test_client_runtime tests.test_client_store -v`

Expected: local answers, restart recovery, rollback resistance, and one-time sealing pass without transport calls.

- [ ] **Step 7: Commit the local runtime**

```bash
git add ksat/client/runtime.py ksat/client/store.py tests/test_client_runtime.py
git commit -m "feat: run and recover assessments locally"
```

---

### Task 9: Coordinator transport and durable submission retry

**Files:**
- Create: `ksat/client/coordinator.py`
- Create: `ksat/client/outbox.py`
- Create: `tests/test_client_coordinator.py`
- Create: `tests/test_client_outbox.py`
- Modify: `ksat/client/store.py`

**Interfaces:**
- Consumes: coordinator `/api/client/v1` API, trusted coordinator CA file, device identity, client session token, `ClientStore`, and `SignedResponseBundle`.
- Produces: `CoordinatorClient.enroll()`, `login()`, `prefetch_catalog()`, `download_pack()`, `assessments()`, `start_attempt()`, `submit_bundle()`, and `OutboxWorker`.

- [ ] **Step 1: Write failing typed-transport tests**

Create `tests/test_client_coordinator.py` with `httpx.MockTransport`:

```python
class CoordinatorClientTests(unittest.TestCase):
    def test_problem_response_preserves_code_and_retryability(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            503,
            headers={"Retry-After": "1"},
            json={"detail": {"code": "submission_busy", "message": "Submission is queued.", "retryable": True}},
        ))
        client = self.make_client(transport)
        with self.assertRaises(CoordinatorProblem) as caught:
            client.submit_bundle(self.bundle)
        self.assertEqual(caught.exception.code, "submission_busy")
        self.assertTrue(caught.exception.retryable)

    def test_pack_download_rejects_hash_mismatch(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"damaged"))
        with self.assertRaises(ContentVerificationError):
            self.make_client(transport).download_pack("r1", "a" * 64, self.destination)
        self.assertFalse(self.destination.exists())
```

Also assert that HTTPS certificate verification is configured from `coordinator-ca.pem`, the access token is sent as a bearer token, and partial pack files are atomically renamed only after hash verification.

- [ ] **Step 2: Write failing outbox retry tests**

Create `tests/test_client_outbox.py` with fake clock, random source, and coordinator:

```python
class ClientOutboxTests(unittest.TestCase):
    def test_retry_survives_restart_and_acknowledges_exactly_once(self):
        self.coordinator.results = [ConnectionError("offline"), self.receipt]
        worker = OutboxWorker(self.store, self.coordinator, self.clock, random_source=lambda: 0.5)
        worker.process_due_once()
        pending = self.store.pending_submissions()[0]
        self.assertEqual(pending.retry_count, 1)
        self.assertIn("offline", pending.last_error)

        self.clock.advance(seconds=2)
        restarted = OutboxWorker(self.reopen_store(), self.coordinator, self.clock, random_source=lambda: 0.5)
        restarted.process_due_once()
        self.assertEqual(self.reopen_store().pending_submissions(), [])
        self.assertEqual(self.reopen_store().load_attempt(self.attempt_id).state, "acknowledged")
```

Add tests that retryable HTTP failures back off at 1, 2, 4, 8, 16, and 30-second caps with ±20% jitter, while nonretryable `invalid_bundle_signature` remains sealed and records `faculty_intervention_required` without deleting the bundle.

- [ ] **Step 3: Run transport and outbox tests and verify they fail**

Run: `python -m unittest tests.test_client_coordinator tests.test_client_outbox -v`

Expected: coordinator and outbox modules are missing.

- [ ] **Step 4: Implement the HTTPS coordinator client**

Create `ksat/client/coordinator.py`:

```text
CoordinatorProblem(code, message, retryable, retry_after: float | None = None)
CoordinatorClient(base_url, ca_file, identity, *, transport=None, timeout_seconds=10.0)
CoordinatorClient.enroll(label, enrollment_code) -> DeviceEnrollmentReceipt
CoordinatorClient.login(student_id, password) -> ClientSession
CoordinatorClient.prefetch_catalog() -> list[dict[str, Any]]
CoordinatorClient.download_pack(release_id, expected_hash, destination) -> Path
CoordinatorClient.assessments() -> list[dict[str, Any]]
CoordinatorClient.start_attempt(release_id, confirmed_content_hash) -> AttemptStartResponse
CoordinatorClient.submit_bundle(bundle) -> SubmissionReceipt
```

Require an `https://` base URL outside tests. Parse coordinator errors into `CoordinatorProblem`; never replace a structured message with `Something went wrong`. Use a `.partial` file and `os.replace()` for pack downloads. Store the student token only in process memory and clear it on logout.

Before each non-enrollment request, generate a fresh UUID nonce and UTC timestamp, build `device_request_bytes()`, and add the four device-proof headers signed with the protected device private key. Add `Authorization: Bearer <student token>` only to assessment listing and start. `submit_bundle()` intentionally works without that bearer token so the outbox drains after a client restart or while no student is signed in.

- [ ] **Step 5: Implement the persistent outbox worker**

Create `ksat/client/outbox.py`:

```text
OutboxWorker(store, coordinator, clock, random_source=random.random)
OutboxWorker.start() -> None
OutboxWorker.stop(timeout_seconds: float = 5.0) -> None
OutboxWorker.wake() -> None
OutboxWorker.process_due_once() -> int
```

The background thread sleeps on a `Condition`, not a fixed busy loop. Retry network errors, timeouts, HTTP 429, `submission_busy`, and server 5xx. Persist retry count, next time, and exact error before sleeping. On receipt, call `ClientStore.acknowledge()` before notifying UI listeners. Preserve nonretryable bundles and expose their error state for faculty intervention.

- [ ] **Step 6: Run transport, outbox, and sealing tests**

Run: `python -m unittest tests.test_client_coordinator tests.test_client_outbox tests.test_client_runtime -v`

Expected: offline retry survives process/store reopen and acknowledged attempts leave the outbox exactly once.

- [ ] **Step 7: Commit client transport and outbox**

```bash
git add ksat/client/coordinator.py ksat/client/outbox.py ksat/client/store.py tests/test_client_coordinator.py tests/test_client_outbox.py
git commit -m "feat: retry sealed submissions from lab clients"
```

---

### Task 10: Loopback client application and student interface

**Files:**
- Create: `client_app.py`
- Create: `static/client/index.html`
- Create: `static/client/app.js`
- Create: `static/client/styles.css`
- Create: `tests/test_client_app_api.py`
- Create: `tests/test_client_ui_contract.py`

**Interfaces:**
- Consumes: `DeviceIdentityStore`, `ClientStore`, `AssessmentRuntime`, `CoordinatorClient`, `OutboxWorker`, existing branding assets, and client configuration.
- Produces: loopback routes for enrollment, login, prefetch, assessment discovery, local attempt state, local answer/violation operations, sealing, queued status, receipt, and logout.

- [ ] **Step 1: Write failing loopback API tests proving answer clicks are local**

Create `tests/test_client_app_api.py` with injected fake coordinator/runtime/store:

```python
class ClientAppApiTests(unittest.TestCase):
    def test_answer_route_saves_locally_without_coordinator_call(self):
        response = self.client.put(
            f"/api/attempts/{self.attempt_id}/responses/{self.q1}",
            json={"answer": "B"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selected_answer"], "B")
        self.assertEqual(self.fake_coordinator.calls, [])

    def test_submit_locks_immediately_when_coordinator_is_offline(self):
        self.fake_coordinator.offline = True
        response = self.client.post(f"/api/attempts/{self.attempt_id}/submit", json={"confirmed": True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "sealed_pending")
        self.assertEqual(response.json()["message"], "Your answers are safe and will upload automatically.")
```

Also test same-machine resume after app reconstruction, timer expiry sealing, acknowledged result display, corrupt-local-data error, and an intervention-required nonretryable submission.

- [ ] **Step 2: Write failing UI contract tests**

Create `tests/test_client_ui_contract.py` that reads `static/client/app.js` and asserts:

```python
self.assertIn("Your answers are safe and will upload automatically.", source)
self.assertIn("data-answer", source)
self.assertIn("sealed_pending", source)
self.assertNotIn("/api/client/v1", source)
self.assertNotIn("Something went wrong", source)
```

The browser must call only the loopback `client_app.py`; coordinator credentials, CA handling, content keys, and device private keys remain in the Python client process.

- [ ] **Step 3: Run client API and UI tests and verify they fail**

Run: `python -m unittest tests.test_client_app_api tests.test_client_ui_contract -v`

Expected: `client_app.py` and client static files are missing.

- [ ] **Step 4: Build the loopback FastAPI application**

Create `create_client_app(services: ClientServices | None = None) -> FastAPI` in `client_app.py`. `ClientServices` contains identity, store, runtime, coordinator, and outbox. Production configuration is read from `%ProgramData%\KSAT Client\client-config.json`; tests inject services.

Define the injection boundary explicitly:

```python
@dataclass
class ClientServices:
    identity_store: DeviceIdentityStore
    store: ClientStore
    runtime: AssessmentRuntime
    coordinator: CoordinatorClient
    outbox: OutboxWorker
```

Expose these loopback-only routes:

```text
GET  /api/state
POST /api/device/enroll
POST /api/login
POST /api/logout
POST /api/content/prefetch
GET  /api/assessments
POST /api/assessments/{release_id}/start
GET  /api/attempts/active
GET  /api/attempts/{attempt_id}
PUT  /api/attempts/{attempt_id}/responses/{question_id}
POST /api/attempts/{attempt_id}/violations
POST /api/attempts/{attempt_id}/submit
GET  /api/attempts/{attempt_id}/result
```

Bind production Uvicorn only to `127.0.0.1:8010`. The startup lifespan opens the store, starts the outbox, restores or expires an active attempt, and starts background pack prefetch when a device is enrolled. Shutdown stops the outbox and closes resources.

- [ ] **Step 5: Implement the student interface**

Create a dedicated client UI that reuses `static/branding`, `static/styles.css`, and `static/math.css` but does not reuse central API calls from `static/app.js`. The UI states are:

```text
device_setup -> login -> waiting_or_ready -> in_progress
             -> sealed_pending -> acknowledged_result
             -> faculty_intervention_required
```

On answer click, mark the button selected immediately, call the loopback answer route, and retain the prior selection if the local write reports an error. Render a visible local-save indicator. On submit or timer expiry, disable all answer controls before calling the local submit route. In `sealed_pending`, show the durable safety message and retry state; never offer an edit or back-to-exam action. On acknowledgment, render the coordinator score immediately.

Keep the existing full-screen, focus-loss, copy/cut/paste, and context-menu protections, but send violations only to the local route. Reenter full-screen after restart before showing an in-progress attempt.

- [ ] **Step 6: Add explicit user-facing error mapping**

Map exact problem codes to messages:

```javascript
const problemMessages = {
  coordinator_unavailable: 'The assessment server is temporarily unavailable. Your saved work is safe.',
  start_window_closed: 'The 10-minute start window has closed. Ask Faculty for help.',
  content_hash_mismatch: 'The assessment download failed verification and will be downloaded again.',
  device_inactive: 'This lab computer is not registered. Ask Faculty or IT for help.',
  corrupt_local_attempt: 'Saved assessment data could not be verified. Do not close the application; ask Faculty for help.',
  faculty_intervention_required: 'The sealed submission needs Faculty attention. Your answers remain saved on this computer.',
};
```

Unknown errors use `The requested action could not be completed.` plus a local diagnostic reference, not a generic claim that work was lost.

- [ ] **Step 7: Run loopback API, UI, runtime, and feedback regressions**

Run: `python -m unittest tests.test_client_app_api tests.test_client_ui_contract tests.test_client_runtime test_feedback_ui -v`

Expected: installed-client flow passes and the existing web feedback UI remains unchanged.

- [ ] **Step 8: Commit the installed client application**

```bash
git add client_app.py static/client/index.html static/client/app.js static/client/styles.css tests/test_client_app_api.py tests/test_client_ui_contract.py
git commit -m "feat: add installed lab assessment client"
```

---

### Task 11: Faculty operations, audit trail, and compatibility migration

**Files:**
- Create: `scripts/upgrade_distributed_assessments.py`
- Create: `tests/test_distributed_admin.py`
- Create: `tests/test_distributed_migration.py`
- Modify: `app.py:2302-2440`
- Modify: `app.py:2080-2180`
- Modify: `static/app.js:385-460`
- Modify: `README.md`

**Interfaces:**
- Consumes: existing faculty dashboard/test/student administration, distributed release/attempt/submission tables, and audit events.
- Produces: device administration, start/submission statuses, void-and-reauthorize workflow, per-student/global duration extension audit, and a safe upgrade command.

- [ ] **Step 1: Write failing faculty-operation tests**

Create `tests/test_distributed_admin.py`:

```python
class DistributedAdminTests(unittest.TestCase):
    def test_dashboard_counts_distributed_attempt_states(self):
        data = self.admin_get("/api/admin/tests").json()
        test = next(item for item in data["tests"] if item["test_id"] == self.test_id)
        self.assertEqual(test["distributed_status"], {
            "eligible": 3, "started": 2, "submitted": 1, "voided": 0,
        })

    def test_void_and_reauthorize_preserves_audit(self):
        response = self.admin_post(
            f"/api/admin/attempts/{self.attempt_id}/void",
            {"reason": "Lab computer hardware failure", "authorize_retake": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["retake_authorized"])
        event = self.connection.execute(
            "SELECT * FROM audit_events WHERE attempt_id=? ORDER BY audit_id DESC", (self.attempt_id,)
        ).fetchone()
        self.assertEqual(event["event_type"], "attempt_voided")
        self.assertIn("hardware failure", event["details_json"])
```

Add tests that an accepted submission cannot be voided without a separate faculty confirmation flag, closing the start window does not seal active attempts, and a +5 minute extension adds five minutes to each active attempt plus the duration offered to students who have not started.

- [ ] **Step 2: Write failing migration tests**

Create `tests/test_distributed_migration.py` with a pre-upgrade fixture containing students, a question bank, a faculty test, a submitted attempt, responses, and violations. Run the upgrade twice and assert historical row counts/scores are unchanged, new tables exist, submitted tests are not resampled, and an unlaunched faculty test receives one prepared release.

- [ ] **Step 3: Run admin and migration tests and verify they fail**

Run: `python -m unittest tests.test_distributed_admin tests.test_distributed_migration -v`

Expected: distributed admin fields/routes and upgrade script are missing.

- [ ] **Step 4: Implement faculty status and device administration**

Extend the faculty APIs and UI to show:

- device label, ID, enrollment time, active/inactive state, and revoke action;
- release preparation status and content hash prefix;
- 10-minute start-window countdown;
- eligible, started, submitted, and voided counts;
- per-attempt device, start, deadline, submit time, score, and violations;
- submission-queue pressure from `SubmissionWriter.pending_count`.

Add `Duplicate assessment` for a used release. It copies test name with `Copy`, selection rules, difficulty, and duration, then runs normal release preparation to create a new question snapshot and encryption key. It does not copy attempts, submissions, scores, or audit events.

Add admin routes to rotate the enrollment code, revoke/reactivate devices, and inspect an exact attempt order derived from its stored seed. Never expose content keys, device public-key details beyond a short fingerprint, answer keys, or response-bundle JSON in the general dashboard.

- [ ] **Step 5: Implement audited void, retake, and extension operations**

Add Pydantic payloads with nonblank reason fields and routes:

```text
POST /api/admin/attempts/{attempt_id}/void
POST /api/admin/attempts/{attempt_id}/extend
POST /api/admin/tests/{test_id}/extend
```

Voiding changes status to `voided`, leaves local/central evidence intact, records the faculty actor/reason, and allows one new attempt only when `authorize_retake=true`. Extending a test updates the release duration for not-yet-started students and adds the same delta to existing active attempt deadlines; extending one attempt affects only that attempt. Every operation writes `audit_events` in the same transaction.

- [ ] **Step 6: Implement the compatibility upgrade command**

Create `scripts/upgrade_distributed_assessments.py` with:

```text
upgrade(db_path: Path, data_dir: Path, *, dry_run: bool = False) -> dict[str, int]
```

The command refuses to run while a faculty assessment has `status='in_progress'`, creates an automatic timestamped backup, runs additive migrations, prepares releases only for unlaunched/unsubmitted faculty tests lacking a release, and prints counts for devices, prepared releases, preserved students, preserved attempts, and preserved responses. `--dry-run` opens the backup copy and never modifies the live database.

- [ ] **Step 7: Preserve the existing practice and history paths**

Add regression assertions that `/api/student/practice/start`, retry-incorrect, result history, CSV export, and database backup still read existing rows. During rollout, the coordinator browser student page directs faculty-assessment starts to the installed client but leaves personal practice behavior unchanged.

- [ ] **Step 8: Run admin, migration, registration, and feedback suites**

Run: `python -m unittest tests.test_distributed_admin tests.test_distributed_migration test_registration test_feedback_ui -v`

Expected: all distributed operations and existing records/practice tests pass.

- [ ] **Step 9: Commit faculty operations and migration**

```bash
git add app.py static/app.js README.md scripts/upgrade_distributed_assessments.py tests/test_distributed_admin.py tests/test_distributed_migration.py
git commit -m "feat: manage distributed assessments from faculty console"
```

---

### Task 12: Coordinator TLS, secrets, and separate Windows installers

**Files:**
- Create: `ksat/coordinator/tls.py`
- Create: `coordinator_main.py`
- Create: `installer/KSATCoordinator.iss`
- Create: `installer/KSATClient.iss`
- Create: `tests/test_coordinator_tls.py`
- Modify: `build-windows.bat`
- Modify: `WINDOWS_EXE_BUILD.md`
- Modify: `app.py:2510-2565`

**Interfaces:**
- Consumes: Windows ProgramData directories, coordinator signing/pack keys, TLS certificate/key, client CA certificate, PyInstaller, and Inno Setup.
- Produces: first-run coordinator secrets, HTTPS coordinator executable, loopback client executable, separate installers, client config, and certificate trust installation.

Use version `2.0.0` for both applications and installers because the deployment topology and client protocol are incompatible with the current `1.3.3` single-server package. Update `APP_VERSION`, build output names, and both Inno Setup `AppVersion` values together.

- [ ] **Step 1: Write failing first-run secret and certificate tests**

Create `tests/test_coordinator_tls.py`:

```python
class CoordinatorTlsTests(unittest.TestCase):
    def test_first_run_creates_stable_keys_and_lan_certificate(self):
        first = load_or_create_coordinator_security(self.directory, hostname="ksat-coordinator.local")
        second = load_or_create_coordinator_security(self.directory, hostname="ksat-coordinator.local")
        self.assertEqual(first.signing_public_key_b64, second.signing_public_key_b64)
        self.assertEqual(first.ca_certificate_pem, second.ca_certificate_pem)
        certificate = x509.load_pem_x509_certificate(first.server_certificate_pem)
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn("ksat-coordinator.local", names.get_values_for_type(x509.DNSName))
        self.assertNotIn(first.signing_private_key_b64.encode(), first.ca_certificate_pem)
```

Also test atomic recovery after a missing public metadata file, refusal to overwrite corrupt private key material, and generated enrollment code length of at least 20 random characters.

- [ ] **Step 2: Run TLS tests and verify they fail**

Run: `python -m unittest tests.test_coordinator_tls -v`

Expected: `ksat.coordinator.tls` is missing.

- [ ] **Step 3: Implement coordinator first-run security material**

Create `ksat/coordinator/tls.py`:

```python
@dataclass(frozen=True)
class CoordinatorSecurity:
    signing_private_key_b64: str
    signing_public_key_b64: str
    pack_master_key: bytes
    enrollment_code: str
    ca_certificate_pem: bytes
    server_certificate_pem: bytes
    server_private_key_pem: bytes


load_or_create_coordinator_security(data_dir: Path, *, hostname: str) -> CoordinatorSecurity
```

Reuse `load_or_create_coordinator_keyring()` for the Ed25519 protocol signing key, 32-byte pack master key, and 24-character enrollment code. Generate the local CA and server certificate with configured DNS name plus current LAN IP addresses. Store private files under `%ProgramData%\KSAT Coordinator\secrets`; write atomically and refuse silent regeneration of corrupt material. Export only `coordinator-ca.pem`, hostname, and coordinator signing public key for client installation.

- [ ] **Step 4: Create explicit coordinator and client entry points**

`coordinator_main.py` loads security, configures `app.state`, and runs Uvicorn on `0.0.0.0:8443` with `ssl_certfile` and `ssl_keyfile`. It opens the faculty UI at `https://127.0.0.1:8443` and retains existing stale-process handling adapted to port 8443.

`client_app.py` remains bound to `127.0.0.1:8010`. Its config requires coordinator URL, trusted CA path, and expected coordinator signing public key. Refuse startup if the URL is non-HTTPS or public-key configuration is absent.

- [ ] **Step 5: Build two executables**

Modify `build-windows.bat` to build:

```text
dist\KSATCoordinator.exe  <- coordinator_main.py + faculty static/templates
dist\KSATClient.exe       <- client_app.py + client static/branding/math assets
```

Add `--collect-all cryptography --collect-all httpx --collect-all psutil` and keep FastAPI/Starlette/Uvicorn collections. Use distinct temporary work/spec paths. Fail the batch file if either executable or installer build fails.

- [ ] **Step 6: Create coordinator and client installers**

`KSATCoordinator.iss` installs for administrators, creates `%ProgramData%\KSAT Coordinator` with administrators/SYSTEM write access, opens private-network TCP 8443, launches the coordinator, and exposes the public CA export folder read-only.

`KSATClient.iss` installs `KSATClient.exe`, prompts IT for coordinator hostname and the exported `coordinator-ca.pem`, validates both exist, writes `%ProgramData%\KSAT Client\client-config.json`, installs the CA certificate with `certutil -addstore Root`, grants the client data directory the minimum write permission needed by the lab user, and creates a desktop shortcut. It does not open an inbound firewall port.

- [ ] **Step 7: Run security tests and build both executables/installers**

Run: `python -m unittest tests.test_coordinator_tls tests.test_crypto tests.test_client_identity -v`

Then run: `build-windows.bat`

Expected: both test suites pass and the release directory contains one coordinator installer and one client installer. Inspect PyInstaller output for missing cryptography backends before accepting the build.

- [ ] **Step 8: Commit TLS and packaging**

```bash
git add app.py coordinator_main.py client_app.py build-windows.bat WINDOWS_EXE_BUILD.md ksat/coordinator/tls.py installer/KSATCoordinator.iss installer/KSATClient.iss tests/test_coordinator_tls.py
git commit -m "build: package coordinator and lab client separately"
```

---

### Task 13: End-to-end load gate, outage recovery, and rollout evidence

**Files:**
- Create: `scripts/load_distributed_assessment.py`
- Create: `tests/test_distributed_end_to_end.py`
- Create: `docs/distributed-assessment-operations.md`
- Modify: `README.md`
- Modify: `WINDOWS_EXE_BUILD.md`

**Interfaces:**
- Consumes: completed coordinator/client protocol, two installers, temporary coordinator database, and real HTTP clients.
- Produces: reproducible 30-client and 100-client validation, resource/latency report, outage-retry evidence, and deployment runbook.

- [ ] **Step 1: Write the failing end-to-end acceptance test**

Create `tests/test_distributed_end_to_end.py`:

```python
class DistributedEndToEndTests(unittest.TestCase):
    def test_client_starts_answers_offline_submits_and_receives_score(self):
        self.coordinator.launch(self.test_id, at=self.launch_time)
        client = self.installed_client("device-01", "S100")
        client.prefetch()
        attempt = client.start(self.release_id, at=self.launch_time + timedelta(minutes=2))
        client.disconnect()
        client.answer(attempt.attempt_id, self.q1, "B")
        client.restart()
        client.submit(attempt.attempt_id)
        self.assertEqual(client.state(attempt.attempt_id).state, "sealed_pending")
        client.reconnect()
        client.process_outbox()
        result = client.result(attempt.attempt_id)
        self.assertEqual(result.score, 1)
        self.assertEqual(self.coordinator.submission_count(attempt.attempt_id), 1)
```

Add an expiry-offline case, lost-acknowledgment replay, corrupt-pack redownload, same-student/different-device rejection, and faculty void/retry authorization.

- [ ] **Step 2: Run the acceptance test and verify any remaining integration gap fails**

Run: `python -m unittest tests.test_distributed_end_to_end -v`

Expected before integration fixes: at least one concrete wiring failure between the real coordinator route, client transport, local runtime, and outbox. Record the exact failing assertion; do not weaken it.

- [ ] **Step 3: Complete only the wiring required by the acceptance test**

Connect production service factories so `coordinator_main.py` uses the real schema/auth/release/attempt/submission services and `client_app.py` uses the real identity/store/runtime/transport/outbox services. Keep dependency injection for tests. Add no new feature behavior in this step.

- [ ] **Step 4: Implement the reproducible load runner**

Create `scripts/load_distributed_assessment.py` with command-line arguments:

```text
--base-url https://ksat-coordinator.local:8443
--ca-file coordinator-ca.pem
--clients 100
--questions 100
--start-spread-seconds 30
--submission-spread-seconds 0
--report load-report.json
```

The runner provisions test fixtures only when `KSAT_LOAD_TEST=1`, prefixes every student ID, device label, and test name with `LOAD-<uuid>`, prefetches one shared pack, enrolls 100 simulated device keys, starts clients within the 10-minute window, creates signed bundles, synchronizes submission with a barrier when spread is zero, retries retryable failures, and writes JSON containing request counts, status/error codes, p50/p95/p99 latency, coordinator CPU/memory, SQLite size, maximum writer queue depth, duplicate count, and missing-attempt count. In a final transaction, delete only records reachable from that exact `LOAD-<uuid>` test/student/device prefix and report the deleted row counts.

- [ ] **Step 5: Run the full automated suite**

Run:

```text
python -m unittest discover -v
```

Expected: all existing and distributed tests pass with zero failures and zero errors.

- [ ] **Step 6: Run the 30-client reproduction and 100-client release gate**

Against the intended coordinator hardware, run:

```text
python scripts/load_distributed_assessment.py --base-url https://ksat-coordinator.local:8443 --ca-file coordinator-ca.pem --clients 30 --questions 100 --start-spread-seconds 30 --submission-spread-seconds 0 --report load-report-30.json
python scripts/load_distributed_assessment.py --base-url https://ksat-coordinator.local:8443 --ca-file coordinator-ca.pem --clients 100 --questions 100 --start-spread-seconds 30 --submission-spread-seconds 0 --report load-report-100.json
```

Release thresholds are exact: zero lost attempts, zero duplicate results, zero corruption/signature errors, zero `database is locked` errors, 100 accepted authoritative results, local answer latency below 100 ms, and submission acknowledgment p95 below 10 seconds. If any threshold fails, retain the report and return to the responsible task instead of increasing server capacity.

- [ ] **Step 7: Verify outage recovery under load**

Start the 100-client submission run, stop the coordinator after clients seal, wait until every client reports `sealed_pending`, restart the coordinator, and allow the outboxes to drain. The resulting report must show 100 acknowledged attempts, no editable sealed attempt, no duplicate, and no lost bundle.

- [ ] **Step 8: Write the operator runbook**

Create `docs/distributed-assessment-operations.md` with exact procedures for coordinator installation, CA export, client installation, device enrollment/revocation, assessment preparation, 10-minute launch, monitoring statuses, queued submission recovery, machine-failure void/retry, backup/restore, key/certificate backup, enrollment-code rotation, upgrade dry-run, load-test execution, and diagnostic-log collection. Link it from `README.md` and `WINDOWS_EXE_BUILD.md`.

- [ ] **Step 9: Perform final build and artifact checks**

Run `build-windows.bat`, install both packages on two disposable Windows machines or VMs, enroll the client, run one assessment through coordinator outage/restart, and verify immediate score display after acknowledgment. Confirm the client installer opens no inbound firewall rule and the client package/archive contains no answer metadata by searching extracted strings for `correct_answer`, `solution_steps`, and known seeded correct answers.

- [ ] **Step 10: Commit load tooling and operations documentation**

```bash
git add scripts/load_distributed_assessment.py tests/test_distributed_end_to_end.py docs/distributed-assessment-operations.md README.md WINDOWS_EXE_BUILD.md
git commit -m "test: verify distributed assessment rollout"
```

---

## Final review checkpoint

Before declaring implementation complete, use `superpowers:verification-before-completion` and provide fresh evidence for:

- the full `unittest` suite;
- both Windows executable/installer builds;
- the 30-client reproduction report;
- the 100-client simultaneous-submission report;
- the coordinator-outage recovery report;
- the answer-key leakage scan;
- upgrade dry-run and historical row-count comparison;
- a clean diff limited to the approved distributed-assessment scope.
