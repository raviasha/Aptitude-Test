# Coordinator Firewall Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upgrade an installed Coordinator 2.0.0 firewall ownership record safely, rebuild the signed 2.1.0 installer, and publish it to GitHub main.

**Architecture:** Accept only the exact supported legacy and current ownership records. Verify the corresponding live firewall rule before changing it; migrate the legacy rule name to the current name with rollback if saving the new record fails. Keep the existing executable and application data unchanged.

**Tech Stack:** Inno Setup Pascal Script, Windows Firewall/PowerShell, Python pytest, Authenticode, Git.

**Spec:** User-approved correction in this conversation: migrate the supplied `KSAT Faculty Coordinator 2.0.0` ownership record, rebuild Coordinator setup, and push to GitHub.

## Global Constraints

- Preserve all student records, runtime settings, CA/private keys, and public connection files.
- Only recognise `KSAT Faculty Coordinator 2.0.0` and `KSAT Faculty Coordinator 2.1.0`; never adopt an arbitrary record or broaden firewall access.
- Keep the existing exact program path, TCP port validation, inbound/allow/Private/enabled checks.
- Do not modify the host machine's real firewall during automated tests.
- Preserve unrelated dirty files; no client or builder rebuild; no force push.

## Review Focus

- Legacy/current rule-name collisions must fail without changing either rule.
- Tampered metadata, unsupported names, wrong executable paths, or mismatched live rules must fail before mutation.
- Failed metadata persistence must restore the prior rule name and port.
- Current-version reinstall must remain idempotent; a clean install must still work.
- Uninstall must remove only the verified owned rule, including an unmigrated legacy record left by a partial upgrade.

## Task 1: Verified legacy firewall migration

**Files:** Modify `installer/KSATCoordinator.iss`; create `tests/test_coordinator_firewall_upgrade.py`; update `docs/coordinator-runtime-upgrade-fix.md`.

**Interfaces:** Extend `OwnerPort` to return the validated stored rule name as well as the port. Parameterise rule verification, absence checking, mutation, and rollback by that validated name. Keep `ConfigureOwnedFirewall(PortValue: String)` and `DeleteOwnedFirewall(ForUpgrade: Boolean)` as installer entry points.

- [x] Write a compiled Inno test harness executing the actual installer functions, with only external firewall/file-operation boundaries controlled in disposable test storage. The literal user-provided 2.0.0 record must migrate to 2.1.0 with port 8443 and all security fields preserved. Run it against the current source and observe rejection of the legacy record.
- [x] Add cases for each Review Focus item, port bounds, a custom valid port, and rollback command failure. Assert outcomes and state, not source-text presence.
- [x] Implement exact legacy-name acceptance, live-rule verification, destination collision refusal, verified rename/update, atomic metadata save, rollback, and legacy-aware uninstall.
- [x] Run the regression/packaging/runtime checks, then the documented application suite with native-tool environment variables. Final run: `python -m pytest tests -v --tb=short -o faulthandler_timeout=180` — 746 passed, 34 skipped, 587 subtests passed; 36 dependency deprecation warnings. No failures.
- [x] Document the failure cause and safe in-place upgrade procedure, including a ProgramData backup and no uninstall.

## Task 2: Sign, inspect, and publish the corrected installer

**Files:** Rebuild `release/KSATCoordinatorSetup-2.1.0.exe`; update only its entry in `release/SHA256SUMS.txt`.

**Interfaces:** Consume the existing signed `dist/KSATCoordinator.exe`; use the repository's signing and installer inspection helpers.

- [x] Compile `installer/KSATCoordinator.iss` with the installed Inno compiler, then sign with the existing approved CurrentUser publisher key without exporting it.
- [x] Verify Authenticode publisher/thumbprint, extract the installer, and verify its embedded executable exactly matches the existing signed Coordinator executable; run private-payload inspection.
- [x] Obtain an independent code review and resolve blocking findings before publication.
- [ ] Stage only this fix's source, tests, documentation, installer, and checksum; commit and fast-forward GitHub main plus the feature branch without overwriting remote changes.
- [ ] Verify the remote commit and downloaded installer checksum; provide the normal download link and explain that only the Coordinator installer changed.
