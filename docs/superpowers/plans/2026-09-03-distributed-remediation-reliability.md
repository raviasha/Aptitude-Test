# Distributed Remediation Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three remaining Important findings with fail-closed one-entry authenticated-state recovery, an explicit validated client-state upgrade, and truthful externally controlled outage reporting.

**Architecture:** Keep SQLite mutations and HMAC journal entries atomic, then recover only a cryptographically validated one-entry anchor lag. Classify client databases before any DDL, block ordinary startup on nonempty legacy data, and expose an administrator-confirmed adoption path. External outage mode delegates stop/start to exact operator checkpoints and verifies pinned-HTTPS reachability while representing unavailable telemetry as null.

**Tech Stack:** Python 3, SQLite, HMAC-SHA256, FastAPI client runtime, `unittest`, Inno Setup Pascal script, JSON reports.

**Spec:** `docs/superpowers/specs/2026-09-03-distributed-remediation-reliability-design.md`

## Global Constraints

- Do not install software, change Windows services, ACLs, certificate stores, or firewalls, contact external machines, or use a production signing credential.
- Preserve cached packs, valid in-progress attempts, sealed-pending outbox work, and acknowledged attempts during an explicitly authorized legacy migration.
- Never automatically authenticate a nonempty legacy store that an ordinary lab user could previously modify.
- Recovery may advance an authenticated anchor by exactly one committed journal entry only; larger gaps or any cryptographic/state mismatch fail closed.
- External outage reports must be based on an observed pinned-HTTPS down/up transition; unsupported server metrics are unavailable, not zero.
- Failure reports must not contain credentials, ownership tokens, exception messages, filesystem paths, or supplied URLs.
- Keep the accepted 100-client outage p99 miss documented; do not optimize or rerun that gate.

---

### Task 1: Exact one-entry authenticated-state recovery

**Files:**
- Modify: `ksat/client/store.py:287-640`
- Test: `tests/test_client_store.py`

**Interfaces:**
- Consumes: existing `_journal_mac()`, `_state_digest()`, `_read_anchor()`, `_write_anchor()`, and `authenticated_state_journal` rows.
- Produces: frozen `_ValidatedJournalEntry(sequence, operation, previous_mac, state_digest, entry_mac)`, `_validated_journal_chain(connection) -> tuple[_ValidatedJournalEntry, ...]`, `_recover_or_verify_authenticated_state(*, allow_legacy_migration_recovery: bool) -> None`, and constructor option `allow_legacy_state_migration: bool = False`.

- [ ] **Step 1: Write failing crash-recovery and rejection tests**

  Add tests that patch the protected store's `_write_anchor` to raise after the SQLite commit, then reopen and assert the mutation is present and the anchor catches up. Add an initial-construction factory whose first anchor write fails and assert a reopen recovers the sole empty `initialize` entry. Add direct journal fixtures proving two entries ahead, forged HMAC, wrong state digest, and a missing anchor over nonempty state all raise the exact opaque authenticated-state error.

  ```python
  with patch.object(store, "_write_anchor", side_effect=OSError("injected")):
      with self.assertRaises(OSError):
          store.cache_pack(release_id, content_hash, pack_path, verified=True)
  recovered = ClientStore(database_path, integrity_key=key, integrity_anchor_path=anchor)
  self.assertEqual(pack_path.resolve(), recovered.verified_pack(release_id, content_hash))
  ```

- [ ] **Step 2: Run the new tests and capture RED**

  Run: `python -m unittest tests.test_client_store.ClientStoreTests.test_authenticated_state_recovers_exactly_one_committed_entry_after_anchor_failure tests.test_client_store.ClientStoreTests.test_authenticated_state_recovers_initial_anchor_creation_failure tests.test_client_store.ClientStoreTests.test_authenticated_state_recovery_rejects_invalid_forward_shapes -v`

  Expected: failures because current startup requires exact anchor/journal equality and rejects the initial journal-only state.

- [ ] **Step 3: Implement chain validation and bounded recovery**

  Validate every journal row from sequence 1 and the zero MAC, compare the current state digest to the tail, and compare the external anchor to either the tail or exactly the penultimate validated row. Atomically rewrite only the already-validated tail when the gap is exactly one. Permit a missing anchor automatically only for the sole `initialize` row over empty state; reserve the sole `legacy_v1_migration` row case for the confirmed migration path.

  ```python
  if anchor == tail:
      return
  if len(rows) >= 2 and anchor == rows[-2]:
      self._write_anchor(tail.sequence, tail.state_digest, tail.entry_mac)
      self._verify_current_authenticated_state(self.connection)
      return
  raise ValueError(_STATE_INTEGRITY_ERROR)
  ```

