# Lab Package Builder acceptance record

Status: Builder **2.1.3** packages Client **2.1.3** with browser-controlled client lifetime and the retained hostname-resolution fallback. **User-approved pilot/prerelease only: physical elevated installation and standard-user launch/close/reopen still need lab verification before whole-lab deployment**. Coordinator remains 2.1.0.

## Builder 2.1.3 / Client 2.1.3 — browser lifecycle (2026-10-06)

- Opening the shortcut starts the protected client service on demand; closing the final KSAT tab starts a grace period, then saves the attempt, clears the login and safely stops the service. Durable pending results and existing exam deadlines are preserved. Uploads and updates resume on the next launch, not while fully closed. See the [behavior and required one-PC pilot](client-window-lifecycle.md).
- This session has no administrator access for a real installed-service pilot. The user explicitly approved publication as a pilot/prerelease after signed-build and isolated executable checks. No service installation, live student-data modification or new central-update bundle is claimed.
- Release-version tests failed against the prior 2.1.2 pins before the version change. The focused payload/release/package/launcher suite then passed **49 tests and 12 subtests** in 17.60 seconds.
- Final 2.1.3 application verification: `python -m pytest tests -v -o faulthandler_timeout=90`, with native Inno/extractor paths enabled, passed **810 tests and 596 subtests**, with **34 skipped** and 37 existing deprecation warnings, in **796.25 seconds** (exit 0). No production source changed after this run or the signed builds.
- Generic installer: **104,898,952 bytes**, SHA-256 `86d3374c682ea42fe038284f1264a0c604d3b12f055b551f5861d7623cca7b27`. Native compilation and extraction verified all four embedded resources against the approved payload.
- Builder: **139,520,648 bytes**, SHA-256 `6ba57b4b956cb0aa6c84a71f15778b9d7ba38e51730d9f16d4af372c30a45f2a`. Frozen version, exact embedded payload allowlist/hashes, recursive private-state inspection and signatures passed. The builder and bundled client both report 2.1.3.
- A disposable lab package passed native compile/sign/extract and receipt verification, with both receipt versions equal to 2.1.3. Test-only installer SHA-256: `ba2a2f6f97f459f98bbcd0772ae8b6b455fc725988f5e334605e7229a1e16dcd`. This synthetic-lab package is not distributed.
- Both EXEs use the unchanged `CN=KSAT LAB RELEASE SIGNING` publisher, thumbprint `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`. Only public certificate/update-key resources were recovered from the hash- and signature-verified previous builder; the private signing key stayed in this PC's Windows certificate store.
- The actual signed client executable's lifecycle, launcher, service-permission, runtime, outbox and connection code objects match source; embedded browser JavaScript matches exactly. It passed two isolated startup/restart checks against a disposable TLS Coordinator 2.1.0, accepted authenticated WebSocket presence, retained configuration bytes and listened only on IPv4 loopback. These console-mode checks do not substitute for the Windows service permission/shutdown pilot. The initial smoke harness had a token-regex escaping typo; correcting that harness passed without a product change.
- Coordinator installer SHA-256 remains `6a06aee788f5bbee01dee399506b6190fbb6af5d4ec61429917e561d5f4e7ddb`; previous 2.1.2 assets are retained. Use the same URL and two public connection files in Builder 2.1.3 to generate a new lab installer. Previously generated EXEs do not update themselves.
- Bare repository-wide pytest again reproduced the same **10 unrelated question-bank collection errors** in 4.11 seconds (missing PDF/schema dependencies and duplicate test-module names), listed in the older acceptance record below. These were not silently counted as passing application tests.

## Builder 2.1.2 / Client 2.1.2 — hostname fallback (2026-10-05)

