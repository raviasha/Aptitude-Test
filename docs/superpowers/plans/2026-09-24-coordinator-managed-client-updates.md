# Coordinator-Managed Lab Client Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Faculty publish one signed Windows client bundle through the coordinator so every idle lab client updates before sign-in, while adding safe manual-submission confirmation and readable browser-integrity reporting.

**Architecture:** A dedicated update protocol validates an offline Ed25519-signed manifest plus an Authenticode-signed installer. The coordinator stores one staged/pilot/published release and serves it through existing pinned HTTPS and device-authenticated routes; the LocalSystem client downloads it into protected state and hands installation to a separate updater executable with last-known-good rollback. A one-time PowerShell bootstrap deploys the updater-enabled client remotely from a hostname list.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite, Ed25519 via `cryptography`, Authenticode/SignTool, PyInstaller, Inno Setup, vanilla JavaScript, Windows PowerShell/CIM, `unittest`, Node.js UI harnesses.

**Spec:** `docs/superpowers/specs/2026-09-24-coordinator-managed-client-updates-design.md`

## Global Constraints

- Windows Faculty Coordinator and Windows lab clients only; Ubuntu updates are out of scope.
- The coordinator receives one normal installer upgrade and does not self-update.
- Future client update bundles remain separate from coordinator installers.
- All published client updates are mandatory before a new student sign-in.
- An active attempt resumes on its installed version; a sealed submission uploads before updating.
- One explicitly selected pilot client must pass before **Publish to all clients** is enabled.
- The offline update-signing private key never enters the repository, coordinator, client, bundle, logs, or release payload.
- The client repeats manifest signature, archive, SHA-256, platform, version, and Authenticode checks after download.
- Right-click remains blocked but produces no new integrity event and is excluded from violation counts.
- Timer expiry submits automatically; manual submission requires a second explicit action.
- Administrator credentials for bootstrap remain only in a `PSCredential` in memory.
- Production installers require institution-controlled Authenticode and update-manifest signing; test signing is restricted to isolated acceptance.
- Version this feature release as `2.1.0` consistently in Python entrypoints, Windows resources, installer scripts, and release filenames.

## Review Focus

- A bundle may be correctly signed but target the wrong architecture or claim installer metadata that disagrees with Authenticode; coordinator and client must both reject it before state changes.
- Power or network loss can occur after any durable update stage; restart must either resume safely or leave the last known-good client usable without losing assessment state.
- An installed client may have an active attempt, a sealed pending submission, or both stale update files and a newer published release; assessment recovery wins and stale artifacts cannot be installed.
- Historical response bundles and `contextmenu` rows predate the new fields/catalogue; they must remain readable and must not become signature failures or counted violations.
- One hundred clients may reconnect together after being offline; randomized starts, bounded concurrency, range resume, and status writes must not starve assessment submissions.

---

## File structure

- `ksat/integrity.py`: canonical browser-integrity catalogue, aliases, explanations, evidence classes, counting and incident grouping.
- `ksat/update_protocol.py`: strict update-manifest models, semantic-version comparison, deterministic bundle parsing and Ed25519 verification.
- `ksat/coordinator/client_updates.py`: coordinator persistence and state transitions for upload, pilot, publish, withdraw, discovery and reports.
- `ksat/client/updates.py`: durable client update state, policy evaluation, resumable download and pre-install verification.
- `client_updater.py`: separate Windows updater entrypoint, silent installer execution, health checks and rollback.
- `scripts/build_client_update.py`: offline signed bundle builder.
- `scripts/bootstrap_windows_clients.ps1`: one-time hostname-list bootstrap with dry-run and machine-readable report.
- Existing `app.py`, `client_app.py`, coordinator/client protocol code, static UIs, installer scripts and Windows release tooling integrate these focused modules.

### Task 1: Canonical integrity-event catalogue and readable reporting

**Files:**
- Create: `ksat/integrity.py`
- Create: `tests/test_integrity_events.py`
- Modify: `static/client/app.js:1287-1401`
- Modify: `app.py:116-131,2028-2040,2699-2710,3738-3776`
- Modify: `tests/test_client_ui_contract.py`
- Modify: `tests/test_distributed_submission.py`
- Modify: `tests/test_registration.py`

**Interfaces:**
- Produces: `IntegrityDefinition(code, title, explanation, evidence_class, counts_as_violation)`, `canonical_integrity_code(code: str) -> str`, `integrity_definition(code: str) -> IntegrityDefinition`, `summarize_integrity_events(rows) -> list[dict]`.
- Produces: canonical `context_menu` as a historical informational alias only; new client code prevents the menu without calling `recordViolation`.

- [ ] **Step 1: Write catalogue and coverage tests that fail against the raw mapping**

