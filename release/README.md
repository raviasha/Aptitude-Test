# Current KSAT release files

The newest **pilot/prerelease** is **Client 2.1.4 and Lab Package Builder 2.1.4**. Coordinator remains **2.1.0**. Test one lab PC before wider deployment; the real administrator/standard-user installation pilot is still required.

## 2.1.4 pilot downloads — corrected upgrade check

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

- Standalone Coordinator: [2.1.0](KSATCoordinator-2.1.0.exe). The retained [Client 2.1.0](KSATClient-2.1.0.exe) is older; use the installers above for current deployments.
- Existing central-update bundle: [KSATClientUpdate-2.1.0.ksat-client-update](KSATClientUpdate-2.1.0.ksat-client-update). This is the older 2.1.0 bundle, **not** an update for builder-installed 2.1.1 clients.
- Private-lab publisher trust: [certificate](KSATLabReleaseSigning.cer) and [trust installation script](Install-KSATLabReleaseTrust.ps1), for the standard 2.1.0 workflow. Generated lab installers handle their bundled public trust themselves.
- [Retained 2.1.0 checksums](SHA256SUMS.txt), [previous 2.1.2 builder checksum](KSATLabPackageBuilder-2.1.2.sha256), and [previous 2.1.2 client checksum](KSATClientSetup-2.1.2.sha256).

## Instructions

- [Standard 2.1.0 lab setup and upgrade](../docs/ksat-2.1-lab-upgrade.md)
- [Create a lab-specific client installer](../docs/lab-package-builder.md)
- [Install the generated client on student PCs](../docs/lab-client-installation.md)
- [Builder verification and remaining pilot checks](../docs/lab-package-builder-acceptance.md)

## Older releases

Previous Windows 1.3.x/2.0.0 installers, Ubuntu 2.0 pilot packages, and their older guides and verification records are preserved in [archive/](archive/README.md). They are historical downloads, not recommended for a new current-version installation.