- At the user's request, the builder release number now matches its bundled client: **2.1.2**. The earlier locally built 1.0.3 artifact below was never published and is superseded. The client installer/payload is unchanged by this numbering adjustment. The builder filename, Windows version resource, GUI and generated receipt use 2.1.2.
- Matching-number release verification: the updated builder output-name test failed before the version change, then the focused client/update/builder/packaging suite passed **126 tests and 22 subtests** in 42.39 seconds. Bare repository-wide collection still has the same ten unrelated question-bank errors listed below (4.37 seconds); this is not a claim that every repository test passes.
- Published-candidate Builder **2.1.2**: **139,382,048 bytes**, SHA-256 `0a2c46cd16a652a04c28ec14cd5c579618d63e0b441e74ba08a088498ce4c414`. Rebuilt, signed and rechecked with the exact same approved Client 2.1.2 payload, frozen resource allowlist and recursive private-state inspection. A fresh disposable package passed compile/sign/extract and asserted both receipt versions equal 2.1.2; its test-only installer SHA-256 is `e317cb81d177788c277508014d2e16dbf8f273941a45583e22b9d9fe3e3c3525`. This synthetic-lab package is not distributed.

- Lab evidence: configured `desktop-i7nsd5h.local` failed name resolution; the short Windows hostname resolved to the current server IPv4 address and TCP 8443 succeeded. The existing DHCP/certificate fix was already on GitHub main and in the published Coordinator installer. This change addresses the separate client name-resolution failure.
- Only single-label `.local` HTTPS names can fall back, and only after a typed DNS resolution error before any HTTP request is sent. The alternate dial name retains the original TLS SNI, CA verification, hostname verification and HTTP Host. No fixed IP, Windows hosts-file edit, configuration migration, new CA or coordinator change is involved. `.local` coordinator traffic bypasses environment proxies as direct lab traffic.
- The transport remembers only the successful short-name preference in memory; new TCP connections resolve it again. Failures reset the preference for a later operation, never replay an already-sent request. A slow OS DNS failure can receive up to one second of additional socket-connect timeout for the one safe fallback; this is not a hard wall-clock DNS deadline.
- TDD reproduced the original connection failure, the builder probe failure, and the slow-DNS one-shot failure before their respective fixes. Final real TLS suite: **16 passed**; independently rerun by the reviewer: **16 passed**. Includes changing addresses, original Host/SNI, untrusted CA, same-CA wrong hostname, successful POST and dropped-response no-replay, short-name failure/primary recovery, noneligible names and TCP refusal. Reviewer reported no release blockers.
- Full application run with native Inno/extractor paths enabled: **771 passed, 34 skipped, 587 subtests passed**, 41 existing deprecation warnings, in **727.24 seconds**. This run started before the final slow-DNS grace adjustment and five additional regression cases; the final 16-test transport run above verifies those later changes. Focused connection/packaging verification earlier passed **101 tests and 20 subtests**.
- Final post-review focused suite: **91 passed, 18 subtests passed** in 37.78 seconds, covering hostname transport, coordinator client, central-update manager/runner, builder GUI/controller, release assembly and lab package construction.
- Bare repository-wide `pytest -q` again stopped with the same ten unrelated question-bank tooling collection errors listed in the 1.0.2 record below: missing PDF/schema dependencies and duplicate module names. No unrelated tooling was changed.
- The final signed client executable's frozen connection modules match the reviewed source code objects. It passed two real startup/restart checks against an isolated TLS Coordinator 2.1.0, reported client version 2.1.2, retained configuration bytes, and listened only on IPv4 loopback. The first smoke harness lacked the `state` directory normally created by setup; correcting the harness resolved that check without a product change.
- Manual installation is selected for this release. The existing central-update implementation and pinned update verification key are unchanged; no new central-update bundle is supplied. See [manual deployment and limits](client-hostname-recovery.md).
- Final generic installer: **104,797,416 bytes**, SHA-256 `375190dfb749056afc29e6acf7a652c8fc44ad7dd597d4c4c97527cd52f6e702`. All four extracted resources matched the approved signed payload. Client/updater/guard were rebuilt; public publisher certificate and update verification key were recovered unchanged from the hash/signature-verified prior Builder.
- Superseded local Builder 1.0.3: **139,382,248 bytes**, SHA-256 `2df8e821155191748bf5cb08cb405e41a15d66fc3068f4cdf82bf2eddcd0a12c`. Frozen Tcl/Tk resources, exact payload allowlist/hashes, recursively decompressed private-state scan and pinned signatures passed. This unpublished artifact is not the matching-number release download.
- A disposable lab package passed native compile/sign/extract/receipt verification with Builder 1.0.3 and Client 2.1.2; SHA-256 `d1ea8dd524b1be5316911b9d2efd21d9fa2ec5fbe004f202d3be718c663f581c`. This test-profile package is not distributed. No physical elevated installation on a lab PC is claimed by these source/executable tests. Coordinator installer SHA-256 remains `6a06aee788f5bbee01dee399506b6190fbb6af5d4ec61429917e561d5f4e7ddb`.