```python
def test_context_menu_alias_is_informational():
    item = integrity_definition("contextmenu")
    self.assertEqual("context_menu", item.code)
    self.assertFalse(item.counts_as_violation)
    self.assertIn("right-click", item.explanation)

def test_every_client_emitted_code_has_readable_metadata():
    for code in CLIENT_EMITTED_CODES:
        item = integrity_definition(code)
        self.assertNotEqual(code, item.title)
        self.assertIn(item.evidence_class, {"blocked_action", "visibility_change", "monitoring_anomaly"})
```

Add a Node contract asserting `contextmenu` calls `preventDefault()` but does not add an integrity request. Add API and CSV tests asserting historical `contextmenu` rows do not increment `violation_count`, while `browser_monitor_gap` renders a cause-unverified explanation.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest tests.test_integrity_events tests.test_client_ui_contract tests.test_distributed_submission test_registration -q`

Expected: FAIL because `ksat.integrity` is absent, context menu is still recorded, and API/CSV output uses raw labels and raw counts.

- [ ] **Step 3: Implement the immutable catalogue and legacy aliases**

```python
@dataclass(frozen=True, slots=True)
class IntegrityDefinition:
    code: str
    title: str
    explanation: str
    evidence_class: Literal["blocked_action", "visibility_change", "monitoring_anomaly"]
    counts_as_violation: bool = True

ALIASES = {"contextmenu": "context_menu", "fullscreen_exit": "fullscreen_exited"}

def canonical_integrity_code(code: str) -> str:
    return ALIASES.get(code, code)
```

Include exact definitions for `fullscreen_exited`, `visibility_hidden`, `focus_lost`, `browser_page_hidden`, `browser_page_reloaded`, `browser_frozen`, `browser_monitor_gap`, `browser_monitor_restarted`, `browser_storage_unavailable`, `copy`, `cut`, `paste`, `dragstart`, `drop`, `print_attempt`, `shortcut_c`, `shortcut_x`, `shortcut_v`, `shortcut_p`, `shortcut_s`, and informational `context_menu`. Unknown codes return **Unrecognized integrity event** and retain the original code only in technical details.

- [ ] **Step 4: Route every faculty result and CSV through the catalogue**

Replace SQL raw `COUNT(*)` for integrity display with catalogue-aware summarization after fetching rows. Return `title`, `explanation`, `evidence_class`, `occurred_at`, `canonical_code`, and optional `original_code`. Count and flag only entries where `counts_as_violation` is true. Group same-code visibility events within two seconds for the primary incident list but retain every occurrence in `details`.

- [ ] **Step 5: Stop recording context-menu requests in the client**

```javascript
document.addEventListener('contextmenu', event => {
  if (ui.attempt && canEdit(ui.state)) event.preventDefault();
});
['copy', 'cut', 'paste', 'dragstart', 'drop'].forEach(name => {
  document.addEventListener(name, event => blockAndRecord(event, name));
});
```

- [ ] **Step 6: Run the focused tests and commit**

Run: `python -m unittest tests.test_integrity_events tests.test_client_ui_contract tests.test_distributed_submission test_registration -q`

Expected: PASS.

```powershell
git add ksat/integrity.py tests/test_integrity_events.py static/client/app.js app.py tests/test_client_ui_contract.py tests/test_distributed_submission.py test_registration.py
git commit -m "Explain and classify browser integrity events"
```

### Task 2: Manual submission confirmation and submission-cause audit

**Files:**
- Modify: `ksat/protocol.py:739-758`
- Modify: `ksat/client/runtime.py:402-470,1018-1043`
- Modify: `ksat/client/store.py:560-640,1710-1765`
- Modify: `client_app.py:120-140,2348-2387`
- Modify: `static/client/index.html`
- Modify: `static/client/app.js:220-275,800-950,1340-1370`
- Modify: `static/client/style.css`
- Modify: `ksat/coordinator/submissions.py:290-340,730-765`
- Modify: `ksat/coordinator/schema.py`
- Modify: `app.py`
- Modify: `tests/test_client_runtime.py`
- Modify: `tests/test_client_store.py`
- Modify: `tests/test_client_app_api.py`
- Modify: `tests/test_client_ui_contract.py`
- Modify: `tests/client_ui_flow_harness.js`
- Modify: `tests/test_distributed_submission.py`

**Interfaces:**
- Consumes: Task 1 catalogue so submission audit entries do not count as violations.
- Produces: `SubmissionCause = Literal["manual_confirmed", "timer_expired", "sealed_recovery"]` and `AssessmentRuntime.submit(*, cause: SubmissionCause)`.
- Produces: `ConfirmBody.confirmed: bool` plus `cause: SubmissionCause`; only `manual_confirmed` is accepted from the browser route.
- Produces: additive `attempts.submission_cause TEXT` coordinator audit field populated transactionally while accepting a submission.

- [ ] **Step 1: Write failing runtime/API tests for explicit cause**

```python
def test_manual_submit_records_signed_non_violation_cause(self):
    sealed = runtime.submit(cause="manual_confirmed")
    codes = [item.event_type for item in sealed.bundle.integrity_events]
    self.assertIn("submission_manual_confirmed", codes)

