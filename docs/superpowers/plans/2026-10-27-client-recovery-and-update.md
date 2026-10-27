# Client recovery and managed updates implementation plan

> **For agentic workers:** Use superpowers:executing-plans for the main update work and superpowers:dispatching-parallel-agents for the independent recovery and Coordinator tasks. Each worker follows test-driven development; the integrated change receives a fresh review.

**Goal:** Release Client/Builder 2.1.7 and Coordinator 2.2.6 with archived practice-attempt recovery, pending-submission-preserving upgrades, and reliable reporting.
**Architecture:** Existing authenticated store and maintenance/lifecycle locks protect recovery. The signed updater owns a quiescent preservation handoff and durable version/release/attempt-bound outcome. Client background work relays verified outcomes and retries policy when eligible. Existing Coordinator API maintains verified last-contact times and pilot identity display.
**Tech stack:** Python, SQLite, FastAPI, JavaScript, Tk, PyInstaller, Inno Setup, existing KSAT signing identity.
**Spec:** ../specs/2026-10-27-client-recovery-and-update-design.md (approved by user on 2026-10-27).

## Global constraints

- No active test interruption, invented receipt, deleted unapproved submission, changed enrollment, or bypassed signature/integrity checks.
- Recovery handles exact attempt_identity_mismatch practice candidates after administrator confirmation; other errors get diagnosis.
- Existing blocked 2.1.5 clients require interactive bootstrap; legacy managed handoff must be demonstrated or explicitly unsupported for this release.
- Preserve archives under protected ProgramData and keep private material out of logs/Git.
- Reuse current linked worktree; do not touch the main user checkout or installed lab applications.

## Review focus

- Service/outbox concurrency while snapshotting, installing, restoring availability, and reporting health.
- Archive failures, anchor interruption, changed selections, unsupported errors, and active tests.
- Legacy helper still running after new files are installed; never infer managed success from version alone.
- Failed network policy requests and completed outcomes across process restarts.
- Exact version/attempt binding and truthful publication gating.

## Task 1: Recovery utility (parallel)

Files: new ksat/client/recovery.py, client_recovery.py, tests/test_client_recovery.py. Worker may add dedicated tests, but must not edit parent-owned packaging/client_app/store files.
- [x] Add failing real authenticated-store tests for selected stale practice archive, retention, blocked active/tampered state, backup failure, and idempotence.
- [x] Implement protected archive then authenticated selected-attempt removal, session exclusion, bounded diagnostics, and administrator Tk UI.
- [x] Run meaningful tests and return changes/interface details for packaging and client UI integration.

## Task 2: Coordinator reporting (parallel)

Files: ksat/coordinator/routes.py, ksat/coordinator/client_updates.py, static/app.js, dedicated Coordinator tests. app.py only if necessary and coordinate with parent.
- [x] Add failing tests for authenticated last contact, unauthenticated non-contact, selected pilot label, and Unknown/Waiting vs false Offline.
- [x] Implement changes without weakening existing exact-version/stale-attempt validation.
- [x] Run API/store/UI tests and return evidence.

## Task 3: Client updater and integration (main)

Files: ksat/client/updates.py, ksat/client/updater.py, client_updater.py, client_install_guard.py, client_app.py, static/client/app.js, update-focused tests; new preservation helper if needed.
- [x] Add failing tests: pending queue can update, active remains blocked, policy failure visible, single attempt identity, verified terminal reporting/retry, real preservation, no overlapping helpers.
- [x] Implement consistent policy and handoff; integrate safe Faculty diagnostics and installed recovery shortcut instructions.
- [x] Assess actual legacy updater code. Implement tested handoff or use spec fallback requiring interactive bootstrap; never manufacture healthy status.
- [x] Run relevant runtime, store, installer, updater, API and UI regressions.

## Task 4: Package, review, publish (main)

Files: versions, installer/KSATClient.iss, scripts/windows_release.py, release build tools, release guides/evidence.
- [x] Integrate recovery EXE and Start menu shortcut in signed Client and Builder payloads; bump all applicable versions consistently.
- [x] Fresh independent whole-change review; resolve important findings with tests.
- [x] Build/sign/inspect installers and frozen startup; record checksums and source revision. Keep physical pilot explicitly unverified.
- [x] Publish source and versioned release assets to GitHub, verifying remote hashes. Do not mutate old published artifacts or deployed rollout state.

## Execution record

- 2026-10-27: approved; parent on task 3, parallel tasks 1/2 dispatched after plan creation. Baseline HEAD 442f2cd. No product code modified at plan creation.

- Recovery worker: 118 passed, 1 skipped; exact practice selection, archive failure, transaction/anchor interruption, active races, and GUI confirmation covered.
- Coordinator worker: 38 focused tests plus 15 subtests passed; authenticated contact and pilot identity implemented.
- Actual 2.1.5 helper tested: its unbound legacy success record cannot certify preservation. Approved fallback chosen: interactive bootstrap 2.1.7, no managed bundle; future source floor 2.1.7.
- Fresh review found coherent old-state rollback could pass independent authentication. Fix in progress adds baseline ancestry and attempt continuity, with legitimate upload acknowledgements allowed.
- Parent manager/packaging focused run: 43 passed before final rollback-failure and installer additions. Integrated client/coordinator/builder regression run in progress.

- Integrated regression: 755 passed, 27 environment-dependent skips, 208 subtests passed (494.55 seconds). Manager/UI/packaging final run: 85 passed, 7 subtests passed. Final continuity re-review: 14 focused tests passed; exact coherent-rollback reproducer now rejected, no remaining release blockers.
- Coordinator2.2.6 signed build complete: frozen HTTPS startup, client compatibility handshake, UAC/smoke payload equivalence and exact Inno payload verified. Initial pre-freeze build was discarded after detecting a changed client JS resource; final source-frozen build passed.
- Client/Builder2.1.7 build underway using source-input digest record; no 2.1.7 managed bundle. Existing-install parent ACL normalization and healthy interactive rollback seed included.

- Final focused rerun: 73 passed, 4 subtests passed. Source frozen at 1f9a0b0; all 126 input digests unchanged through builds.
- Signed Client2.1.7 and Builder2.1.7 built; real frozen Client TLS startup/loopback-only listener, exact Inno helper payloads, public-only Builder payload and pinned signer verified. No physical PC pilot performed. Publishing next.

- Published source and Coordinator artifacts to main at 2e45e2f; published Client/Builder under https://github.com/raviasha/Aptitude-Test/releases/tag/client-v2.1.7 as a private-lab prerelease. GitHub asset SHA-256/size verification passed for installer, Builder, checksums and evidence. Public Coordinator/Client/Builder download links returned HTTP 200. All planned work complete; physical affected-PC pilot remains explicitly outstanding and is not represented as automated verification.