## Builder 1.0.2 — installer control handoff (2026-10-01)

- A real client upgrade log showed administrative access, successful file replacement, then a generic post-install exception and a failed abort notification. The exact `GuardCommand` PowerShell fragment reproduced the failure locally: `$null` became an empty .NET string backup path in `File.Replace`, causing "The path is not of a legal form."
- The fix supplies `[NullString]::Value` while retaining atomic replacement and all maintenance-lock, active-test, pending-upload and trust checks. It applies to both completion and cancellation; no permissions, certificates, profiles or student databases are reset.
- New regression tests compile the actual shipped Pascal procedures into an unprivileged disposable Inno harness. Both configure-to-commit and configure-to-abort failed before the fix and passed after it. An independent reviewer reran these and the builder release tests: **5 passed**, no review findings.
- Native installer verification: **39 passed, 3 subtests passed** in 260.86 seconds (`test_lab_installer.py` plus `test_installer_control_handoff.py`, with Inno/extractor paths configured). This includes generic and lab modes and the existing trust-bundling variants.
- Reused signed client/updater/guard executables remain **2.1.1**. The only approved payload resource hash changed is `KSATClient.iss`; the public keys are unchanged. The refreshed manifest, recursive private-state scan, frozen payload allowlist/exact bytes and Authenticode publisher pin were verified. No Coordinator rebuild or central-update bundle was produced.
- Builder EXE: **139,373,728 bytes**, file version **1.0.2**, SHA-256 `927ce98d37f03cc58955fe580cb009bf7d946fedd9e2b4bb7e6872837b402657`.
- Corrected generic client installer: **104,790,752 bytes**, version **2.1.1**, SHA-256 `ba147b37ed6180f3d14d38cc3da9e505de2b7887c0053094243a41c645729344`. Every extracted file matched the approved payload. Both released EXEs use the unchanged pinned private-lab signer.
- A disposable lab profile passed native compile/sign/extract and receipt checks (Builder 1.0.2/client 2.1.1). Its test-only installer hash is `de23e427aafb3678d76c43e714491fdf74de7b9590cde844f85be68c9b8ea11d`; it is not distributed as a real-lab installer. No elevated KSAT install was run on this packaging PC.
- Bare repository-wide `pytest -q` cannot collect the separate question-bank tooling in this build environment: missing `pdfplumber`, `pypdf`, `jsonschema`, plus colliding `test_build` module names. The ten collection errors are in `python_vision_calibration/tests/{test_baseline,test_cli}.py`, `textbook_chapters/tests/{test_build,test_chapter02,test_chapter03,test_chapter04,test_vision_pipeline}.py`, and `scripts/{test_compile_logical_recovery_results,test_generate_v2_marker_overrides,test_run_logical_boundary_recovery}.py`. These unrelated tools/dependencies were not modified for this installer release.
- The first application-only run stopped progressing at 57% and was terminated without a pass/fail verdict; it was rerun with verbose progress, a 90-second stack-dump diagnostic and native installer tools enabled.
- Final application verification: **760 passed, 34 skipped, 587 subtests passed**, 41 deprecation warnings in 945.67 seconds. Command: `python -m pytest tests -v -o faulthandler_timeout=90`, with `KSAT_ISCC` and `KSAT_INNOEXTRACT` set. Exit status 0; no failures. This is the completed release test run, not the interrupted run above.