- [ ] **Step 4: Run recovery tests and the complete client-store suite**

  Run: `python -m unittest tests.test_client_store -v`

  Expected: all client-store tests pass, including existing tamper/rollback protections.

---

### Task 2: Explicit versioned v1-to-v2 client-state migration

**Files:**
- Modify: `ksat/client/store.py:287-1515`
- Modify: `client_app.py:35-45,1368-1400,2220-2340`
- Modify: `installer/KSATClient.iss:65-285`
- Modify: `docs/distributed-assessment-operations.md`
- Test: `tests/test_client_store.py`
- Test: `tests/test_windows_entrypoints.py`
- Test: `tests/test_windows_packaging.py`

**Interfaces:**
- Consumes: strict cached-pack fields, `_validated_attempt_snapshot()`, outbox lifecycle rules, `DeviceIdentityStore.load_or_create()`, `derive_state_integrity_key()`, and `ClientProcessLock`.
- Produces: `ClientStateMigrationRequired(ValueError)`, schema constants version 1 and 2, `ClientStore(..., allow_legacy_state_migration=False)`, `ClientStore.migration_summary() -> dict[str, int]`, `migrate_client_state(program_data: Path, *, confirmed: bool) -> dict[str, int]`, CLI flags `--migrate-state` and `--confirm-legacy-state`, and installer switch `/CONFIRMLEGACYSTATEMIGRATION=1`.

- [ ] **Step 1: Write failing store migration tests**

  Build version-0/version-1 legacy fixtures with no anchor and lifecycle rows for a cached pack, one in-progress attempt, one sealed-pending attempt/outbox, and one acknowledged attempt. Assert ordinary protected construction raises `ClientStateMigrationRequired` without creating the journal/anchor or changing `user_version`. Assert confirmed migration preserves canonical row snapshots and counts, sets version 2, and verifies on reopen. Corrupt one cached timestamp and one outbox shape in separate fixtures and assert confirmed migration fails before creating the journal/anchor. Inject the migration anchor write failure and assert ordinary startup fails while a second confirmed migration recovers.

  ```python
  before = snapshot_authenticated_tables(database_path)
  with self.assertRaises(ClientStateMigrationRequired):
      ClientStore(database_path, integrity_key=key, integrity_anchor_path=anchor)
  migrated = ClientStore(
      database_path,
      integrity_key=key,
      integrity_anchor_path=anchor,
      allow_legacy_state_migration=True,
  )
  self.assertEqual(before, snapshot_authenticated_tables(database_path))
  self.assertEqual(2, migrated.connection.execute("PRAGMA user_version").fetchone()[0])
  ```

- [ ] **Step 2: Run migration tests and capture RED**

  Run: `python -m unittest tests.test_client_store.ClientStoreTests.test_protected_store_blocks_nonempty_legacy_state_without_mutation tests.test_client_store.ClientStoreTests.test_confirmed_legacy_migration_preserves_all_lifecycle_states tests.test_client_store.ClientStoreTests.test_confirmed_legacy_migration_rejects_invalid_state_without_mutation tests.test_client_store.ClientStoreTests.test_confirmed_legacy_migration_recovers_anchor_failure -v`

  Expected: import/signature/schema-version failures because no explicit migration contract exists.

- [ ] **Step 3: Implement read-only classification, strict validation, and version stamping**

  Inspect `sqlite_master`, `PRAGMA user_version`, journal presence, and anchor presence before `_migrate()`. Treat version 0/1 nonempty state without authenticated entries as legacy; block unless the explicit constructor option is true. On confirmed migration, require supported tables/columns, `quick_check == ok`, an empty `foreign_key_check`, canonical cached-pack fields, a valid snapshot for every attempt, valid outbox retry/time/status/error fields, and exact bundle linkage. Only after validation may `_migrate()` append one `legacy_v1_migration` entry and stamp version 2 in the same immediate transaction. Plain newly created stores stamp version 1; verified authenticated version-0 stores stamp version 2.

  ```python
  self.connection.execute("BEGIN IMMEDIATE")
  anchor = self._append_authenticated_entry(
      self.connection, "legacy_v1_migration"
  )
  self.connection.execute("PRAGMA user_version = 2")
  self.connection.commit()
  self._write_anchor(*anchor)
  ```

- [ ] **Step 4: Run store migration and regression tests**

  Run: `python -m unittest tests.test_client_store tests.test_client_outbox tests.test_client_runtime -v`

  Expected: all tests pass with legacy data preserved and existing authenticated tamper checks intact.

