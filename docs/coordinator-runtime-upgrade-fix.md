# Coordinator 2.1.0 upgrade corrections — 29 September 2026

The corrected Coordinator installer and standalone EXE automatically migrate a
valid 2.0.0 runtime configuration to 2.1.0. The product version remains 2.1.0;
use the binaries from the commit containing this document, not an earlier download
with the same filename. Verify them against that commit's `release/SHA256SUMS.txt`.

The 29 September installer also corrects the legacy firewall ownership upgrade.
The earlier installer copied the application successfully but then rejected a
valid `firewall-owner.json` naming `KSAT Faculty Coordinator 2.0.0`. That caused
“The firewall ownership record is invalid” even when the Coordinator could run.
Re-running the corrected setup over that partial installation is supported; do
not delete the record, manually rename its contents, or disable the firewall.
The standalone Coordinator EXE is unchanged by this firewall-only correction.

## Upgrade the faculty server

1. Finish active exams and close the running Coordinator application, not just its
   browser tab.
2. Back up the entire `C:\ProgramData\KSAT Coordinator` directory while the
   Coordinator is stopped. Keep this backup outside the installation directories.
3. Run the corrected `KSATCoordinatorSetup-2.1.0.exe` as administrator, installing
   over the existing installation. Do not uninstall or delete ProgramData.
4. Keep the existing hostname and port. Setup validates and upgrades the old
   configuration automatically; no PowerShell edit is needed.
   Setup also verifies the existing 2.0.0 firewall rule, renames it to 2.1.0,
   and saves the updated ownership record. An already-current rule is supported.
5. Launch the Coordinator. Confirm existing student accounts and exam records are
   present, then test one client before deploying further.
6. For the Lab Package Builder, copy both current public connection files from
   `C:\ProgramData\KSAT Coordinator\public` after this successful startup:
   `coordinator-ca.pem` and `coordinator-public.json`. Use the same hostname/port
   recorded in the metadata. The metadata is republished as version 2.1.0; the
   existing CA and signing identity are retained.

No Client or Lab Package Builder rebuild is required for this correction. Do not
copy the server's `secrets` directory to clients or the packaging PC.

## Safety and scope

- Only the version marker changes in a valid, canonical 2.0.0 runtime file;
  hostname, bind address, port and interactive setting are preserved.
- The original runtime file is saved as
  `coordinator-runtime.pre-2.1.0.json.bak` beside the current file.
- The migration takes the Coordinator process lock and replaces the file
  atomically. Repeating a successful upgrade does not rewrite the backup.
- Unknown versions, malformed settings and conflicting backups are rejected, not
  silently reset. If one of these is reported, preserve the files for diagnosis.
- Student databases and private identity files are not migration inputs and are
  not rewritten by this configuration migration. The runtime backup is not a
  substitute for the full server-data backup in step 2.
- Firewall migration accepts only the exact supported 2.0.0/2.1.0 records and
  verifies the live executable path, port, TCP, inbound, allow, enabled and
  Private-profile settings before modification. A conflicting destination rule
  or a modified live rule is rejected without overwriting it.
- If saving the new ownership record fails, setup attempts to restore the old
  rule name and port. A rollback failure is reported explicitly; preserve the
  existing record and setup log for diagnosis rather than deleting either.

## Verification

Regression tests cover installer validation, startup, repeated upgrades,
malformed/unknown configurations, process locking, conflicting backups and failed
atomic replacement. Packaged verification uses disposable data and a non-elevated
probe whose decompressed application payload is checked against the production
EXE. It exercises the installer's `--validate-config` command, HTTPS startup,
student preservation, unchanged security identity and builder-compatible exports.

The signed installer is extracted to verify that its embedded executable matches
the signed Coordinator. An actual elevated installation and client connection on
the target lab remain pilot checks; the automated checks do not claim to reproduce
that lab's permissions, firewall, hostname resolution or network.

Firewall regression coverage compiles the actual installer Pascal functions and
executes their actual PowerShell verification scripts against disposable rule
objects. Only the external firewall boundary is substituted: tests never modify
the test machine's firewall. Cases cover the reported legacy record, current and
clean installs, collisions, tampered metadata, mismatched live rules, custom and
boundary ports, save/rollback failures, and legacy/current uninstall ownership.

For the 29 September firewall correction, the final application suite completed
with **746 passed, 34 skipped, and 587 subtests passed** (36 dependency deprecation
warnings). An independent review identified the current-record/leftover-legacy
collision case; its added regression failed before correction and passed after.
The final signed setup's SHA-256 is
`6a06aee788f5bbee01dee399506b6190fbb6af5d4ec61429917e561d5f4e7ddb`.
Its embedded Coordinator EXE remains byte-identical to the previously signed
runtime-upgrade build. Existing custom-path quoting and checks of additional
firewall filters were not expanded by this narrow correction; unusual custom
install paths or manually modified rules may still need separate diagnosis.
