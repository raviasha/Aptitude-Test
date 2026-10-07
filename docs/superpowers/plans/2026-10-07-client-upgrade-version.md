# Client upgrade version correction — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish signed Client and Package Builder 2.1.4 that allow legitimate upgrades and create independent installers for different labs without weakening installation safety.

**Architecture:** Put the client release version in one dependency-light module used by the client, installation guard, build tooling and builder. Retain strict installed-version comparison, same-coordinator identity checks, active-test/pending-upload protection and all certificate verification. Verify the frozen installation helper, not only installer labels and file hashes.

**Tech Stack:** Python, pytest/unittest, PyInstaller, Inno Setup, Windows Authenticode, GitHub Releases.

**Spec:** User-approved correction in this chat (2026-10-07): fix the stale 2.1.1 upgrade target in the published 2.1.3 package, publish Client/Builder 2.1.4, and verify packages generated for a different lab. Existing lifecycle behavior is specified in `docs/superpowers/specs/2026-10-06-client-window-lifecycle.md` and is not redesigned here.

## Global constraints

- Client and Builder versions are both 2.1.4; Coordinator remains 2.1.0.
- Builder inputs remain the server URL and that server's two public connection files.
- A fresh PC accepts either lab's independently generated package; an existing PC accepts only its own coordinator's package unless a separate migration is explicitly authorized.
- No uninstall, registry-version workaround, data deletion, trust bypass, signing-key export or service-permission broadening.
- Preserve unrelated working-tree edits and previous release assets.
- Publish as a pilot/prerelease; a real elevated installation and standard-user lab pilot are still required and must not be represented as completed by isolated tests.

## Review focus

1. An installed 2.1.2 or 2.1.3 client must not be compared with a stale target embedded in the helper.
2. Same-version reinstall must remain allowed when otherwise safe; a genuinely newer installed version must remain blocked.
3. Two labs must receive their own URL, CA and signing identity, without packaging-PC coordinator state leaking into either package.
4. A wrong-lab package must fail before service/trust/configuration mutation and preserve existing records.
5. Published generic and builder-generated installers must contain the corrected signed helper, not merely display the new version.

## Task 1: Correct and verify the release end to end

**Files**

- Create `ksat/client/version.py`: dependency-light `CLIENT_VERSION = "2.1.4"`.
- Modify `client_install_guard.py`, `client_app.py`, `ksat/lab_builder/__init__.py`, `ksat/lab_builder/payload.py`, `scripts/windows_release.py`: use the shared constant through existing public aliases.
- Modify `scripts/build_lab_package_builder.py`: derive Windows version-resource values from `BUILDER_VERSION` rather than independent literals.
- Modify `installer/KSATClient.iss`: default client version 2.1.4, checked against the shared constant in tests.
- Add tests in `tests/test_lab_installer.py`, `tests/test_lab_builder_release.py` and `tests/test_windows_packaging.py`; update release-specific expectations in existing payload/package tests.
- Update release links/checksums and installation/acceptance documentation after verification.

**Interfaces**

- Shared `ksat.client.version.CLIENT_VERSION: str` is the authoritative client/builder target. Existing `_CLIENT_VERSION`, `TARGET_VERSION`, `BUILDER_VERSION` and packaging aliases remain available to their current callers.
- Keep `inspect_installation(program_data, profile, target_version, rollback_authorized=False)` and the protected helper/session entrypoints unchanged.

- [ ] Add failing tests asserting the actual helper target agrees with runtime/build/builder versions, and that its real preflight accepts installed 2.1.2/2.1.3 with the matching profile. Run them before production changes; expect the existing 2.1.1 target to fail.
- [ ] Add coverage for same-version reinstall, genuine newer-version rejection, and active/pending state preservation using disposable data and mocked Windows registry/service boundaries only.
- [ ] Add two independently generated coordinator profiles: fresh configuration must contain each profile's exact URL/CA/identity; a cross-lab replacement must fail with existing bytes and service state unchanged.
- [ ] Implement the shared version and resource derivation, retaining all existing safety checks. Run focused guard/installer/payload/build/version tests; require no failures.
- [ ] Run bare repository pytest and the full application suite with native Inno/extractor paths enabled. Record unrelated question-bank collection errors separately by name rather than treating them as passing.
- [ ] Build and sign new client/updater/guard, generic installer and Builder 2.1.4 using the existing protected Windows signing identity. Verify signatures, checksums and exact embedded files.
- [ ] Inspect the frozen helper's effective target and execute its preflight logic against disposable upgrade cases. Compile/sign/extract two different synthetic-lab installers and confirm each embeds only its own public connection inputs plus the corrected helper. Do not distribute these synthetic-lab packages.
- [ ] Run actual signed-client isolated startup/restart checks, retain the coordinator binary/hash, and record the unperformed elevated/standard-user pilot explicitly.
- [ ] Obtain one independent review of the whole correction and address findings with regression tests before release.
- [ ] Commit only task files; fast-forward GitHub main and the feature branch. Publish new immutable 2.1.4 pilot assets, verify public-download hashes, and identify 2.1.3 as superseded in the current download instructions. Do not replace old assets in place.

## Completion evidence

Record test commands/results, source commit, both release hashes, frozen-helper target, two-lab package checks and the remaining physical pilot in `docs/lab-package-builder-acceptance.md`. Deliver direct verified downloads and instructions to regenerate each lab's installer with that lab's unchanged URL and two public files.