- [ ] **Step 5: Write failing administrator CLI and installer contract tests**

  Assert `--migrate-state` uses the ProgramData client path and process lock, rejects a non-admin caller, refuses a nonempty legacy store without `--confirm-legacy-state`, returns only count fields after success, and treats the confirmation flag as invalid without the migration operation. Assert the installer stops the old service, invokes `--migrate-state` before service configuration/start, and appends `--confirm-legacy-state` only when `/CONFIRMLEGACYSTATEMIGRATION=1` is supplied.

  ```python
  result = client_main(
      ["--migrate-state", "--confirm-legacy-state"],
      environ={"ProgramData": directory},
      administrator_check=lambda: True,
  )
  self.assertEqual(0, result)
  ```

- [ ] **Step 6: Run entrypoint and packaging tests and capture RED**

  Run: `python -m unittest tests.test_windows_entrypoints tests.test_windows_packaging -v`

  Expected: CLI parser and installer-source assertions fail because migration wiring is absent.

- [ ] **Step 7: Implement the administrator entrypoint and installer preflight**

  Add `migrate_client_state()` around `ClientProcessLock`, derive the state HMAC from the protected identity, construct the store with explicit confirmation, close it, and return a count-only summary. Add mutually exclusive CLI operation/confirmation validation. In Inno Setup, build the migration parameters from `{param:CONFIRMLEGACYSTATEMIGRATION|0}`, run the executable after stopping the old service and before CA/service configuration, and abort on nonzero exit.

  ```pascal
  Parameters := '--migrate-state';
  if ExpandConstant('{param:CONFIRMLEGACYSTATEMIGRATION|0}') = '1' then
    Parameters := Parameters + ' --confirm-legacy-state';
  if (not Exec(ExpandConstant('{app}\{#AppExeName}'), Parameters,
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode)) or
      (ResultCode <> 0) then
    RaiseException('Client state requires administrator-reviewed migration.');
  ```

- [ ] **Step 8: Document the upgrade and recovery procedure**

  State the stop/backup/inspect/confirm/install sequence, the exact installer switch, the count-only success summary, the invalid/tampered preflight block, recovery from the untouched backup, and the fact that a failed migration anchor publication requires rerunning the confirmed migration rather than starting the service.

- [ ] **Step 9: Run combined migration gates**

  Run: `python -m unittest tests.test_client_store tests.test_windows_entrypoints tests.test_windows_packaging -v`

  Expected: all migration, entrypoint, packaging, and existing service-authority tests pass.

---

### Task 3: Observed external outage controls and truthful failure reports

**Files:**
- Modify: `scripts/load_distributed_assessment.py:970-1550,1650-1730`
- Modify: `docs/distributed-assessment-operations.md`
- Test: `tests/test_distributed_end_to_end.py`

**Interfaces:**
- Consumes: pinned-CA `_HttpClient`, `/api/build`, existing `run_isolated_gate()` orchestration, and `LoadGateFailure.report`.
- Produces: `_ExternalFixture(..., stopped_checkpoint: Callable[[], None], started_checkpoint: Callable[[], None])`, nullable external telemetry accessors, report field `metrics_availability`, `run_external_gate(..., stopped_checkpoint, started_checkpoint)`, CLI option `--outage-control operator-checkpoint`, and `_redacted_failure_report(arguments) -> dict[str, Any]`.

- [ ] **Step 1: Write failing external outage and reporting tests**

  Use the disposable local HTTPS coordinator as the authorized external target. Supply callbacks that really stop and restart it, assert the down probe and bounded up poll complete, and assert `coordinator_service_restarted` is true. Supply no-op STOPPED/STARTED callbacks and assert the gate rejects still-up/still-down states. Assert external CPU/RSS/SQLite/writer metrics are null and `metrics_availability == "unavailable"`. Patch setup, checkpoint, and cleanup paths to raise exceptions containing secret URL/password/path strings; assert `main()` writes a constant-code failure report containing none of them.

  ```python
  report = load.run_external_gate(
      root,
      base_url=fixture.base_url,
      ca_file=fixture.ca_file,
      stopped_checkpoint=fixture.stop_coordinator,
      started_checkpoint=fixture.restart_coordinator,
      outage=True,
      **credentials,
  )
  self.assertEqual("unavailable", report["metrics_availability"])
  self.assertIsNone(report["coordinator_resources"]["peak_rss_bytes"])
  ```

- [ ] **Step 2: Run external-focused tests and capture RED**

  Run: `python -m unittest tests.test_distributed_end_to_end.DistributedEndToEndTests.test_external_outage_requires_observed_pinned_https_stop_and_start tests.test_distributed_end_to_end.DistributedEndToEndTests.test_external_mode_reports_unavailable_server_metrics_as_null tests.test_distributed_end_to_end.DistributedEndToEndTests.test_unexpected_external_failures_write_constant_code_redacted_report -v`

  Expected: callback-argument failures, fabricated zero metrics, or missing report output.

