# Current KSAT release files

Use these downloads for **Coordinator 2.1.0**, the corrected **standard Client 2.1.1 installer**, and **Lab Package Builder 1.0.2** (which creates preconfigured **2.1.1 clients**).

## Downloads

| Purpose | Download |
| --- | --- |
| Install/update the faculty server | [KSATCoordinatorSetup-2.1.0.exe](KSATCoordinatorSetup-2.1.0.exe) |
| Install the standard client manually (URL and public files required for a new PC) | [KSATClientSetup-2.1.1.exe](https://github.com/raviasha/Aptitude-Test/releases/download/lab-package-builder-v1.0.2/KSATClientSetup-2.1.1.exe) |
| Create one preconfigured client installer per lab (recommended) | [KSATLabPackageBuilder-1.0.2.exe](https://github.com/raviasha/Aptitude-Test/releases/download/lab-package-builder-v1.0.2/KSATLabPackageBuilder-1.0.2.exe) |

Run the builder on the designated packaging PC. Supply only the server URL and two public connection files; tools are detected automatically and optional controls are under Advanced. Distribute its generated lab installer to student PCs, not the builder itself. Large EXEs are GitHub Release assets; their download links and checksums remain here. Pilot on one or two PCs before whole-lab deployment.

**October 1 installer fix:** Builder 1.0.2 fixes the Windows PowerShell finalization error reported as "The administrator setup operation failed." Recreate old lab packages with this builder; downloading it does not repair an existing lab EXE. Close the failed installer before retrying. Do not uninstall or delete client data. Coordinator and client runtime binaries are unchanged by this installer fix.

## Supporting files

- Standalone Coordinator: [2.1.0](KSATCoordinator-2.1.0.exe). The retained [Client 2.1.0](KSATClient-2.1.0.exe) is older; use the installers above for current deployments.
- Existing central-update bundle: [KSATClientUpdate-2.1.0.ksat-client-update](KSATClientUpdate-2.1.0.ksat-client-update). This is the older 2.1.0 bundle, **not** an update for builder-installed 2.1.1 clients.
- Private-lab publisher trust: [certificate](KSATLabReleaseSigning.cer) and [trust installation script](Install-KSATLabReleaseTrust.ps1), for the standard 2.1.0 workflow. Generated lab installers handle their bundled public trust themselves.
- [Retained 2.1.0 checksums](SHA256SUMS.txt), [current builder checksum](KSATLabPackageBuilder-1.0.2.sha256), and [corrected standard client installer checksum](KSATClientSetup-2.1.1.sha256).

## Instructions

- [Standard 2.1.0 lab setup and upgrade](../docs/ksat-2.1-lab-upgrade.md)
- [Create a lab-specific client installer](../docs/lab-package-builder.md)
- [Install the generated client on student PCs](../docs/lab-client-installation.md)
- [Builder verification and remaining pilot checks](../docs/lab-package-builder-acceptance.md)

## Older releases

Previous Windows 1.3.x/2.0.0 installers, Ubuntu 2.0 pilot packages, and their older guides and verification records are preserved in [archive/](archive/README.md). They are historical downloads, not recommended for a new current-version installation.
