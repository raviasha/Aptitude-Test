# KSAT release files

## Coordinator 2.2.3, Client 2.1.6 and Lab Package Builder 2.1.6

Signed private-lab pilot builds. Build, signature, frozen startup and payload
checks passed; **a real installed-PC pilot remains required before broad
deployment**. The existing release publisher and signing identity are unchanged.

| Purpose | Download |
| --- | --- |
| Upgrade the faculty server | [KSATCoordinatorSetup-2.2.3.exe](coordinator-2.2.3/KSATCoordinatorSetup-2.2.3.exe) |
| Standalone Coordinator | [KSATCoordinator-2.2.3.exe](coordinator-2.2.3/KSATCoordinator-2.2.3.exe) |
| Install/upgrade a Windows client directly | [KSATClientSetup-2.1.6.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.6/KSATClientSetup-2.1.6.exe) |
| Generate a lab-specific installer containing Client 2.1.6 | [KSATLabPackageBuilder-2.1.6.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.6/KSATLabPackageBuilder-2.1.6.exe) |
| Update existing clients from the server | [KSATClientUpdate-2.1.6.ksat-client-update](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.6/KSATClientUpdate-2.1.6.ksat-client-update) |

The large client installer, builder and managed bundle are distributed as GitHub
Release assets under `client-v2.1.6`.
[Coordinator checksums](coordinator-2.2.3/SHA256SUMS.txt) ·
[Coordinator verification](coordinator-2.2.3/verification.json) ·
[Client/Builder checksums](client-2.1.6/SHA256SUMS.txt) ·
[Client/Builder verification](client-2.1.6/verification.json)

The complete visual fix requires **Coordinator 2.2.3 and Client 2.1.6**.
The server removes matching textbook question-number prefixes from reasoning
text in newly created releases and converts supporting images to the existing
image-stimulus format understood by older clients. Client 2.1.6 hides duplicate
text only after a reviewed complete question/option image loads, restores that
text on failure, and provides supporting-image transcription fallback in tests
and review. Client 2.1.5 remains supported but lacks the full UI fix.

Open **Faculty > Client updates**, upload
the `.ksat-client-update`, select an enrolled Windows pilot and wait for
**healthy** on **2.1.6** before **Publish to all clients**. Idle clients update
before student login when KSAT starts/connects. Active tests and pending uploads
defer managed updates. Devices enroll automatically; no hand-maintained host
list is required. Fully closed clients do not update in the background; open
KSAT on those PCs to participate. Ubuntu is unsupported for this rollout.

The updated quantitative packages are:

- [Six corrected chapters](../question-banks/quantitative-visual-update-20261022/quantitative-visual-updates-6-chapters.zip): chapters 24, 25 and 36–39, adding 30 geometry diagrams and removing duplicate chart/table text from 306 questions while preserving answers, options and solutions.
- [All 39 chapters](../question-banks/quantitative-visual-update-20261022/quantitative-aptitude-master-39-chapters.zip): the six corrected chapters plus 33 unchanged chapters, for a new setup.

Existing installations should import the six revised banks alongside the old
banks using their `[Visual revision 2026-10-22]` names, then select the revised
copies for new tests. Do not force replacement or delete old banks/history.
Existing reasoning ZIPs are unchanged and need no reimport; create new
tests/releases after the server upgrade for number cleanup. Existing releases
are immutable.

Finish active exams and uploads, stop the Coordinator, then install over the
existing installation and retain ProgramData, connection settings and security
identities. Do not uninstall or replace identities. Use the matching Builder
2.1.6 to regenerate lab installers; existing builder-generated EXEs do not
update themselves.

[Coordinator upgrade and bank guide](../docs/coordinator-2.2.3.md) ·
[Client visual update and central rollout guide](../docs/client-2.1.6-visual-update.md)

## Previous releases

## Coordinator 2.2.2

[Download Coordinator 2.2.2](coordinator-2.2.2/KSATCoordinatorSetup-2.2.2.exe)
to import textbook master ZIPs up to 100 MB directly. Individual chapters
remain limited to 50 MB, and the existing expanded-size, file-count and
chapter-count protections are unchanged. Install over the existing
Coordinator and retain ProgramData. Clicking the Coordinator shortcut while
the server is already running now reopens its browser page. No client update
is needed.

[Upgrade guide](../docs/coordinator-2.2.2.md) ·
[Checksums](coordinator-2.2.2/SHA256SUMS.txt) ·
[Build verification](coordinator-2.2.2/verification.json)

## Coordinator 2.2.1

