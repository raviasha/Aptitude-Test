# Coordinator 2.2.2 upgrade guide

[Download the installer](../release/coordinator-2.2.2/KSATCoordinatorSetup-2.2.2.exe).
This server-only update allows direct import of textbook master ZIPs up to
100,000,000 bytes. The 87.8 MB *A New Approach to Reasoning* master can
therefore be selected and imported without extracting it first.

The installed **KSAT Faculty Coordinator** shortcut is also safe to click
again: it starts the server when stopped, and reopens the Coordinator page
when the server is already running. Closing only the browser does not stop the
server or disconnect student clients.

Individual chapter ZIPs remain limited to 50,000,000 bytes. The existing
safety limits also remain in force: 150,000,000 expanded bytes, 10,000 files
and 200 chapters per master, along with unsafe-path, duplicate-file,
encryption and symbolic-link rejection.

## Upgrade the server

Finish active exams and uploads. Sign in as an administrator, click **Stop
server** and confirm, then wait for the Coordinator process to exit. Run
**KSATCoordinatorSetup-2.2.2.exe** over the existing installation. Do not
uninstall and do not delete `C:\ProgramData\KSAT Coordinator`.

Existing accounts, question banks, results, security identity, network
settings and clients are retained. No client update or question-bank reimport
is required. After installation, start the Coordinator and confirm that the
sign-in page shows **Build 2.2.2**.

If the browser is closed accidentally, click the same Coordinator shortcut to
open the page again. Do not start or reinstall a second server.

## Import the reasoning master

Open **Question banks**, select
`new-reasoning-chapters-01-41.zip`, and import it. The Coordinator validates
the complete outer and nested archive before importing the 41 chapter banks.
Assign the imported banks to **A New Approach to Reasoning** if needed.
