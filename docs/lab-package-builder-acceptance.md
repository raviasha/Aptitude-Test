# Lab Package Builder acceptance record

Status: signed builder packaging verified; independent review/final regression check pending. **Not yet approved for whole-lab deployment**.

## Verified so far

- Shared strict public-profile validation, publisher pinning and Windows-store signing.
- Machine trust installed with explicit administrator approval on the designated packaging PC; exact publisher identity independently verified.
- Client, updater and guard frozen and signed as 2.1.1. Coordinator/wire metadata remain 2.1.0.
- Generic and lab Inno 6.7.3 installers compiled and extracted from disposable signed fixtures without installing them.
- Real lab pipeline compiled, signed and inspected an installer using disposable public Coordinator inputs; all embedded bytes matched approved resources.
- Native smoke installer SHA-256: `2e9cbbe7c6f396179dcc4a81f9d035244b74f4354ff493d66b42bfae49ba6190`. This is a test-profile artifact, not a real-lab installer for distribution.

- Builder SHA-256: `500ddc9d5f38440e3ba1c4f7b449b4e98d67babdcc70a746a115422d4baa778f`.
- Builder size: 139,364,800 bytes; version 1.0.0; bundled client version 2.1.1.
- Authenticode publisher: `CN=KSAT LAB RELEASE SIGNING`; thumbprint `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`.
- Frozen Tk/Tcl runtime, exact embedded resource allowlist/hashes, executable signatures and decompressed private-state scan passed.
- Full suite: 716 passed, 32 skipped, 547 subtests passed, 35 deprecation warnings. Follow-up scanner/release/payload tests: 10 passed and 4 subtests passed. Final regression run and independent review pending.

The GitHub Release asset carries the binary; `release/KSATLabPackageBuilder-1.0.0.sha256` records its checksum. No disposable test-profile installer is distributed as a real-lab installer.

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