Use the new Builder to regenerate each lab package with the same server URL and public files. Close the failed installer before retrying on one pilot PC. Existing generated EXEs are unchanged; do not uninstall or delete ProgramData. The physical-lab checks below still apply.

## Builder 1.0.1 — simplified designated-PC workflow (2026-09-29)

- Main screen contains only server URL, CA PEM and public metadata JSON. Advanced controls are collapsed by default. Lab name is derived from the hostname; the default output is a unique folder under `Downloads\KSAT Lab Installers`.
- Missing saved tool paths are repaired through discovery. This packaging PC's signer/extractor and their dependencies are provisioned under `C:\Users\ravis\KSAT Build Tools`, outside temporary and app-virtualized directories. Its signing key stays in the Windows certificate store.
- Create checks the signer and TLS connection automatically; offline builds still require explicit acknowledgment. No client or Coordinator binary was rebuilt. The approved 2.1.1 client resources were recovered from the hash/signature-verified 1.0.0 Builder and revalidated unchanged.
- Independent review found no Critical or Important issues. Its Minor finding—old version numbers in receipts/diagnostics—was reproduced in failing tests, corrected to use the shared version constant, and verified passing.
- Real GUI preparation and native compile/sign/extract pipeline passed with a disposable offline test profile, automatically detected tools and hostname-derived name. Receipt reports 1.0.1. Disposable smoke installer SHA-256: `f6e8cd1a5ea03fc943cda34377702648d911313d0a0fddb06d60f6937ce69c53`. This test-profile installer is not distributed.
- Signed Builder SHA-256: `4f8c7a5994d35c1156ceeca337ff4a1a9dc5fba855ae2781380e888d9c15546b`; size **139,372,744 bytes**; EXE version **1.0.1**; publisher/pin unchanged. Frozen Tk/Tcl resources, embedded public-resource allowlist/hashes, recursive private-state scan and Authenticode verification passed.
- Frozen code objects for the Builder package, GUI, setup, build pipeline and controller match the final source after normalizing source filenames.
- Final unchanged-source regression run: **758 passed, 34 skipped, 587 subtests passed**, 35 deprecation warnings, 615.93 seconds. Command: `python -m pytest tests -q`, with native Node, Inno Setup and innoextract paths configured. No failures. The earlier run overlapped the final tool-discovery/fixture edits and is not the release acceptance result.
- The actual signed EXE was opened for desktop inspection: three inputs, readable layout at this PC's current scaling, successful bundled-client verification, Advanced expansion/collapse, and all three automatically populated tool paths were confirmed. The verification copy closed normally; the user's older open Builder was left untouched. This does not cover every display scaling or a physical student-PC installation.

## Historical 1.0.0 verification

- Shared strict public-profile validation, publisher pinning and Windows-store signing.
- Machine trust installed with explicit administrator approval on the designated packaging PC; exact publisher identity independently verified.
- Client, updater and guard frozen and signed as 2.1.1. Coordinator/wire metadata remain 2.1.0.
- Generic and lab Inno 6.7.3 installers compiled and extracted from disposable signed fixtures without installing them.
- Real lab pipeline compiled, signed and inspected an installer using disposable public Coordinator inputs; all embedded bytes matched approved resources.
- Final native smoke installer SHA-256: `a8ae7341a706e32b67aa1423110c3e7dffa544767f2c795f09a56f3d86264c40`. This is a test-profile artifact, not a real-lab installer for distribution.