[Download Coordinator 2.2.1](coordinator-2.2.1/KSATCoordinatorSetup-2.2.1.exe)
for improved diagram readability in the classic Coordinator assessment on
smaller desktop screens. The multi-chapter test, ZIP import and completed-result
retention features from 2.2.0 remain available. Install over the existing
Coordinator and retain ProgramData. No client update is needed.

[Upgrade guide](../docs/coordinator-2.2.1.md) ·
[Checksums](coordinator-2.2.1/SHA256SUMS.txt) ·
[Build verification](coordinator-2.2.1/verification.json)

## Coordinator 2.2.0 — 9 October 2026

[Download Coordinator 2.2.0](coordinator-2.2.0/KSATCoordinatorSetup-2.2.0.exe)
for book-grouped multi-chapter tests, direct textbook master-ZIP import, and
completed results retained in CSV after deleting tests. This replaces the
October 7 Coordinator 2.1.0 build. The app and installer display **2.2.0**.
No client update is needed.

[Upgrade and usage guide](../docs/coordinator-2.2.0.md) ·
[Checksums](coordinator-2.2.0/SHA256SUMS.txt) ·
[All 39 quantitative chapters in one ZIP](../question-banks/latest/textbook-downloads/quantitative-aptitude-chapters-01-39.zip)

The following **Client/Builder 2.1.5** and **Coordinator 2.2.2** downloads are retained for reference. Use the 2.1.6/2.2.3 links above for the visual fixes and new managed-update bundle.

## 2.1.5 pilot downloads — upgrade clients with saved pending uploads

| Purpose | Download |
| --- | --- |
| Upgrade an existing client, retaining its server configuration and saved data | [KSATClientSetup-2.1.5.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.5/KSATClientSetup-2.1.5.exe) |
| Create a preconfigured **2.1.5 client** installer for your lab | [KSATLabPackageBuilder-2.1.5.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.5/KSATLabPackageBuilder-2.1.5.exe) |
| Install/update the faculty server | [KSATCoordinatorSetup-2.2.2.exe](coordinator-2.2.2/KSATCoordinatorSetup-2.2.2.exe) |

**Interactive installation no longer stops solely for `pending_submission`.** Finish active tests, then run the new installer normally (not silently). Saved answers, queued uploads, device identity and server settings remain intact. Do not uninstall or delete ProgramData. Reopen KSAT afterward to resume eligible upload retries; this update does not itself resolve network failures or faculty-intervention submissions.

Silent/unattended installations still defer pending uploads. Active tests, damaged state, different-server packages and true downgrades remain blocked. Use Builder 2.1.5 with each lab's URL and two public connection files to regenerate its installer. Old generated EXEs do not update automatically. Coordinator stays running; no new central-update bundle is included.

[Installation instructions](../docs/lab-client-installation.md) · [Verification and pilot limits](../docs/lab-package-builder-acceptance.md) · [Client checksum](KSATClientSetup-2.1.5.sha256) · [Builder checksum](KSATLabPackageBuilder-2.1.5.sha256)

## Previous 2.1.4 pilot downloads — corrected upgrade check

| Purpose | Download |
| --- | --- |
| Upgrade an existing client, retaining its server configuration | [KSATClientSetup-2.1.4.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.4/KSATClientSetup-2.1.4.exe) |
| Create a preconfigured **2.1.4 client** installer for your lab | [KSATLabPackageBuilder-2.1.4.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.4/KSATLabPackageBuilder-2.1.4.exe) |
| Install/update the faculty server (unchanged) | [KSATCoordinatorSetup-2.1.0.exe](KSATCoordinatorSetup-2.1.0.exe) |

**October 7 installer correction:** The 2.1.3 install helper incorrectly targeted 2.1.1, causing false `downgrade_refused` errors. In 2.1.4 the helper, client and builder share one version value. Browser-close lifecycle and hostname recovery remain included.

For each lab, use Builder 2.1.4 with **that lab's server URL and its two public connection files**. Regenerate old lab EXEs; they cannot update themselves. A fresh PC accepts its lab's package; a configured PC is not silently moved to another coordinator. Finish tests/uploads, install over the old client, and do not uninstall, edit the registry version or delete ProgramData. Coordinator remains running; no new central-update bundle is included.

[Installation instructions](../docs/lab-client-installation.md) · [Verification and pilot limits](../docs/lab-package-builder-acceptance.md) · [Client checksum](KSATClientSetup-2.1.4.sha256) · [Builder checksum](KSATLabPackageBuilder-2.1.4.sha256)

## Superseded 2.1.3 pilot downloads — use 2.1.4 above

Retained for history only: these installers contain the stale upgrade target described above. Do not regenerate lab packages with Builder 2.1.3.

