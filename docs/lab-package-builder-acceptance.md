# Lab Package Builder acceptance record

Status: independent review completed; all three Important findings fixed and final signed artifacts verified. **Pilot release only: not yet approved for whole-lab deployment**.

## Verified so far

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
- Published the large binary as a GitHub Release asset, with source/checksum/guides on the feature branch; the download location differs from older repository binaries, and `main` is unchanged.

## Not run — required pilot checks

Native desktop automation and disposable lab PCs were not available in this execution environment. Do not infer these results from unit tests or successful compilation:

- GUI walkthrough at 100%, 150% and 200% scaling, long paths, keyboard-only navigation and invalid bundles.
- Generate two distinct real-lab packages through the GUI.
- One-EXE installation on two clean Windows client PCs, distinct identities and approved UAC flow.
- Student registration/sign-in, launched test, submission, faculty-close review and sign-out.
- Restart clients and Coordinator, then verify hostname-based reconnection.
- Existing same-server upgrade retaining records; wrong-lab rejection with unchanged data.
- Active-attempt/pending-submission refusal and service recovery following an interrupted install.
- Future generic central update on both profiles and recovery to their lab-specific cached installer.

Record PC names, dates, installer hashes and observed outcomes here after lab IT performs the pilot. Antivirus/SmartScreen acceptance is a separate check, not implied by Authenticode verification.