def test_timer_expiry_records_timer_cause(self):
    clock.advance(duration_seconds)
    snapshot = runtime.tick()
    self.assertEqual("sealed_pending", snapshot.state)
    self.assertIn("submission_timer_expired", event_codes(store, snapshot.attempt_id))
```

Add compatibility coverage that a pre-2.1 sealed bundle without a cause still verifies and is reported as `sealed_recovery`, not as a violation.

- [ ] **Step 2: Write failing UI-flow tests for the modal**

Test that the first submit activation opens the dialog, initial focus is **Continue assessment**, Escape cancels, Enter outside the confirm control cannot submit, double-click produces one POST, counts are accurate, the confirmed POST includes `{"confirmed":true,"cause":"manual_confirmed"}`, and timer expiry does not open the dialog.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python -m unittest tests.test_client_runtime tests.test_client_store tests.test_client_app_api tests.test_client_ui_contract tests.test_distributed_submission -q`

Expected: FAIL because submission cause and confirmation modal do not exist.

- [ ] **Step 4: Add backward-compatible cause recording**

Use informational signed event codes `submission_manual_confirmed` and `submission_timer_expired` before sealing. Add these definitions to the Task 1 catalogue with `counts_as_violation=False`. During coordinator ingestion, derive `attempts.submission_cause` from the signed events in the same transaction that accepts the bundle. If neither event is present in a valid pre-2.1 bundle, store `sealed_recovery`; never modify or re-sign that legacy bundle. The client also records `sealed_recovery` in its local attempt audit when it resumes an already sealed outbox item, but this local note is not sent as a new event.

```python
SubmissionCause = Literal["manual_confirmed", "timer_expired", "sealed_recovery"]

def submit(self, *, cause: SubmissionCause) -> AttemptSnapshot:
    self.store.record_integrity_event(
        record.attempt_id,
        f"submission_{cause}",
        occurred_at=self._trusted_now(record),
    )
    return self._seal(record)
```

Keep old signed response bundle shape intact; use the existing signed integrity-event collection for new causes so protocol version 1 and cached old bundles remain valid. Return `submission_cause` in the faculty result API and CSV audit export, and prove that replaying the same bundle cannot change it.

- [ ] **Step 5: Implement an accessible confirmation dialog**

Add a real `<dialog id="submit-confirmation">` containing `answered-count`, `unanswered-count`, **Continue assessment**, and **Submit assessment** controls. Put confirmation in a single `requestManualSubmission()` function guarded by `ui.submitting` and `ui.confirmingSubmission`. Use `showModal()`, focus the continue button, and close on Escape. Only the dialog confirm handler calls `submitAttempt('manual_confirmed')`.

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m unittest tests.test_client_runtime tests.test_client_store tests.test_client_app_api tests.test_client_ui_contract tests.test_distributed_submission -q`

Expected: PASS.

```powershell
git add ksat/protocol.py ksat/client/runtime.py ksat/client/store.py client_app.py static/client/index.html static/client/app.js static/client/style.css ksat/coordinator/submissions.py ksat/coordinator/schema.py app.py tests/test_client_runtime.py tests/test_client_store.py tests/test_client_app_api.py tests/test_client_ui_contract.py tests/client_ui_flow_harness.js tests/test_distributed_submission.py
git commit -m "Confirm manual assessment submission"
```

### Task 3: Strict signed client-update bundle protocol

**Files:**
- Create: `ksat/update_protocol.py`
- Create: `ksat/windows_authenticode.py`
- Create: `scripts/build_client_update.py`
- Create: `tests/test_update_protocol.py`
- Create: `tests/test_windows_authenticode.py`
- Create: `tests/test_build_client_update.py`
- Modify: `ksat/crypto.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `ClientUpdateManifest`, `VerifiedClientUpdate`, `parse_client_update(path, public_key_b64, authenticode_verifier) -> VerifiedClientUpdate`, `compare_versions(left, right) -> int`.
- Produces: `verify_authenticode(path, expected_publisher) -> AuthenticodeIdentity` backed by Windows `WinVerifyTrust`, including certificate validity and exact normalized publisher comparison.
- Produces: public-only `build/update-release-public.json`, embedded into coordinator, client and updater builds; the corresponding private key remains an external operator input.
- Produces: CLI `python -m scripts.build_client_update --installer PATH --version 2.1.0 --minimum-source-version 2.0.0 --publisher SUBJECT --private-key-file PATH --output PATH`.

- [ ] **Step 1: Write failing strict-format tests**

