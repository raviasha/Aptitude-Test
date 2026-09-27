# Current KSAT release files

Use this folder for the **2.1.0 Coordinator/standard Client** and the **Lab Package Builder 1.0.0** (which creates preconfigured **2.1.1 clients**).

## Downloads

| Purpose | Download |
| --- | --- |
| Install/update the faculty server | [KSATCoordinatorSetup-2.1.0.exe](KSATCoordinatorSetup-2.1.0.exe) |
| Install the standard client manually | [KSATClientSetup-2.1.0.exe](KSATClientSetup-2.1.0.exe) |
| Create one preconfigured client installer per lab | [KSATLabPackageBuilder-1.0.0.exe](https://github.com/raviasha/Aptitude-Test/releases/download/lab-package-builder-v1.0.0/KSATLabPackageBuilder-1.0.0.exe) |

Run the builder on the designated packaging PC. Distribute its generated lab installer to student PCs, not the builder itself. The builder EXE is a GitHub Release asset because of its size; its download link and [checksum](KSATLabPackageBuilder-1.0.0.sha256) remain here. Pilot on one or two PCs before whole-lab deployment.

## Supporting files

- Standalone executables: [Coordinator 2.1.0](KSATCoordinator-2.1.0.exe) and [Client 2.1.0](KSATClient-2.1.0.exe). Normally use the installers above.
- Existing central-update bundle: [KSATClientUpdate-2.1.0.ksat-client-update](KSATClientUpdate-2.1.0.ksat-client-update). This is the older 2.1.0 bundle, **not** an update for builder-installed 2.1.1 clients.
- Private-lab publisher trust: [certificate](KSATLabReleaseSigning.cer) and [trust installation script](Install-KSATLabReleaseTrust.ps1), for the standard 2.1.0 workflow. Generated lab installers handle their bundled public trust themselves.
- [2.1.0 checksums](SHA256SUMS.txt) and [builder checksum](KSATLabPackageBuilder-1.0.0.sha256).

## Instructions

- [Standard 2.1.0 lab setup and upgrade](../docs/ksat-2.1-lab-upgrade.md)
- [Create a lab-specific client installer](../docs/lab-package-builder.md)
- [Install the generated client on student PCs](../docs/lab-client-installation.md)
- [Builder verification and remaining pilot checks](../docs/lab-package-builder-acceptance.md)

## Older releases

Previous Windows 1.3.x/2.0.0 installers, Ubuntu 2.0 pilot packages, and their older guides and verification records are preserved in [archive/](archive/README.md). They are historical downloads, not recommended for a new current-version installation.