- [ ] **Step 3: Implement external reachability checkpoints and nullable metrics**

  Remove the synthetic availability toggle. After the STOPPED callback, perform a pinned-CA HTTPS probe and require a transport failure. After the STARTED callback, poll `/api/build` until a bounded monotonic deadline and require success. External resource accessors return `None`; monitoring aggregates only numeric observations; isolated mode retains real numbers. Add `metrics_availability` to every successful/threshold report.

  ```python
  stopped_checkpoint()
  if self._pinned_https_is_reachable():
      raise RuntimeError("External outage checkpoint was not observed.")
  started_checkpoint()
  self._wait_for_pinned_https()
  ```

- [ ] **Step 4: Implement exact operator checkpoints and redacted fallback reports**

  Require `--outage-control operator-checkpoint` for external outage mode. Prompt for exact `STOPPED` and `STARTED` responses and reject any other text without executing a command. Catch post-parse runtime/setup/checkpoint/cleanup exceptions, serialize only safe dimensions and flags, set `completed` false and `failure.code` to `gate_execution_failed`, then write the requested report and return 1. Continue using a `LoadGateFailure`'s already-redacted threshold report.

  ```python
  def _redacted_failure_report(arguments):
      return {
          "schema_version": 1,
          "mode": "authorized_external_https" if arguments.external else "production_https_subprocess",
          "clients": arguments.clients,
          "questions_per_client": arguments.questions,
          "outage": bool(arguments.outage),
          "completed": False,
          "failure": {"code": "gate_execution_failed"},
          "failed_enforced_thresholds": ["gate_execution_completed"],
      }
  ```

- [ ] **Step 5: Document the server/client operator procedure**

  Document the separate server-console stop, exact STOPPED confirmation, observed pinned-HTTPS outage, client offline seal, separate server-console start, exact STARTED confirmation, observed pinned-HTTPS recovery, and null external telemetry semantics. State that the harness never executes a remote or local stop/start command.

- [ ] **Step 6: Run distributed end-to-end tests**

  Run: `python -m unittest tests.test_distributed_end_to_end -v`

  Expected: all isolated and external gate tests pass without contacting an external machine.

---

### Task 4: Final focused verification and evidence

**Files:**
- Modify: `.superpowers/sdd/distributed-lab-assessment/progress-ledger.md`
- Modify: `.superpowers/sdd/distributed-lab-assessment/final-fix-report.md`

**Interfaces:**
- Consumes: focused test output and the accepted 100-client gate metrics already recorded in the fix report.
- Produces: a dated follow-up section mapping each Important finding to implementation, tests, remaining deployment gates, and commits.

- [ ] **Step 1: Run focused security and regression suites**

  Run: `python -m unittest tests.test_client_store tests.test_client_outbox tests.test_client_runtime tests.test_client_identity tests.test_windows_entrypoints tests.test_windows_packaging tests.test_distributed_end_to_end -v`

  Expected: all tests pass.

- [ ] **Step 2: Run syntax and diff checks**

  Run: `python -m py_compile ksat/client/store.py client_app.py scripts/load_distributed_assessment.py`

  Run: `git diff --check`

  Expected: both commands exit 0; Git may print only the repository's existing LF-to-CRLF conversion notices.

- [ ] **Step 3: Update internal evidence without rerunning accepted load gates**

  Append the three design decisions, exact focused test count/duration, syntax/diff results, and unchanged gate disposition: normal 100-client PASS at p99 55.748 ms; outage correctness PASS with sole accepted local-answer p99 threshold miss at 109.184 ms. Preserve the production signing credential gate and physical-install/external-machine gates.

- [ ] **Step 4: Commit source, tests, and documentation**

  Run: `git add ksat/client/store.py client_app.py scripts/load_distributed_assessment.py installer/KSATClient.iss docs/distributed-assessment-operations.md tests/test_client_store.py tests/test_windows_entrypoints.py tests/test_windows_packaging.py tests/test_distributed_end_to_end.py docs/superpowers/specs/2026-09-03-distributed-remediation-reliability-design.md docs/superpowers/plans/2026-09-03-distributed-remediation-reliability.md`

  Run: `git commit -m "fix: harden client recovery and external outage gates"`

- [ ] **Step 5: Verify final status and commit**

  Run: `git status --short --branch`

  Run: `git show --stat --oneline --decorate HEAD`

  Expected: tracked tree clean on `codex/distributed-lab-assessment`; the ignored internal SDD report files contain the final evidence.