```python
def test_verified_bundle_binds_manifest_signature_and_installer(self):
    result = parse_client_update(bundle, public_key, fake_authenticode)
    self.assertEqual("2.1.0", result.manifest.client_version)
    self.assertEqual(hashlib.sha256(installer).hexdigest(), result.manifest.installer_sha256)

def test_rejects_duplicate_json_keys_extra_members_and_zip_bombs(self):
    for bad in (duplicate_keys, extra_member, oversized_member):
        with self.assertRaises(ValueError):
            parse_client_update(bad, public_key, fake_authenticode)
```

Cover signature mutation, digest/length mismatch, unsafe filename, wrong OS/architecture, malformed semantic versions, equal/downgrade targets, missing timestamp, publisher mismatch, expired/not-yet-valid signer certificate, untrusted Authenticode chain, and noncanonical archive order. Unit-test Authenticode decision mapping through an injected Windows API adapter; the packaged-artifact test in Task 11 validates a real signed executable.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_update_protocol tests.test_windows_authenticode tests.test_build_client_update -q`

Expected: FAIL because update protocol and builder are absent.

- [ ] **Step 3: Implement strict models and deterministic parsing**

```python
class ClientUpdateManifest(ProtocolModel):
    format_version: Literal[1]
    release_id: str
    client_version: str
    minimum_source_version: str
    target_os: Literal["windows"]
    target_architecture: Literal["x86_64"]
    installer_filename: str
    installer_size: int = Field(gt=0, le=250_000_000)
    installer_sha256: str
    authenticode_publisher: str
    published_at: datetime
    release_notes: str = Field(min_length=1, max_length=4000)
    health_check_timeout_seconds: int = Field(ge=15, le=300)
```

Limit the archive to `manifest.json`, the exact installer filename, and `manifest.sig`; limit total uncompressed bytes to 260 MB. Verify Ed25519 over the exact canonical manifest, then SHA-256/size and injected Authenticode verifier. Use numeric three-component semantic versions with no prerelease syntax for this release. Production entrypoints load the embedded public-key document and fail closed when it is absent, malformed, or contains private-key material.

- [ ] **Step 4: Implement the offline builder without retaining secrets**

Read the Ed25519 private key from an operator-supplied file, never copy it into build/output, write deterministic ZIP timestamps/order, immediately parse the output with the public key, and print only release ID, version, bundle SHA-256 and path.

- [ ] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_update_protocol tests.test_windows_authenticode tests.test_build_client_update -q`

Expected: PASS.

```powershell
git add ksat/update_protocol.py ksat/windows_authenticode.py scripts/build_client_update.py tests/test_update_protocol.py tests/test_windows_authenticode.py tests/test_build_client_update.py ksat/crypto.py .gitignore
git commit -m "Define signed client update bundles"
```

### Task 4: Coordinator update storage and pilot/publish state machine

**Files:**
- Create: `ksat/coordinator/client_updates.py`
- Create: `tests/test_client_update_store.py`
- Modify: `app.py:1040-1170`
- Modify: `ksat/coordinator/schema.py`

**Interfaces:**
- Consumes: Task 3 `VerifiedClientUpdate`.
- Produces: `ClientUpdateStore.upload`, `.select_pilot`, `.publish`, `.withdraw`, `.policy_for_device`, `.record_status`, `.dashboard`.
- Produces tables `client_update_releases` and `client_update_device_status`.

- [ ] **Step 1: Write failing migration and transition tests**

```python
def test_publish_requires_successful_exact_pilot(self):
    release = store.upload(verified)
    store.select_pilot(release.release_id, "device-a")
    with self.assertRaises(UpdateStateError):
        store.publish(release.release_id)
    store.record_status(release.release_id, "device-a", "healthy", "2.1.0", None)
    self.assertEqual("published", store.publish(release.release_id).state)
```

