# Coordinator 2.1.0 upgrade correction — 28 September 2026

The corrected Coordinator installer and standalone EXE automatically migrate a
valid 2.0.0 runtime configuration to 2.1.0. The product version remains 2.1.0;
use the binaries from the commit containing this document, not an earlier download
with the same filename. Verify them against that commit's `release/SHA256SUMS.txt`.

## Upgrade the faculty server

1. Finish active exams and close the running Coordinator application, not just its
   browser tab.
2. Back up the entire `C:\ProgramData\KSAT Coordinator` directory while the
   Coordinator is stopped. Keep this backup outside the installation directories.
3. Run the corrected `KSATCoordinatorSetup-2.1.0.exe` as administrator, installing
   over the existing installation. Do not uninstall or delete ProgramData.
4. Keep the existing hostname and port. Setup validates and upgrades the old
   configuration automatically; no PowerShell edit is needed.
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
