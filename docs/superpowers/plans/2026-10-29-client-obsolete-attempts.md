# Automatic obsolete-attempt recovery

## Authorized behaviour

The lab owner explicitly instructed that submissions rejected with
`attempt_identity_mismatch` are obsolete and should no longer require a visit
to each PC. Automatically run the existing verified archive/removal operation
for that exact rejection. Do not weaken the server's identity validation or
discard other failures. Keep archives, current tests, other submissions,
enrollment, configuration and server results intact.

## Implementation plan

- [x] Add failing authenticated-store tests for automatic recovery, exact-code
  selection, active-test deferral, archive failure, maintenance exclusion and
  update-health deferral.
- [x] Reuse the manual recovery archive and transaction logic for an in-process
  operation under maintenance, runtime and store locks. Clear only the removed
  runtime attempt. Run before startup recovery and during state refresh.
- [x] Test existing rejection at startup and newly rejected submission during
  use; ensure managed update preservation verification finishes before recovery.
- [x] Add the previously requested running-version labels to Client and
  Coordinator without blocking normal UI startup.
- [x] Restore `source_key` in the real question API and verify the two
  photographed Missing Number questions in the shipping UI.
- [ ] **ON HOLD per user:** Build Client/Builder 2.1.8 and Coordinator 2.2.8, including the previous
  Coordinator runtime version in its migration allowlist. Produce a signed
  managed bundle for clients on 2.1.7 or later.
- [ ] **ON HOLD per user:** Verify packaged payloads and publish installers
  and instructions to the existing GitHub repository.

## Decisions and evidence

- The user subsequently instructed: **do not build installers yet**, and added
  the duplicate-image/text defect with photographs of Missing Number questions
  72 and 14. Packaging and publication steps are on hold. Version 2.1.8/2.2.8
  labels in source refer to unreleased work; existing released artifacts remain
  unchanged. No build has been started.
- Root cause of duplicate text: `client_app._question_payload` discarded
  `source_key`, which the shipped browser replacement rule requires. Restore
  the field and test actual chapter data through the real API and browser code.
- Reviewer findings: defer startup recovery without a permanent corruption
  flag when several obsolete attempts await update health; reconcile runtime
  pointers even if the final archive outcome marker fails after commit; retry
  state lookup when archival occurs between state and attempt requests. All
  reproduced in failing tests and fixed. Keep the version labels in this work.
- Final review found that retained submissions needed full runtime recovery
  after archival. Added deferred and live mixed-queue API regressions, plus a
  deferred browser-close regression. Fixed them and received a no-blocker
  follow-up review. No blanket removal of other failures was introduced.
- Final source verification: **409 passed, 1 skipped, 108 subtests passed**
  across recovery, client UI/API/runtime/outbox, preservation, update lifecycle,
  browser lifecycle, updater and end-to-end updates, entrypoints, packaging,
  Coordinator migration and Builder tests. Thread exceptions were treated as
  errors. Four existing FastAPI lifespan deprecation warnings remain. Log:
  `tmp/client-218-verified-source.log` (local, not a release artifact).
- Visually inspected the original PNGs for `ch404-q0014` and `ch404-q0072`:
  both contain their complete directions and question diagrams/table, matching
  the supplied photographs. No bank changes or reimport are necessary.

- Reuse the existing worktree and archival format. The user has specified both
  the rejection and disposition; no further policy approval is needed.
- Automatic recovery runs only after the updater has completed preservation
  health checks. Otherwise removal would correctly look like lost data to the
  existing updater. No weakening of those checks.
- Clients 2.1.5/2.1.6 still need their one-time interactive upgrade. Clients
  2.1.7 can receive the new managed bundle; physical rollout remains a pilot
  verification step, not a claim based on build tests.