Cover duplicate release/version, immutable artifact hash, inactive pilot, wrong pilot report, withdrawal, idempotent repeated status, stale status attempt, offline calculation, and existing-database migration.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_client_update_store -q`

Expected: FAIL because the store and tables do not exist.

- [ ] **Step 3: Add additive schema and transactional state transitions**

```sql
CREATE TABLE client_update_releases (
  release_id TEXT PRIMARY KEY,
  client_version TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('uploaded','pilot','published','withdrawn')),
  manifest_json TEXT NOT NULL,
  bundle_filename TEXT NOT NULL,
  bundle_sha256 TEXT NOT NULL,
  pilot_device_id TEXT,
  created_at TEXT NOT NULL,
  published_at TEXT,
  withdrawn_at TEXT
);
CREATE TABLE client_update_device_status (
  release_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  installed_version TEXT NOT NULL,
  diagnostic_code TEXT,
  attempt_id TEXT NOT NULL,
  reported_at TEXT NOT NULL,
  PRIMARY KEY(release_id, device_id)
);
```

Store bundles under coordinator-owned `Client Updates/<release_id>/bundle.ksat-client-update` using quarantine plus atomic rename. Never trust a database filename without resolving it below that root.

- [ ] **Step 4: Run tests and commit**

Run: `python -m unittest tests.test_client_update_store tests.test_distributed_migration -q`

Expected: PASS.

```powershell
git add ksat/coordinator/client_updates.py tests/test_client_update_store.py app.py ksat/coordinator/schema.py
git commit -m "Store and stage client updates"
```

### Task 5: Coordinator admin and device update APIs

**Files:**
- Modify: `app.py`
- Modify: `ksat/coordinator/routes.py`
- Modify: `ksat/coordinator/__init__.py`
- Create: `tests/test_client_update_api.py`
- Modify: `tests/test_client_auth_api.py`

**Interfaces:**
- Consumes: Task 4 store and existing `_verified_device(request, connection)` authentication.
- Produces admin endpoints `/api/admin/client-updates`, `/upload`, `/{release_id}/pilot`, `/{release_id}/publish`, `/{release_id}/withdraw`.
- Produces device endpoints `/api/client/v1/update-policy`, `/api/client/v1/updates/{release_id}/bundle`, `/api/client/v1/updates/{release_id}/status`.

- [ ] **Step 1: Write failing authorization and range-download tests**

Test admin+CSRF requirements, rejected invalid bundle leaving no file/row, active enrolled device proof, inactive/wrong device rejection, pilot-only policy visibility, published policy visibility, withdrawn exclusion, `Range: bytes=N-`, `206`/`Content-Range`, invalid range `416`, and idempotent status attempt IDs.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_client_update_api tests.test_client_auth_api -q`

Expected: FAIL with missing routes.

- [ ] **Step 3: Implement streaming routes with fixed response contracts**

```python
@router.get("/api/client/v1/update-policy")
async def update_policy(request: Request):
    with config.connect() as connection:
        device_id = await _verified_device(request, connection)
        return store.policy_for_device(device_id, installed_version=request.headers["x-ksat-client-version"])
```

Return manifest/signature metadata from policy, stream bundle bytes from a coordinator-owned file descriptor, cap status body size, validate stage against a closed enum, and never return filesystem paths.

- [ ] **Step 4: Run tests and commit**

Run: `python -m unittest tests.test_client_update_api tests.test_client_auth_api -q`

Expected: PASS.

```powershell
git add app.py ksat/coordinator/routes.py ksat/coordinator/__init__.py tests/test_client_update_api.py tests/test_client_auth_api.py
git commit -m "Serve authenticated client updates"
```

### Task 6: Faculty Client Updates dashboard and pilot gate

**Files:**
- Modify: `static/app.js`
- Modify: `static/style.css`
- Modify: `templates/index.html`
- Modify: `tests/test_faculty_ui_contract.py`
- Create: `tests/client_update_admin_flow.js`

**Interfaces:**
- Consumes: Task 5 admin endpoints.
- Produces: Client Updates navigation page with upload, pilot select, publish, withdraw, status table and diagnostics.

- [ ] **Step 1: Write failing DOM-flow tests**

Use a Node harness to assert invalid upload errors remain visible, pilot selection lists only active devices, Publish is disabled until the selected device is `healthy` at the exact target version, withdrawal requires confirmation, and status labels are Updated/Downloading/Waiting/Offline/Failed.

- [ ] **Step 2: Run UI tests and verify RED**

Run: `python -m unittest tests.test_faculty_ui_contract -q`

Expected: FAIL because the page and flow do not exist.

- [ ] **Step 3: Implement the dashboard with safe copy**

Render release ID, version, signer/publisher, SHA-256, size, release notes and state. Use the existing `api()` CSRF helper. Do not use raw `innerHTML` for server-supplied release notes, hostnames or diagnostics; construct nodes or escape every field.

- [ ] **Step 4: Run UI tests and commit**

Run: `python -m unittest tests.test_faculty_ui_contract -q`

Expected: PASS.

```powershell
git add static/app.js static/style.css templates/index.html tests/test_faculty_ui_contract.py tests/client_update_admin_flow.js
git commit -m "Add client update administration"
```

### Task 7: Durable client policy, resumable download and assessment deferral

**Files:**
- Create: `ksat/client/updates.py`
- Create: `tests/test_client_updates.py`
- Modify: `ksat/client/coordinator.py`
- Modify: `ksat/client/store.py`
- Modify: `client_app.py`

**Interfaces:**
- Consumes: Task 3 update parser and Task 5 device APIs.
- Produces: `ClientUpdateManager.check()`, `.download()`, `.prepare_install()`, `.recover()`, `UpdateSnapshot` and durable `client_update_state.json`.
- Produces: `CoordinatorClient.update_policy`, `.download_update_range`, `.report_update_status`.

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_idle_outdated_client_prepares_verified_update(self):
    snapshot = manager.check()
    self.assertEqual("required", snapshot.stage)
    manager.download()
    self.assertEqual("ready_to_install", manager.snapshot().stage)