| Purpose | Download |
| --- | --- |
| Install/update the faculty server (unchanged) | [KSATCoordinatorSetup-2.1.0.exe](KSATCoordinatorSetup-2.1.0.exe) |
| Upgrade an existing client while retaining its server configuration, or configure a new client manually | [KSATClientSetup-2.1.3.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.3/KSATClientSetup-2.1.3.exe) |
| Create a preconfigured **2.1.3 client** installer for any lab | [KSATLabPackageBuilder-2.1.3.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.3/KSATLabPackageBuilder-2.1.3.exe) |

**October 6 window lifecycle update:** Opening the shortcut starts the client service without a normal-launch administrator prompt. Closing the last KSAT tab signs out and safely stops the service after a 15-second grace period and upload cleanup. Unsynchronized results stay queued for the next launch; exam deadlines are not reset. Uploads/central updates do not run while the client is fully closed. The 2.1.2 hostname recovery fix is retained.

Use the same server URL, CA and public metadata files. Older builders and already-created lab installers do not update themselves: run Builder 2.1.3 to create a new lab EXE. Finish tests/uploads, then install over the old version with administrator approval; do not uninstall or delete ProgramData. No coordinator rebuild or new central-update bundle is included.

[Release and pilot checks](../docs/client-window-lifecycle.md) · [Client checksum](KSATClientSetup-2.1.3.sha256) · [Builder checksum](KSATLabPackageBuilder-2.1.3.sha256)

## Previous 2.1.2 downloads

| Purpose | Download |
| --- | --- |
| Install/update the faculty server | [KSATCoordinatorSetup-2.1.0.exe](KSATCoordinatorSetup-2.1.0.exe) |
| Upgrade an existing client, retaining its server configuration; or set up a new PC manually | [KSATClientSetup-2.1.2.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.2/KSATClientSetup-2.1.2.exe) |
| Create one preconfigured client installer per lab (recommended for new PCs) | [KSATLabPackageBuilder-2.1.2.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.2/KSATLabPackageBuilder-2.1.2.exe) |

Run the builder on the designated packaging PC. Supply only the server URL and two public connection files; tools are detected automatically and optional controls are under Advanced. Distribute its generated lab installer to student PCs, not the builder itself. Large EXEs are GitHub Release assets; their download links and checksums remain here. Pilot on one or two PCs before whole-lab deployment.

**October 5 hostname fix:** Client 2.1.2 falls back to the short Windows hostname only when a configured single-label `.local` name fails DNS lookup, retaining the original TLS identity and trusted CA. No fixed server IP or new connection files are required. The standard installer retains existing configuration on upgrade; new PCs need the URL and public files unless using a generated lab installer. Finish tests/uploads before installation; do not uninstall or delete ProgramData. See the [manual upgrade guide](../docs/client-hostname-recovery.md).

**Matching version numbers:** Builder 2.1.2 contains Client 2.1.2. Use it again with each new lab's URL and public files. Old builders and previously generated installers do not update themselves. The October 1 PowerShell installer handoff fix is included. Coordinator remains 2.1.0; no new central-update bundle is supplied.

## Supporting files

- Current standalone Coordinator: [2.2.3](coordinator-2.2.3/KSATCoordinator-2.2.3.exe). The retained [Client 2.1.0](KSATClient-2.1.0.exe) is older; use the current client release above.
- Historical central-update bundle: [KSATClientUpdate-2.1.0.ksat-client-update](KSATClientUpdate-2.1.0.ksat-client-update). Use the new **2.1.6** bundle linked above for this rollout.
- Private-lab publisher trust: [certificate](KSATLabReleaseSigning.cer) and [trust installation script](Install-KSATLabReleaseTrust.ps1), for the standard 2.1.0 workflow. Generated lab installers handle their bundled public trust themselves.
- [Retained 2.1.0 checksums](SHA256SUMS.txt), [previous 2.1.2 builder checksum](KSATLabPackageBuilder-2.1.2.sha256), and [previous 2.1.2 client checksum](KSATClientSetup-2.1.2.sha256).

## Instructions

- [Standard 2.1.0 lab setup and upgrade](../docs/ksat-2.1-lab-upgrade.md)
- [Create a lab-specific client installer](../docs/lab-package-builder.md)
- [Install the generated client on student PCs](../docs/lab-client-installation.md)
- [Builder verification and remaining pilot checks](../docs/lab-package-builder-acceptance.md)

## Older releases

Previous Windows 1.3.x/2.0.0 installers, Ubuntu 2.0 pilot packages, and their older guides and verification records are preserved in [archive/](archive/README.md). They are historical downloads, not recommended for a new current-version installation.
