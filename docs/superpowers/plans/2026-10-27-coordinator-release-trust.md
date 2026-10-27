# Coordinator Release Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Publish Coordinator 2.2.4 whose elevated private-lab installer installs pinned public publisher trust without a separate manual command.

**Architecture:** Bundle a narrow PowerShell trust helper and the selected public publisher certificate in Inno Setup. Verify resource hashes and certificate identity before any certificate-store write; invoke during setup preparation, before replacing application files. Both Coordinator-only and full release builders must use the same explicit private-lab opt-in definitions.

**Tech Stack:** Python, PowerShell 5.1, Inno Setup 6, PyInstaller, existing private-lab Authenticode signing.

**Spec:** `docs/superpowers/specs/2026-10-27-coordinator-release-trust-design.md`

## Global Constraints

- Coordinator 2.2.4 only; Client and Builder remain 2.1.6 and their existing release bundle remains unchanged.
- Preserve all ProgramData, records, lab connection identities and update verification keys.
- Only public certificate material may enter installers; no private key or network trust download.
- Validate exact certificate identity, code-signing purpose, validity and resource hashes before trust changes.
- Existing machine trust is retained on reinstall and uninstall; do not remove shared certificates.
- No actual development-PC or live lab trust-store changes during tests.
- Generic/non-lab builds must not automatically trust their signer.

## Review Focus

1. Partial existing trust (Root present, TrustedPublisher absent): add only the missing entry.
2. Wrong, expired, future-dated, or non-code-signing certificate: reject before the first write.
3. Import reports success but certificate remains absent: fail setup with an actionable error.
4. Non-lab build or mismatched packaged resource: never import an unintended certificate.
5. Upgrade from 2.2.3: preserve database, identity and previous migration backups while accepting the old runtime version.

### Task 1: Verified trust bootstrap and installer integration

**Files:** create `scripts/install_coordinator_release_trust.ps1`, `tests/test_coordinator_release_trust.py`, and a test-only PowerShell boundary fixture under `tests/fixtures/`; modify `installer/KSATCoordinator.iss`, `scripts/windows_release.py`, `scripts/build_coordinator_lab_release.py`, and packaging tests as necessary.

**Interfaces:**
- PowerShell helper parameters: mandatory `Certificate`, `ExpectedThumbprint`, `ExpectedSha256`; expected subject is `CN=KSAT LAB RELEASE SIGNING`. Output distinguishes existing and newly added stores; errors produce a nonzero exit.
- Python `prepare_coordinator_installer_trust(layout: ReleaseLayout, signing: ReleaseSigningConfig) -> list[str]`: for lab builds validate/export the public certificate to `dist/publisher.cer` and return explicit Inno definitions enabling bundled trust and binding its thumbprint. For non-lab builds return disabled trust definitions.
- Inno `EnsurePublisherTrust()` validates extracted helper/certificate hashes then runs the helper hidden, awaiting its exit. `PrepareToInstall` invokes it for both new and existing lab installations and returns a clear failure message on error.

- [ ] Add failing tests for absent, complete and partial existing trust; invalid hash/thumbprint/subject/EKU/date; import failure and unsuccessful post-import verification. Execute the real PowerShell helper with only certificate-store commands mocked in a child process.
- [ ] Run `python -m pytest tests/test_coordinator_release_trust.py -q`; observe the expected failures before implementation.
- [ ] Implement the helper using bounded public certificate input, validation before store access, idempotent exact-certificate checks and verified imports into LocalMachine Root/TrustedPublisher. Keep any already-added approved certificate on a later failure; retry must complete safely.
- [ ] Add failing packaging/integration checks for lab/non-lab compiler definitions, public-resource staging, setup ordering and no uninstall certificate removal.
- [ ] Implement Inno integration and the shared Python compiler-definition helper; use it from both build entry points. Include user-visible trust notice and retain normal administrator approval.
- [ ] Run helper, packaging and native Inno harness tests; inspect lab installer resources and verify non-lab compilation. Store tests must never access the real machine certificate stores.
- [ ] Commit this tested implementation.

### Task 2: Version, verify, build and publish

**Files:** modify Coordinator version declarations in `app.py`, `coordinator_main.py`, `scripts/windows_release.py`, `installer/KSATCoordinator.iss`, `static/app.js`, `static/index.html`; update corresponding version assertions, runtime-upgrade and import-smoke tests. Add `docs/coordinator-2.2.4.md`, update current release/rollout guides, and create `release/coordinator-2.2.4/` artifacts and verification records.

**Interfaces:** preserve handshake protocol `2.1.0`; accept runtime configuration versions through `2.2.3`, with current version `2.2.4`. Use the unchanged certificate thumbprint `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908` and existing update public key.

- [ ] Add/update the version and 2.2.3-to-2.2.4 preservation regression first; run and observe expected failures.
- [ ] Bump only Coordinator versions and retain 2.2.3 in the runtime migration allowlist. Do not alter historical release records or Client/Builder versions.
- [ ] Run focused trust/runtime/packaging/native tests and the application suite. Record skipped tests and any pre-existing calibration failures accurately, without changing unrelated pipeline fixtures.
- [ ] Build with `python -m scripts.build_coordinator_lab_release --signing-directory "C:/ProgramData/KSAT Release Signing/lab-2026" --output release/coordinator-2.2.4 --innoextract "C:/Users/ravis/KSAT Build Tools/innoextract670/innoextract.exe"` using the existing build Python. Verify signatures, extracted public helper/certificate/application bytes, frozen HTTPS startup and unchanged update public key.
- [ ] Obtain a bounded independent code review of certificate validation, setup integration, both build paths and version/data preservation; address actionable findings and re-run affected checks.
- [ ] Publish accurate installation notes: run the new installer once per server, no separate trust script, keep ProgramData, retry uploading the existing Client 2.1.6 bundle. State that real untrusted lab-server installation remains a field check.
- [ ] Commit measured verification/checksums and source. Recheck remote main and push only a normal fast-forward; preserve unrelated work and all older releases.
- [ ] Verify GitHub commit, download availability and artifact hashes; hand off the installer and next steps without claiming lab deployment.

## Execution and baseline

Recommended execution: native in this existing isolated worktree, with one final independent review. Tasks share the same small installer/build boundary, so splitting implementation adds little value.

Baseline: 66 passed, 13 native-environment skips, 12 subtests passed in the focused Coordinator/runtime/packaging suite. The prior release's repository-wide calibration checksum failures are already documented and outside this change.

Plan self-review: all approved spec requirements map to the two tasks; all five review-focus cases have explicit test steps. No client rebuild, certificate rotation or live deployment is included.