def test_active_attempt_and_pending_submission_defer_update(self):
    self.assertEqual("deferred_active_attempt", manager.check(active_attempt=True).stage)
    self.assertEqual("deferred_pending_submission", manager.check(pending_submission=True).stage)
```

Cover partial-file resume, wrong `Content-Range`, changed release during resume, disk-full cleanup, signature recheck, newer release superseding stale partial data, restart at each stage, withdrawn policy, 100 randomized delays bounded to the configured window, and an unavailable coordinator.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_client_updates tests.test_client_coordinator -q`

Expected: FAIL because manager methods and coordinator calls do not exist.

- [ ] **Step 3: Implement canonical durable state and range download**

```python
@dataclass(frozen=True, slots=True)
class UpdateSnapshot:
    release_id: str | None
    target_version: str | None
    stage: Literal["current", "required", "downloading", "verifying", "ready_to_install", "deferred_active_attempt", "deferred_pending_submission", "failed"]
    downloaded_bytes: int
    diagnostic_code: str | None
```

Write state through temp-file/fsync/replace below `%ProgramData%\KSAT Client\updates`. Use exclusive process locking. Verify the full bundle after the final byte and again immediately before creating the install request. Inject clock, random source, transport, disk-space probe and Authenticode verifier for deterministic tests.

- [ ] **Step 4: Integrate startup priority without disrupting attempts**

Client startup order becomes: authenticate local state; recover active attempt; start/flush sealed outbox; if neither exists check mandatory update; expose login only when current. Do not stop prefetch/outbox workers while an assessment safety exception is active.

- [ ] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_client_updates tests.test_client_coordinator tests.test_client_app_api tests.test_client_runtime -q`

Expected: PASS.

```powershell
git add ksat/client/updates.py tests/test_client_updates.py ksat/client/coordinator.py ksat/client/store.py client_app.py
git commit -m "Download and stage mandatory client updates"
```

### Task 8: Separate updater helper, health verification and rollback

**Files:**
- Create: `client_updater.py`
- Create: `ksat/client/updater.py`
- Create: `tests/test_client_updater.py`
- Modify: `client_app.py`
- Modify: `installer/KSATClient.iss`
- Modify: `scripts/windows_release.py`
- Modify: `tests/test_windows_packaging.py`
- Modify: `tests/test_windows_entrypoints.py`

**Interfaces:**
- Consumes: Task 7 canonical install request and staged verified bundle.
- Produces: `Updater.run(request_path: Path) -> UpdateResult`, `HealthVerifier.verify(expected_version, identity_digest, config_digest, state_digest)`, and `KSATClientUpdater.exe`.

- [ ] **Step 1: Write failing helper tests using fake service/installer runners**

Test success, installer nonzero exit, timeout, service stop timeout, mismatched loopback version, changed device identity, changed coordinator configuration, unreadable authenticated store, rollback success, rollback failure diagnostic, power-loss recovery at every stage, and refusal of request/staged paths outside protected update storage.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_client_updater tests.test_windows_packaging tests.test_windows_entrypoints -q`

Expected: FAIL because helper and build target are absent.

- [ ] **Step 3: Implement the updater as a narrow privileged program**

```python
class Updater:
    def run(self, request_path: Path) -> UpdateResult:
        request = self.load_and_verify_request(request_path)
        self.services.stop("KSATLabClientAuthority", timeout=30)
        self.installers.run(request.installer, ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
        self.services.start("KSATLabClientAuthority", timeout=30)
        return self.health.verify(request)
```

Run the helper from a copied path in protected update staging. Persist stages before each external action. On failure, run the last-known-good signed installer silently, recheck health, and retain both diagnostic stages. Never pass credentials, signing keys or arbitrary installer arguments. The bootstrap/current installer copies its own `{srcexe}` into `%ProgramData%\KSAT Client\updates\last-known-good\KSATClientSetup-<version>.exe` only after verifying its Authenticode identity; a successful future update promotes the newly verified installer to that slot only after health passes. Keep one predecessor until the promotion is durable.

- [ ] **Step 4: Add updater to installer and release inspection**

Build `KSATClientUpdater.exe` as its own PyInstaller target. Install it beside the client executable, allow only SYSTEM/Administrators to modify it, and have the main service copy it before launch. Embed the public-only update key in coordinator, client and updater targets. Extend recursive payload inspection to reject private data in all executables and installers and to fail when the public-key resource is missing.

