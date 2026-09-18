# Distributed Assessment: Remaining Work Before Production

**Branch:** `codex/distributed-lab-assessment`

**Current commit:** `304f107`

**Audience:** Faculty, IT, security, and release owner

## Bottom line

The agreed product implementation is complete: one faculty coordinator, separately installed Windows clients, local answering and timers, the same questions in a per-student order, resume only on the same computer, and sealed result submission to the coordinator.

What remains is release and deployment readiness, not a redesign of the assessment workflow.

## Already complete

- Protected Windows client boundary with the `KSATLabClientAuthority` LocalSystem service.
- Local answer persistence, timer handling, sealing, durable submission outbox, retry, and outage recovery.
- Faculty assessment launch, device enrollment/revocation, audit controls, extensions, void/retake handling, and compatibility migration.
- Separate coordinator and client executables/installers.
- Client-side saved coordinator URL, initially configured by an administrator and changeable later through the administrator workflow.
- Canonical Node-enabled test suite recorded as **462/462 passing**.
- Latest client-store reliability correction recorded as **59/59 focused tests passing**.
- 30-client, 100-client, and 100-client outage/restart load gates passed correctness and submission thresholds.

The outage test has one explicitly accepted local-answer p99 miss: **109.184 ms** against the unchanged 100 ms target.

The 462-test and load-gate runs predate commit `304f107`; only the focused client-store tests were rerun after that latest correction.

## Required before production rollout

### 1. Production signing — blocking

Obtain the institution-controlled:

- Authenticode PFX
- Protected PFX password
- Exact publisher subject to pin
- Approved HTTPS timestamp URL

Run the fail-closed production build and regenerate `release/SHA256SUMS.txt`. The current test-signed artifacts must not be distributed as production installers.

### 2. Final independent review — blocking

Run the final scoped review against commit `304f107`, including:

- Atomic legacy client-state migration
- Transient authenticated-anchor recovery
- Adversarial concurrent-writer protection

Record that the final commit has no Critical or Important findings.

### 3. Physical Windows acceptance — blocking

On disposable, authorized Windows machines:

1. Install the coordinator and client packages.
2. Verify the LocalSystem service, protected ACLs, machine-root CA, private firewall rule, and loopback-only client UI.
3. Enroll a client and complete one supervised assessment.
4. Stop/restart the coordinator and verify sealed submissions drain without data loss.

Automated isolated tests cannot prove these operating-system effects.

### 4. Legacy data migration — conditional

If an existing pre-2.0 Aptitude Lab database must be retained:

1. Take a complete protected backup.
2. Run `scripts/upgrade_distributed_assessments.py` with `--dry-run`.
3. Review integrity and row counts.
4. Perform the confirmed additive migration.
5. Repeat integrity and row-count checks.

## Deployment and operations sequence

1. Choose a stable DNS hostname and approved private HTTPS port.
2. Install and initialize the signed coordinator package.
3. Back up the complete coordinator `ProgramData` tree, including keys, database, releases, certificates, and configuration.
4. Install the signed client package on each lab computer and save the coordinator URL.
5. Distribute only the public coordinator CA and metadata files to clients.
6. Rotate the one-time enrollment code, enroll the intended lab computers, then rotate it again.
7. Import and validate the question bank.
8. Create the assessment and prefetch the encrypted pack.
9. Run a supervised pilot with a small group.
10. Rehearse backup restore and coordinator outage recovery.
11. Roll out to the remaining lab computers.

## Optional follow-up (not required for a controlled pilot)

- Optimize the outage-mode local-answer p99 below 100 ms. The 109.184 ms result was explicitly accepted.
- Repeat redundant load gates or a full build when a code change affects the measured path.
- Restructure the multi-commit integration history. This was explicitly deferred.
- Expand physical acceptance from the pilot group to the complete lab fleet.

## Do not do

- Do not distribute test-signed binaries.
- Do not copy coordinator private secrets, enrollment codes, session tokens, or assessment packs outside protected storage.
- Do not bypass TLS verification.
- Do not edit client SQLite files manually.
- Do not move an in-progress attempt to another computer.

## Reference files

- Operations runbook: [`docs/distributed-assessment-operations.md`](docs/distributed-assessment-operations.md)
- Windows build/signing guide: [`WINDOWS_EXE_BUILD.md`](WINDOWS_EXE_BUILD.md)
- Product overview: [`README.md`](README.md)
- Load reports: `load-report-30.json`, `load-report-100.json`, and `load-report-100-outage.json` in the distributed worktree.