- Final builder SHA-256: `3f18d5e68f50af78882eedab8f67d09b31728ac4a3f09cb1a64bad86e3a10638`.
- Final builder size: 139,367,024 bytes; version 1.0.0; bundled client version 2.1.1.
- Authenticode publisher: `CN=KSAT LAB RELEASE SIGNING`; thumbprint `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`.
- Frozen Tk/Tcl runtime, exact embedded resource allowlist/hashes, executable signatures and decompressed private-state scan passed.
- Final post-review suite: **724 passed, 32 skipped, 548 subtests passed**, 33 deprecation warnings in 414.87 seconds. Command: `python -m pytest tests -q`, with `KSAT_NODE`, `KSAT_ISCC` and `KSAT_INNOEXTRACT` set to the native tools. No test failures. Native compilation/extraction includes private-lab and standard signing modes.
- Final standalone EXE launch smoke: responsive `KSAT Lab Package Builder` window opened and closed normally, without starting from Python or the repository entrypoint. This is not a visual-layout or per-lab workflow acceptance test.

The GitHub Release asset carries the binary; `release/KSATLabPackageBuilder-1.0.0.sha256` records its checksum. No disposable test-profile installer is distributed as a real-lab installer.

## Independent review

The reviewer found no Critical issues, no deferred Minor issues and no out-of-scope behaviors declined for judgment. Three Important findings were reproduced and fixed:

- A slow live installer now retains its process-owned maintenance lock until an explicit terminal command or parent exit; elapsed time cannot restart the client mid-replacement.
- The standard release pipeline now signs and inspects the guard, stages the selected public publisher certificate, and preserves generic non-private/test signing without importing the private-lab root. Last-known-good seeding uses the selected signer pin.
- Cancellation during the final file hash is checked before atomic EXE publication; the incomplete receipt is removed.

Regression tests were observed failing before the fixes. Focused verification passed: 71 tests and 4 subtests; native private-lab/generic signing-mode verification passed in the final suite. No second reviewer pass was substituted for these tests.

## Implementation decisions

- Reused protected process-owned byte-range locks and Windows service control rather than a second mutex/custom IPC system; potential cost is compatibility/recovery rework.
- Inspected authenticated temporary database copies to avoid modifying original SQLite sidecars; potential cost is extra disk I/O and conservative retries.
- Used a signed public INI summary derived from the validated profile; a display mismatch could mislead, so output-byte inspection also verifies it.
- Retained shared public trust after uninstall rather than risk breaking another product; unused certificates may require IT cleanup.
- Blocked legacy version-1 or corrupt state for explicit administrator migration rather than silently rewriting it; potential cost is an extra IT step.
- Originally published the large 1.0.0 binary as a GitHub Release asset, with source/checksum/guides on the feature branch. That work has since been integrated into `main`; current downloads are linked from `release/README.md`.

## Not run — required pilot checks

The 1.0.1 desktop check above does not replace these remaining lab acceptance checks. Disposable lab PCs were not available. Do not infer these results from unit tests or successful compilation:

- GUI walkthrough at 100%, 150% and 200% scaling, long paths, keyboard-only navigation and invalid bundles.
- Generate two distinct real-lab packages through the GUI.
- One-EXE installation on two clean Windows client PCs, distinct identities and approved UAC flow.
- Student registration/sign-in, launched test, submission, faculty-close review and sign-out.
- Restart clients and Coordinator, then verify hostname-based reconnection.
- Existing same-server upgrade retaining records; wrong-lab rejection with unchanged data.
- Active-attempt/pending-submission refusal and service recovery following an interrupted install.
- Future generic central update on both profiles and recovery to their lab-specific cached installer.

Record PC names, dates, installer hashes and observed outcomes here after lab IT performs the pilot. Antivirus/SmartScreen acceptance is a separate check, not implied by Authenticode verification.