- [ ] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_client_updater tests.test_windows_packaging tests.test_windows_entrypoints -q`

Expected: PASS.

```powershell
git add client_updater.py ksat/client/updater.py tests/test_client_updater.py client_app.py installer/KSATClient.iss scripts/windows_release.py tests/test_windows_packaging.py tests/test_windows_entrypoints.py
git commit -m "Install client updates with verified rollback"
```

### Task 9: Mandatory pre-login maintenance UI

**Files:**
- Modify: `client_app.py`
- Modify: `static/client/index.html`
- Modify: `static/client/app.js`
- Modify: `static/client/style.css`
- Modify: `tests/test_client_app_api.py`
- Modify: `tests/test_client_ui_contract.py`
- Modify: `tests/client_ui_flow_harness.js`

**Interfaces:**
- Consumes: Task 7 `UpdateSnapshot` and Task 8 install launch/recovery results.
- Produces: `/api/state.update` and `/api/update/retry`; maintenance states Checking, Downloading, Verifying, Installing and restarting, Update complete, and Failed.

- [ ] **Step 1: Add failing API/UI tests**

Assert outdated idle client never renders login, there is no Skip control, progress is announced accessibly, retry is available only for safe failed stages, active attempt still opens its recovery gate, pending submission remains visible, and a successful restarted build returns to login.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_client_app_api tests.test_client_ui_contract -q`

Expected: FAIL because update state is not exposed or rendered.

- [ ] **Step 3: Implement maintenance rendering and polling**

Use server-provided enum values and fixed local copy; do not render server error details as HTML. Poll boundedly while downloading/installing, stop polling on page lifecycle loss, and preserve the existing integrity-monitor behavior only for active attempts.

- [ ] **Step 4: Run tests and commit**

Run: `python -m unittest tests.test_client_app_api tests.test_client_ui_contract -q`

Expected: PASS.

```powershell
git add client_app.py static/client/index.html static/client/app.js static/client/style.css tests/test_client_app_api.py tests/test_client_ui_contract.py tests/client_ui_flow_harness.js
git commit -m "Show mandatory client update progress"
```

### Task 10: One-time remote bootstrap deployment tool

**Files:**
- Create: `scripts/bootstrap_windows_clients.ps1`
- Create: `scripts/test_bootstrap_windows_clients.ps1`
- Create: `docs/client-update-bootstrap.md`
- Modify: `docs/distributed-assessment-operations.md`

**Interfaces:**
- Consumes: updater-enabled signed `KSATClientSetup-2.1.0.exe`, matching SHA-256, UTF-8 one-hostname-per-line input and interactive `Get-Credential`.
- Produces: `bootstrap-results.json` and `bootstrap-results.csv`; supports `-DryRun`, `-RetryResults`, `-ThrottleLimit`.

- [ ] **Step 1: Write Pester-free PowerShell contract tests with injected transport functions**

The test script dot-sources functions without running `Main`, replaces DNS/CIM/SMB/task/health functions, and asserts hostname normalization, duplicate rejection, credential redaction, dry-run nonmutation, bounded concurrency, Offline/Failed retry selection, cleanup after task failure, and no password in captured arguments/output.

- [ ] **Step 2: Run tests and verify RED**

Run: `pwsh -NoProfile -File scripts/test_bootstrap_windows_clients.ps1`

Expected: FAIL because the bootstrap script does not exist.

- [ ] **Step 3: Implement explicit preflight and deployment stages**

```powershell
param(
  [Parameter(Mandatory)] [string]$HostsFile,
  [Parameter(Mandatory)] [string]$Installer,
  [switch]$DryRun,
  [string]$RetryResults,
  [ValidateRange(1,16)] [int]$ThrottleLimit = 4
)
$Credential = Get-Credential -Message 'Administrator account for KSAT lab clients'
```

Use `New-CimSession -Credential` for preferred transport. Use an authenticated temporary SMB mapping plus Task Scheduler CIM/RPC fallback without embedding the password in process arguments. Preflight DNS, architecture, free space, existing service/version, remote transport, local installer hash and Authenticode. Copy to a protected temporary directory, run one SYSTEM task, poll a result marker, verify `/api/build` remotely through the task, remove task/files, and write redacted atomic reports.

- [ ] **Step 4: Run script tests and document exact operator commands**

Run: `pwsh -NoProfile -File scripts/test_bootstrap_windows_clients.ps1`

Expected: PASS.

Document:

```powershell
pwsh -NoProfile -File scripts/bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\KSATClientSetup-2.1.0.exe -DryRun
pwsh -NoProfile -File scripts/bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\KSATClientSetup-2.1.0.exe -ThrottleLimit 4
pwsh -NoProfile -File scripts/bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\KSATClientSetup-2.1.0.exe -RetryResults .\bootstrap-results.json
```

- [ ] **Step 5: Commit**

```powershell
git add scripts/bootstrap_windows_clients.ps1 scripts/test_bootstrap_windows_clients.ps1 docs/client-update-bootstrap.md docs/distributed-assessment-operations.md
git commit -m "Deploy updater bootstrap to lab clients"
```

