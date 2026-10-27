# Client 2.1.7 and Coordinator 2.2.6

Client 2.1.7 includes an administrator recovery window for obsolete practice
submissions and preserves completed submissions during subsequent managed
upgrades. Coordinator 2.2.6 improves pilot identification and contact reporting.
Lab Package Builder 2.1.7 generates installers with the same client and recovery
tool. The earlier question-image improvements are retained.

## Upgrade the affected PCs

1. Finish any active test. Run **KSATClientSetup-2.1.7.exe** on the affected PC
   and approve the administrator prompt. Install over the existing installation.
   Keep its ProgramData folder and existing Coordinator connection files.
   Completed queued submissions are preserved; they do not require successful
   upload before this interactive installation.
2. From Start, open **KSAT > KSAT Client Recovery** and approve administrator
   access. Click **Refresh diagnosis**.
3. If it lists `attempt_identity_mismatch`, select only the obsolete practice
   attempts faculty has approved for removal. Check the practice confirmation
   box, then click **Archive selected practice attempts** and confirm the list.
4. The tool verifies a protected archive before removing those attempts from
   the live queue. Reopen KSAT. The archive location is displayed on completion.

The archive is stored under `C:\ProgramData\KSAT Client\recovery`. It contains
protected local state and must remain restricted to administrators. Do not copy
an archive over the live database or reset the integrity anchor. Archived
practice attempts do not upload or become graded results. Unselected attempts,
device enrollment and existing Coordinator results remain intact.

For other causes of “Faculty assistance required”, the tool gives a diagnosis.
Time/trust errors, invalid configuration, inactive enrollment and damaged state
are not automatically cleared. Active tests prevent recovery and replacement.

## Why this release needs a one-time interactive installation

Client 2.1.5 blocks its update check when an upload is pending. The legacy updater
also lacks a release/attempt-bound completion record. A server update cannot
change code already running on that PC. Therefore **2.1.7 is an interactive
bootstrap release, with no `.ksat-client-update` bundle**. Do not upload the EXE
to Faculty > Client updates or rename it as an update bundle.

After installing 2.1.7, future compatible managed bundles will require source
version 2.1.7 or later. Those updates preserve queued work, defer active tests,
retry policy checks while the application runs, and report verified completion.
There is no need to update an already installed 2.1.7 client to itself. Existing
2.1.6 rollout records are not rewritten to claim success after a manual upgrade.

## Coordinator and Builder

Install **KSATCoordinatorSetup-2.2.6.exe** over the existing server installation
after stopping the Coordinator. Keep `C:\ProgramData\KSAT Coordinator` intact.
The new UI identifies the selected pilot by its device label and ID, and reports
Unknown when no contact evidence exists instead of incorrectly assuming Offline.
Authenticated contact updates its last-seen time. An online PC is not proof of a
completed update; completion still requires a verified client report.

Use **KSATLabPackageBuilder-2.1.7.exe** with that lab's existing public connection
files to make installers for new PCs. Previously generated installers continue
to contain their original client version.

Automated tests and package inspection do not replace a physical lab pilot.
Install on one affected PC, confirm its diagnosis and archive result, then check
login, a practice test, submission and review before broad deployment.
