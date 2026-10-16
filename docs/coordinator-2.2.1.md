# Coordinator 2.2.1 upgrade guide

[Download the installer](../release/coordinator-2.2.1/KSATCoordinatorSetup-2.2.1.exe).
This server-only update gives diagram solutions more room in the classic
Coordinator browser assessment at smaller desktop widths. Existing lab clients
and connection files remain compatible; no client reinstall is required.
The separate installed lab-client interface is unchanged.

## Upgrade the server

Finish active exams and uploads. Sign in as an administrator, click **Stop server**
and confirm. Wait for the Coordinator process to exit before backing up
`C:\ProgramData\KSAT Coordinator`; closing its browser alone does not stop the
server. Run **KSATCoordinatorSetup-2.2.1.exe** over
the existing installation. Do not uninstall or delete ProgramData.
Existing accounts, question banks, results, security identity and network
settings are retained; existing question banks do not need reimporting.

Start the Coordinator and check that the sign-in page shows **Build 2.2.1**.
Refresh the browser if it still displays the earlier version. Try a client
connection, a test and a CSV export before resuming lab exams. A backup is a
recovery precaution; installation does not automatically restore a backup.

## Features retained

The [2.2.0 usage guide](coordinator-2.2.0.md) still applies to book grouping,
balanced random selection across chapters, multiple chapter ZIP imports,
master-ZIP imports and retention of completed result summaries after deleting
tests. Upload limits are unchanged: 50,000,000 bytes per upload and
150,000,000 expanded bytes, 10,000 files and 200 chapters per master archive.
If a textbook master exceeds any of these limits, extract it once and select the
individual chapter ZIPs in the Coordinator. Keep those chapter ZIPs zipped.

## Compatibility and checks

The product, installer and Coordinator interface display **2.2.1**.
The legacy client handshake and public trust metadata remain **2.1.0**.
The upgrade accepts existing 2.2.0 runtime settings and preserves earlier
runtime backups. Unknown runtime versions are still rejected rather than
silently rewritten.

The signed executable and installer passed HTTPS startup and installer-payload
checks. The layout regression passed at 1024, 1600 and 390 pixel viewport
widths. Independent scoped release tests passed, including preservation of
existing data and security files. The broader repository suite is not fully
green: its calibration and source-fixture failures are tracked separately.
An actual lab-machine upgrade remains a deployment check.

[Checksums](../release/coordinator-2.2.1/SHA256SUMS.txt) and
[build verification](../release/coordinator-2.2.1/verification.json) accompany
the download.