### Task 11: Versioned Windows release, end-to-end verification and operational handoff

**Files:**
- Modify: `app.py:96`
- Modify: `coordinator_main.py:27`
- Modify: `client_app.py` version constant/build endpoint
- Modify: `ksat/coordinator/tls.py:35`
- Modify: `scripts/windows_release.py:33,450-480`
- Create: `build/update-release-public.json` during the controlled release build (generated file; never commit a private key)
- Modify: `installer/KSATCoordinator.iss:1-15`
- Modify: `installer/KSATClient.iss:1-15`
- Modify: `tests/test_windows_entrypoints.py`
- Modify: `tests/test_windows_packaging.py`
- Modify: `docs/distributed-assessment-operations.md`
- Create: `docs/client-update-release-checklist.md`
- Create: `tests/test_client_update_end_to_end.py`

**Interfaces:**
- Consumes: Tasks 1-10.
- Produces: test-signed acceptance artifacts `KSATCoordinatorSetup-2.1.0.exe`, `KSATClientSetup-2.1.0.exe`, and one test-only `.ksat-client-update` bundle; production output still requires institution signing inputs.

- [ ] **Step 1: Write failing version-consistency and end-to-end tests**

Assert every version source equals `2.1.0`. Build an isolated coordinator plus two logical clients: bootstrap client A, upload bundle, pilot A, reject publish before health, publish after health, keep B offline, reconnect B, verify mandatory update, preserve both identities/config/state, defer an active attempt and pending submission, then complete them and update. Exercise 100 concurrent policy/status calls while submission traffic remains successful.

- [ ] **Step 2: Run focused end-to-end tests and verify RED**

Run: `python -m unittest tests.test_client_update_end_to_end tests.test_windows_entrypoints tests.test_windows_packaging -q`

Expected: FAIL until all versions are aligned and the integrated flow is complete.

- [ ] **Step 3: Align version resources and installer upgrade behavior**

Set `APP_VERSION = "2.1.0"`, Inno `AppVersion`/filenames to `2.1.0`, and version resource tuples to `(2,1,0,0)`. Preserve existing AppIds, configuration, trust, identity, authenticated state, packs and results. Include coordinator `Client Updates` storage and client `updates` storage with SYSTEM/Administrators-only permissions. Require the release command to receive the institution's public update key, generate `build/update-release-public.json`, embed that file in all three executables, and abort if a private-key marker is detected anywhere in the payload.

- [ ] **Step 4: Run all automated suites**

Run:

```powershell
python -m unittest discover -s tests -q
python -m unittest test_registration test_question_media test_feedback_ui test_quantitative_bank_visuals test_completed_chapter_packages -q
python -m unittest discover -s data-engineering/textbook_chapters_v2/tests -q
node tests/test_recurring_math.js
pwsh -NoProfile -File scripts/test_bootstrap_windows_clients.ps1
git diff --check
```

Expected: all tests PASS and diff check reports no errors.

- [ ] **Step 5: Build and inspect test-signed Windows artifacts**

Follow `WINDOWS_EXE_BUILD.md` in verification-only test-signing mode. Require successful PyInstaller builds for coordinator, client and updater; Inno installer builds; Authenticode verification; recursive payload secret scans; coordinator/client/updater smoke; and bundle parse verification. Label every artifact non-distributable.

- [ ] **Step 6: Perform disposable Windows pilot acceptance**

On one disposable coordinator VM and at least two disposable client VMs:

1. Install the prior `2.0.0` coordinator/client and create an enrolled identity plus saved state.
2. Upgrade coordinator to `2.1.0` and remotely bootstrap only client A.
3. Upload a newer test client bundle, select A as pilot and verify publication remains disabled until its health report.
4. Publish, bring offline client B online, and verify its automatic pre-login update.
5. Interrupt one download and one installation, verify resume/rollback, then retry.
6. Start an attempt and create a sealed pending submission; verify both defer update correctly.
7. Verify manual submission confirmation, timer-expiry submission, readable integrity explanations, and right-click exclusion.
8. Compare pre/post identity, coordinator trust, database authentication, cached pack and result records.

- [ ] **Step 7: Record evidence and commit**

Write exact hashes, signer identities, test counts, VM versions, pilot results and limitations to `docs/client-update-release-checklist.md`. Do not claim production readiness without institution-signed artifacts and physical-lab acceptance.

```powershell
git add app.py coordinator_main.py client_app.py ksat/coordinator/tls.py scripts/windows_release.py installer/KSATCoordinator.iss installer/KSATClient.iss tests/test_windows_entrypoints.py tests/test_windows_packaging.py tests/test_client_update_end_to_end.py docs/distributed-assessment-operations.md docs/client-update-release-checklist.md
git commit -m "Release coordinator-managed client updates"
```
