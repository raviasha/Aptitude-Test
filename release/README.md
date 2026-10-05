# Current KSAT release files

Use these downloads for **Coordinator 2.1.0**, **standard Client 2.1.2**, and **Lab Package Builder 2.1.2** (which creates preconfigured **2.1.2 clients** for any lab).

## Downloads

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
- [Retained 2.1.0 checksums](SHA256SUMS.txt), [current builder checksum](KSATLabPackageBuilder-2.1.2.sha256), and [current standard client installer checksum](KSATClientSetup-2.1.2.sha256).

## Instructions

- [Standard 2.1.0 lab setup and upgrade](../docs/ksat-2.1-lab-upgrade.md)
- [Create a lab-specific client installer](../docs/lab-package-builder.md)
- [Install the generated client on student PCs](../docs/lab-client-installation.md)
- [Builder verification and remaining pilot checks](../docs/lab-package-builder-acceptance.md)

## Older releases

Previous Windows 1.3.x/2.0.0 installers, Ubuntu 2.0 pilot packages, and their older guides and verification records are preserved in [archive/](archive/README.md). They are historical downloads, not recommended for a new current-version installation.
