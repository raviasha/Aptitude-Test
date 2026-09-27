# KSAT Lab Client Package Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a separate Windows GUI that builds a signed, preconfigured single-EXE client installer for each lab, including certificate setup and compatibility with central updates.

**Architecture:** A frozen Tkinter application uses shared public-profile validation, a certificate-store signing adapter and an isolated Inno Setup packaging pipeline. A lab mode extends the existing client installer rather than wrapping it in another installer. Installation preflight and lifecycle coordination preserve existing records and refuse unsafe or different-server upgrades.

**Tech Stack:** Python, Tkinter/ttk, cryptography, PyInstaller, Inno Setup 6.7.3, Windows certificate store and SignTool, existing client SQLite/identity stores, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-09-27-lab-client-package-builder-design.md` (approved 27 September 2026).

## Global Constraints

- A separate Windows application, **KSAT Lab Package Builder.exe**, creates one self-contained client installer for each lab.
- An authorised administrator still approves Windows elevation.
- Use Inno Setup 6.7.3 to match the current release inspection toolchain.
- Only the EXE needs to be distributed to students.
- The private signing key remains protected on the designated packaging computer and is not embedded in the builder distribution or generated installers.
- No new per-lab update channel, signing key or manifest protocol is introduced.
- Reject a running assessment or unacknowledged submission with an actionable message; never silently stop an examination to install.
- No application changes, real certificate imports, service manipulation, package publication or live-lab installation occur while writing this plan.
- Preserve the existing unstaged deletion of `release/KSATCoordinatorSetup-2.1.0.exe`. Do not restore, stage or commit it as part of this work.
- Execute in an isolated worktree established through the worktree skill. No automatic push or merge.

## Concrete version and compatibility decisions

- New client software version: **2.1.1**. Builder version: **1.0.0**. Existing Coordinator version stays **2.1.0**; no Coordinator rebuild is needed for this feature.
- Keep metadata/protocol compatibility at **2.1.0**. The current metadata validator checks that literal version; do not globally replace it with the new application version.
- Split the existing release tool's common product version only where needed to produce a 2.1.1 client with a 2.1.0 Coordinator. Existing release artifacts remain intact.
- Same-server means equal normalized effective URL, CA SHA-256 and protocol signing public key. Compare the effective saved URL, including `coordinator-url.json`, rather than just the original install configuration.
- Initial publisher pin for the current private-lab release is `CN=KSAT LAB RELEASE SIGNING`, thumbprint `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`. Do not provision a new identity merely to make a build pass.
- Legacy clients without the new maintenance handshake cannot be assumed safe to stop from a stale idle snapshot. Require them to be stopped by an administrator before offline preflight; if running, refuse with instructions to finish all tests/uploads and stop the KSAT client service. Fresh installs and later handshake-enabled clients retain the normal integrated flow. Never bypass this limitation with a force-stop.

## Review Focus

1. A lab name or output directory containing Unicode, reserved Windows names, quotes or shell characters must not produce executable-source injection or overwrite unrelated files (Tasks 1 and 6).
2. A client with a separately changed runtime URL must not be mistaken for the original server just because its installation JSON still matches (Task 3).
3. An installer crash or a second install while a maintenance lease exists must not allow a new attempt during replacement or leave the client permanently locked (Tasks 3 and 4).
4. A central rollback using a lab-specific last-known-good installer must still work without creating a general-purpose downgrade bypass (Tasks 4 and 5).
5. A compiler/signing process completing after cancellation or after an output-file collision must not publish an unverified or overwritten package (Task 6).

## File structure and responsibilities

| File | Responsibility |
| --- | --- |
| `ksat/public_trust.py` | Pure reusable Coordinator public-bundle validation. |
| `ksat/lab_builder/profile.py` | Versioned lab profile, slug, canonical resource hashes. |
| `ksat/lab_builder/signing.py` | Windows-store signing, identity selection and verification. |
| `ksat/lab_builder/payload.py` | Approved payload manifest, allowlist and inspection. |
| `ksat/lab_builder/build.py` | Isolated compile/sign/inspect/publish transaction. |
| `ksat/lab_builder/controller.py` | GUI state, cancellation, settings and worker coordination. |
| `ksat/lab_builder/gui.py`, `lab_package_builder.py` | Tkinter screen and frozen entry point. |
| `ksat/client/install_guard.py`, `client_install_guard.py` | Protected installation guard and helper entry point. |
| `installer/KSATClient.iss` | Generic/lab installer modes using existing client product identity. |
| `scripts/build_lab_package_builder.py` | Maintainer-only frozen builder/payload release assembly. |
| Existing `client_app.py`, `ksat/client/updater.py`, `scripts/windows_release.py` | Narrow integration points; no unrelated refactors. |

Create `ksat/lab_builder/__init__.py` with Task 1. Keep filesystem-independent validation separate from OS-specific signing/installation. Existing tests use unittest classes; new files should follow that pattern and also run under pytest.

## Execution environment and baseline

Use the repository's project Python environment with its declared application dependencies. Record its resolved executable as `$ksatPython` before commands below. Do not install dependencies into another application's environment. Use the existing `tests` directory only, not recursive root discovery that also collects unrelated textbook tools.

- [ ] Confirm clean task-owned changes in the isolated worktree and read both spec and plan.
- [ ] Run `& $ksatPython -m pytest tests/test_windows_entrypoints.py tests/test_windows_packaging.py tests/test_windows_authenticode.py tests/test_client_updater.py tests/test_client_updates.py -q`. Record passes, failures and skips before edits; investigate baseline failures separately.

### Task 1: Share public trust validation and define lab profiles

**Files:** Create `ksat/public_trust.py`, `ksat/lab_builder/__init__.py`, `ksat/lab_builder/profile.py`, `tests/test_lab_profile.py`; modify `client_app.py` around `install_client_configuration` and `_validate_production_trust`; extend `tests/test_windows_entrypoints.py`.

**Interfaces:**
- `PublicTrustBundle` frozen dataclass: `base_url: str`, `ca_pem: bytes`, `metadata_json: bytes`, `ca_sha256: str`, `signing_public_key_b64: str`.
- `validate_public_bundle(base_url: str, ca_pem: bytes, metadata_json: bytes, *, now: datetime) -> PublicTrustBundle`; no filesystem writes or imports into trust stores.
- `LabProfile` frozen dataclass: `format_version: int`, `lab_name: str`, `lab_slug: str`, `trust: PublicTrustBundle`, `sha256: str`.
- `make_lab_profile(lab_name: str, trust: PublicTrustBundle) -> LabProfile`; `encode_profile(profile: LabProfile) -> bytes`; `decode_profile(data: bytes, *, now: datetime) -> LabProfile`.

- [ ] Add failing tests named `test_current_metadata_remains_compatible`, `test_duplicate_json_keys_rejected`, `test_private_key_pem_rejected`, `test_url_binding_and_ca_key_fingerprint`, `test_expiry_checked`, `test_slug_cannot_escape_output`, `test_profile_round_trip_is_canonical`. Assertions: metadata version remains `"2.1.0"`; canonical round trip preserves `sha256`; bad inputs raise `ValueError`; `lab_slug` contains only lowercase ASCII letters, digits and hyphens.
- [ ] Run `& $ksatPython -m pytest tests/test_lab_profile.py -q`; confirm failure identifies missing implementation, not missing dependencies.
- [ ] Extract validation without side effects and retain public error compatibility. Keep the existing CA maximum **256 KiB**, metadata maximum **64 KiB**, profile maximum **512 KiB**; reject duplicate keys/non-finite JSON values, unknown fields, multiple certificates and private-key blocks. Accept a 1–80-character display name after trimming; derive a bounded safe slug and reject DOS-reserved names or an empty ASCII slug with a friendly rename request.
- [ ] Run the new tests plus `tests/test_windows_entrypoints.py` and `tests/test_linux_client.py`; require pass without changing metadata emitted by Coordinator.
- [ ] Commit only Task 1 files with `refactor: share coordinator public trust validation`.

### Task 2: Protected signing and approved payload validation

**Files:** Create `ksat/lab_builder/signing.py`, `ksat/lab_builder/payload.py`, `tests/test_lab_builder_signing.py`, `tests/test_lab_builder_payload.py`; reuse `ksat/windows_authenticode.py` and the inspection rules in `scripts/windows_release.py`.

**Interfaces:**
- `SignerSelection` dataclass: `store_location: Literal['CurrentUser','LocalMachine']`, `thumbprint: str`, `publisher: str`, `timestamp_url: str | None`, `lab_identity: bool`.
- `inspect_signer(selection: SignerSelection) -> AuthenticodeIdentity`; `sign_from_store(path: Path, selection: SignerSelection, signtool: Path) -> AuthenticodeIdentity`.
- `ApprovedPayload` dataclass: `root: Path`, `client_version: str`, `files: dict[str,str]` (relative path to SHA-256), `publisher: str`, `thumbprint: str`.
- `load_approved_payload(root: Path) -> ApprovedPayload`; `verify_payload(payload: ApprovedPayload) -> None`.

- [ ] Add failing signer tests asserting no PFX path/password or `/p` appears in any child arguments; exact thumbprint/store selection is used; wrong EKU, expiry, missing private key, untrusted signature and publisher mismatch block success. Add payload tests asserting modified bytes, unknown files and private-key material are rejected.
- [ ] Run both new test files and confirm expected failures.
- [ ] Implement a fixed-script/native Windows-store adapter with hidden child processes, structured output, argument lists and bounded timeouts. Use SignTool's certificate-store selection; institution mode requires the existing approved HTTPS timestamp policy, private-lab mode uses the existing no-timestamp policy. Verify the completed executable with Windows trust plus the expected pin.
- [ ] Bind payload files to the signed frozen builder through a bundled manifest. Allow only client/updater/guard executables, public certificates/verification keys and fixed installer resources. Do not let users select arbitrary executable payloads. Preserve the generic client's prohibition on embedded lab URLs; profile exceptions apply only to explicit installer resources.
- [ ] Run `& $ksatPython -m pytest tests/test_lab_builder_signing.py tests/test_lab_builder_payload.py tests/test_windows_authenticode.py tests/test_windows_packaging.py -q`; require pass. Real certificate-store tests must be explicitly gated and use a disposable test identity, never import a production key automatically.
- [ ] Commit Task 2 files with `feat: verify and sign lab package payloads`.

### Task 3: Race-safe installation preflight and maintenance guard

**Files:** Create `ksat/client/install_guard.py`, `client_install_guard.py`, `tests/test_client_install_guard.py`; modify `client_app.py` startup/attempt dispatch and the existing client lifecycle integration; consult `ksat/client/store.py`, `ksat/client/windows_service.py` and `ksat/coordinator/process_lock.py`.

**Interfaces:**
- `InstallCheck` dataclass: `state: Literal['fresh','same_server','blocked']`, `installed_version: str | None`, `diagnostic_code: str | None`.
- `inspect_installation(program_data: Path, profile: LabProfile, target_version: str) -> InstallCheck`; must not create device identities, migrate databases or alter configuration.
- `InstallationLease` context manager owns a process-bound Windows maintenance mutex and authenticated local control channel; methods `prepare(profile: LabProfile, target_version: str) -> InstallCheck`, `commit() -> None`, `abort() -> None`.
- Guard CLI receives only a protected staging directory and parent installer PID; it loads fixed resource names, never caller-supplied scripts or secrets. Return structured diagnostic codes, not raw configuration.

- [ ] Add failing tests `test_runtime_url_conflict_blocks_without_writes`, `test_pending_submission_blocks`, `test_active_attempt_blocks`, `test_start_attempt_race_is_serialized`, `test_second_installer_is_rejected`, `test_dead_owner_releases_gate`, `test_unreadable_state_fails_closed`, `test_legacy_running_service_refused`. Assert byte-for-byte unchanged existing config/identity/state on rejected preflight and no stop-service operation on active-attempt refusal.
- [ ] Run `& $ksatPython -m pytest tests/test_client_install_guard.py -q`; confirm the missing API fails.
- [ ] Implement public-bundle and effective-config comparison using Task 1. Read identity without `load_or_create`; validate the protected store without inventing missing state. Treat partial/corrupt installed state as blocked, not fresh.
- [ ] Implement an admin/SYSTEM-only local maintenance channel for handshake-enabled clients. Serialize acquisition with attempt creation and background update scheduling; a successful lease prevents new attempts, then quiesces the service and acquires its existing lifecycle lock. Recheck pending work before replacement. Use process ownership and bounded recovery, not a freely writable flag file. Old running clients lacking this capability fail closed as stated above.
- [ ] Verify race, process-death and service-recovery tests plus existing entrypoint/runtime/API tests. Require `& $ksatPython -m pytest tests/test_client_install_guard.py tests/test_windows_entrypoints.py tests/test_client_runtime.py tests/test_client_app_api.py -q` to pass.
- [ ] Commit Task 3 files with `feat: guard client installs against active assessment work`.

### Task 4: Lab installer mode and automatic trust setup

**Files:** Modify `installer/KSATClient.iss`; create `tests/test_lab_installer.py`; extend `client_install_guard.py` and the release tool's frozen helper inputs only as necessary.

**Interfaces:** Inno compile definitions `KSAT_LAB_MODE` (0/1), `KSAT_PAYLOAD_DIR`, `KSAT_OUTPUT_DIR`, `KSAT_CLIENT_VERSION`; all paths supplied as argument-list elements. Lab resources have fixed filenames `lab-profile.json`, `coordinator-ca.pem`, `coordinator-public.json`, `publisher.cer`. Use Task 3 guard for privileged preparation and lifecycle coordination; no user label is interpolated into generated Inno source.

- [ ] Add failing installer contract and helper tests asserting generic mode retains current selection pages; lab mode shows lab/server/version and skips URL/file controls; bundled/profile hashes are checked before trust import; same-server upgrade keeps data; wrong-server installation makes no changes; publisher import is idempotent; pre-existing/shared certificates survive uninstall.
- [ ] Run `& $ksatPython -m pytest tests/test_lab_installer.py -q`; confirm expected contract failures.
- [ ] Implement lab mode with the existing AppId and service. Extract into a uniquely owned admin/SYSTEM staging directory and reject reparse-point substitutions. Keep files hash-bound until privileged use. Integrate Task 3 before payload replacement; cancellation releases the lease and restores prior availability. Install pinned publisher Root/TrustedPublisher trust and Coordinator CA, recording only trust entries actually added.
- [ ] Distinguish fresh configuration from existing same-server state. Reject interactive downgrades; add a rollback context validated against the protected updater journal and exact cached installer digest, not a bare `/ALLOWDOWNGRADE` flag. Local health success with unavailable Coordinator is reported as installed but disconnected.
- [ ] Compile generic and lab variants against test-signed fixture payloads in disposable directories. Extract and inspect both. Run `& $ksatPython -m pytest tests/test_lab_installer.py tests/test_windows_packaging.py tests/test_windows_entrypoints.py -q`; require pass, no live client service/certificate changes.
- [ ] Commit Task 4 files with `feat: add preconfigured lab client installer mode`.

### Task 5: Versioning and generic central-update compatibility

**Files:** Modify `client_app.py`, `installer/KSATClient.iss`, `ksat/client/updater.py`, `scripts/windows_release.py`; extend `tests/test_client_updater.py`, `tests/test_client_update_end_to_end.py`, `tests/test_windows_packaging.py`, `tests/test_windows_entrypoints.py`.

**Interfaces:** `CLIENT_VERSION = '2.1.1'` and `COORDINATOR_VERSION = '2.1.0'` in the release tool; client runtime/installer agree on 2.1.1. Existing metadata wire version remains 2.1.0. Keep central update bundle schema and public verification key unchanged. Updater creates the protected rollback context consumed by Task 4 only while recovering its own failed update.

- [ ] Write failing assertions for product versions, unchanged metadata acceptance, one generic update serving two lab profiles, preserved config/identity digests, and rollback from a failed generic update to a same-server lab installer. Assert a forged rollback argument without a matching protected journal fails.
- [ ] Run the affected tests and confirm version/integration failures before modifications.
- [ ] Make narrow product-version changes, preserve old release filenames, connect maintenance lease and rollback context to the updater, and retain last-known-good seeding. Do not rebuild or change Coordinator merely to release the client.
- [ ] Run `& $ksatPython -m pytest tests/test_client_updater.py tests/test_client_update_end_to_end.py tests/test_client_updates.py tests/test_windows_packaging.py tests/test_windows_entrypoints.py -q`; require pass.
- [ ] Commit Task 5 files with `feat: retain central updates for lab-configured clients`.

### Task 6: Build pipeline and output publication

**Files:** Create `ksat/lab_builder/build.py`, `tests/test_lab_package_build.py`.

**Interfaces:**
- `BuildTools` dataclass: `iscc: Path`, `signtool: Path`, `innoextract: Path`; builder setup must detect the compatible inspection tool as well as compiler/signer.
- `BuildRequest` dataclass: `profile: LabProfile`, `payload: ApprovedPayload`, `signer: SignerSelection`, `tools: BuildTools`, `output_dir: Path`.
- `BuildEvent` dataclass: `stage: str`, `message: str`; `BuildResult`: `installer: Path`, `receipt: Path`, `sha256: str`.
- `build_lab_installer(request: BuildRequest, *, cancel: threading.Event, progress: Callable[[BuildEvent], None]) -> BuildResult`.

- [ ] Add failing tests `test_compile_sign_inspect_publish_order`, `test_output_collision_never_overwrites`, `test_cancelled_signer_cannot_publish`, `test_unicode_and_shell_paths_are_data`, `test_private_payload_never_shipped`, `test_changed_resource_rejected_before_sign`, `test_receipt_failure_reports_no_success`. Assert only verified output can reach `BuildResult` and cancellation preserves every pre-existing output file.
- [ ] Run `& $ksatPython -m pytest tests/test_lab_package_build.py -q`; confirm missing implementation failures.
- [ ] Implement isolated staging with allowlisted copies and a fixed template. Run all children with `shell=False`, hidden windows and bounded timeouts. Validate ISCC 6.7.3; compile, sign through Task 2, recursively inspect via innoextract, recheck hashes, then publish with no-overwrite semantics. A reserved output name and exact owned-path cleanup prevent races. Recheck cancellation immediately before publication; do not publish a late child result.
- [ ] Write a JSON receipt with version, profile hash, signer pin and installer hash, excluding raw metadata/private paths. Publish the receipt before the final EXE commit and remove only this operation's receipt on failure; success is reported only once both exist.
- [ ] Run build/payload/signing tests; require pass. Perform one real compile/inspection with disposable fixture resources, record output hashes, do not install it.
- [ ] Commit Task 6 files with `feat: build signed lab installers atomically`.

### Task 7: Administrator GUI and diagnostics

**Files:** Create `ksat/lab_builder/controller.py`, `ksat/lab_builder/gui.py`, `lab_package_builder.py`, `tests/test_lab_builder_gui.py`.

**Interfaces:**
- `BuilderSettings` dataclass contains only compiler/signing/inspection paths and `SignerSelection`; store under the current administrator's local application data, never in the student package.
- `BuilderController` methods: `validate_inputs(lab_name: str, base_url: str, ca_path: Path, metadata_path: Path) -> LabProfile`, `test_connection(profile: LabProfile) -> bool`, `start_build(request: BuildRequest, *, offline_acknowledged: bool) -> None`, `cancel() -> None`, `drain_events() -> list[BuildEvent]`.
- `main(argv: list[str] | None = None) -> int` in `lab_package_builder.py`; window title **KSAT Lab Package Builder**. `start_build` uses Task 6 on one worker; Tk changes run on the UI thread only.

- [ ] Add failing controller tests: all five per-lab fields validated; metadata populates but does not silently override an edited URL; offline build needs explicit acknowledgement; repeated clicks start only one worker; settings contain no lab profile/password; user cancellation closes no unrelated process; errors remain readable.
- [ ] Run `& $ksatPython -m pytest tests/test_lab_builder_gui.py -q`; confirm expected failures.
- [ ] Implement the setup status view and per-lab screen from the spec with file/folder pickers, selected version/hostname, stage progress, Cancel, **Validate and Create Installer**, **Test connection** and **Open output folder**. Clearly show that administrator approval is still needed on clients. Use existing CoordinatorClient TLS rules without redirects or disabled verification; missing network is not a malformed-profile error.
- [ ] Add bounded sanitized diagnostic export preview. Use fixed diagnostic codes with actionable messages for each spec failure class. Close-window behaviour cancels safely or waits for publication to finish; never silently claims success.
- [ ] Run GUI/controller tests and manually check the Windows GUI at 100%, 150% and 200% scaling, keyboard-only navigation, long paths and an invalid bundle. Record screenshots; do not infer GUI quality from controller tests.
- [ ] Commit Task 7 files with `feat: add lab package builder desktop interface`.

### Task 8: Frozen delivery and acceptance record

**Files:** Create `scripts/build_lab_package_builder.py`, `tests/test_lab_builder_release.py`, `docs/lab-package-builder.md`, `docs/lab-client-installation.md`, `docs/lab-package-builder-acceptance.md`; modify `WINDOWS_EXE_BUILD.md` and `release/README.md` only for this feature's versioned outputs.

**Interfaces:** `assemble_builder_resources(payload_root: Path, destination: Path) -> Path` produces the hash-bound manifest and allowed resources. Maintainer CLI `build_lab_package_builder.py --payload-root <verified-root> --output-dir <new-output>` creates the windowed builder for the existing release-signing pipeline; it must not read arbitrary parent directories.

- [ ] Add failing tests asserting builder version 1.0.0, approved client 2.1.1, frozen Tk resources present, separate public update key intact, all executable signatures verified and no PFX/password/private-key/client-state files in recursive extraction. Assert absence of Python/repository prerequisites for the operator.
- [ ] Run `& $ksatPython -m pytest tests/test_lab_builder_release.py -q`; confirm expected failure.
- [ ] Implement frozen release assembly and sign/inspect/hash with the established maintainer credentials only. Fail clearly if signing authority or the compiler is unavailable; do not bypass antivirus or restore quarantined files to achieve a build.
- [ ] Write the administrator guide covering official one-time tool setup including innoextract, protected certificate-store provisioning, five per-lab fields, offline verification limits, and distributing only the resulting EXE. Write the student/staff guide covering UAC, no separate trust script, and the legacy-running-client limitation. Do not imply initial private-publisher warnings vanish automatically.
- [ ] Set `KSAT_NODE` to the resolved supported Node executable, then run `& $ksatPython -m pytest tests -q`. The suite invokes the client UI harness with its required script/scenario arguments; do not run that harness without arguments. Record exact failures/skips and compare baseline; a skipped Node test is not UI verification. No broad textbook/data-engineering discovery is needed.
- [ ] Complete the spec's disposable Windows pilot: two lab profiles, two fresh clients with distinct identities, same-server upgrade retaining records, wrong-server refusal, active/pending-work refusal, restart, generic central update and rollback. Mark any unavailable physical test as **not run**, never as passed.
- [ ] Record artifact hashes, signer identities and test results in the acceptance document. Request an independent whole-branch review focused on privileged installation, trust and data preservation; resolve findings and rerun affected tests.
- [ ] Commit only feature source/docs and explicitly verified deliverables. Present the builder and guides for user acceptance. Do not push, merge, publish a central update or install in a real lab without the relevant user request.

## Plan self-review and handoff

Coverage: profile validation (Task 1), signing/approved inputs (Task 2), lifecycle/data protection (Tasks 3–4), central updates/rollback/versioning (Task 5), atomic packaging (Task 6), GUI/error handling (Task 7), frozen delivery/docs/physical acceptance (Task 8). Each Review Focus item has named tests in its owner task. Protocol 2.1.0 is separate from application 2.1.1, and no private signing material is part of lab inputs.

This plan is ready for user review. Implementation starts only after that review and selection of native execution or subagent-driven execution. Recommended: native execution with a final independent review because installer, guard and updater interfaces are tightly coupled; use subagent-driven execution if the user prefers an independent review gate after every task.
