# KSAT Lab Package Builder 2.1.5

This separate Windows application creates one signed, preconfigured client installer per lab. It contains the approved **2.1.5 client**, updater and installation guard. The Coordinator stays at **2.1.0**. Python and the repository are not required to run the builder. This pilot allows interactive upgrades with saved pending submissions, preserving their queue. It retains on-demand service start, safe last-tab shutdown and the certificate-preserving hostname fallback. See the [lifecycle pilot checklist](client-window-lifecycle.md).

## One-time setup on the packaging PC

Use the designated trusted Windows packaging PC. The current signing identity and Microsoft SignTool have already been provisioned on that PC. The private key is not distributed with this application.

Required tools:

- [Inno Setup](https://jrsoftware.org/isinfo.php), specifically **6.7.3**, with the appropriate licence for your use.
- Microsoft Windows SDK **SignTool**. Microsoft's [documented setup options](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations#download-and-install-signtool) include the SDK BuildTools NuGet package.
- [innoextract](https://github.com/dscharrer/innoextract) with support for the installed Inno 6.7 compiler. An older stable extractor may not support this format.
- The authorized code-signing identity in Windows **CurrentUser\My** or **LocalMachine\My**, with accessible private key and the approved public trust installed. Provisioning is an IT operation, not a per-lab input. Do not email/upload a PFX, password or private key.

Open `KSATLabPackageBuilder-2.1.5.exe` on this PC using the Windows account that has the signing key. The builder detects the installed tools automatically and repairs saved paths when their old locations no longer exist. You do not need to select the compiler, signer or extractor for each lab. **Advanced** is collapsed by default; use its **Check setup** or tool overrides only for troubleshooting.

The builder version matches its bundled client version: **2.1.5 creates 2.1.5 clients** for any lab. Changing the lab URL/files does not change the software version. Old builders and previously generated installers do not update automatically.

The current private-lab publisher is `CN=KSAT LAB RELEASE SIGNING`, thumbprint `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`. The builder cannot silently switch to a different key. It remembers tool and certificate selections only, not lab connection files. This is a designated-PC workflow, not a portable signing setup: copying the builder to another PC does not copy its signing key. No signing files need to be put on Google Drive.

## Create a lab installer

1. Start the Coordinator in that lab. Use a **unique server hostname** that every client can resolve. Duplicate computer names must be corrected by lab IT; a package cannot repair DNS or firewall settings.
2. Obtain the Coordinator's exported `coordinator-ca.pem` and `coordinator-public.json`. These are **public connection files**, not private keys. Never select the Coordinator's private CA/server/signing key files.
3. Supply only the **Coordinator HTTPS URL**, **Coordinator CA (.pem)** and **Coordinator metadata (.json)**. Selecting metadata fills a blank URL but never overwrites one you edited.
4. Click **Create Client Installer**. The builder automatically checks the signing setup, TLS connection and compatible Coordinator version, then compiles, signs and inspects the installer. It does not enrol a device or create a student account. **Test connection** remains an optional separate check.
5. By default, the lab name comes from the server hostname and each build gets a new folder under `Downloads\KSAT Lab Installers`. The completed screen shows the full output path; click **Open output folder**. To change the name or choose an existing local output folder, expand **Advanced** before building.
6. If this PC cannot reach the lab, explicitly select **Build offline** under **Advanced** before creating the installer. Public-file validation and signing still run. An offline package does not prove the lab clients can resolve/reach the server. Cancellation waits safely for the current bounded operation and prevents publication.
7. Keep the generated `.json` receipt, which contains the installer checksum and builder version. Distribute **only** `KSATClientSetup-<lab-name>-2.1.5.exe` to student PCs in that lab. Existing output files are never overwritten.

The generated EXE includes the public connection profile and publisher certificate. It does not include device identities, student records, databases, cached tests, passwords or private keys. The final installer signature covers its configuration as well as its client binaries.

## Installation and updates

Follow [the student-PC installation guide](lab-client-installation.md). Test one or two disposable/pilot PCs before a whole-lab rollout; see [the acceptance record](lab-package-builder-acceptance.md).

Same-server upgrades retain existing client settings and device data. A different effective URL, CA or protocol key stops installation; this builder does not migrate a client to a different Coordinator. Active tests prevent replacement. Interactive 2.1.5 installation allows pending submissions and preserves their queue; silent/unattended installation continues to defer pending submissions. Normal downgrades are refused; the central updater's protected recovery journal authorizes only its exact cached rollback installer.

After the 2.1.1 bootstrap, subsequent compatible generic signed central updates remain lab-neutral and preserve the lab settings. You do not need to recreate every lab installer for each future update. This tool does not publish a central update or update the Coordinator.

## Troubleshooting

- **Administrator setup operation failed with a package from Builder 1.0.0/1.0.1:** Builder 1.0.2 corrects the installer safety-helper handoff on Windows PowerShell. Recreate the lab installer with the same three connection inputs in a new output folder. Close the failed setup before running the new package on a pilot PC; do not uninstall or delete KSAT data. The client application remains 2.1.1 and the Coordinator remains 2.1.0. Already-created installers are not repaired by downloading a new builder.
- **Public files or URL invalid:** select both exports from the same Coordinator and confirm URL/port and PC clock. Do not weaken certificate validation.
- **Signing setup incomplete:** use the designated PC, correct certificate store and SignTool path; ask IT to restore access to the existing authorized identity.
- **Packaging PC is missing a tool:** the message names the component. Expand **Advanced** and select its installed location, or ask IT to restore it. The server connection files are not the cause of this error. Keep the extraction tool and its DLL in a permanent tools folder, not a temporary download folder.
- **Compilation failed:** use Inno 6.7.3 and check free space and output permissions.
- **Verification failed:** check the compatible innoextract build. No unsigned/unverified output is reported as successful.
- **Connection unverified:** check server hostname uniqueness, DNS, network profile/firewall and clock. Do not substitute a rotating IP address in a hostname-bound profile.
- **Installation blocked:** finish active tests. Pending uploads block silent installation only; use the interactive 2.1.5 installer if the old client cannot upload. For old running clients see the legacy note in the installation guide. Corrupt/legacy state needs IT review, not deletion.
- **Installation appears stuck:** do not manually restart the client service while setup is still running. The safety lock intentionally lasts until setup commits, cancels or exits. Have IT close the stalled installer before retrying.

Private-lab signatures can still cause a first-launch Windows/SmartScreen warning. Installing trust inside the EXE cannot retroactively make its first launch trusted. IT should verify the download channel, signer and checksum. A successful signature is not antivirus clearance; do not disable protection to make a build/install succeed.

Uninstall preserves student data and shared public certificates. The helper records trust it added; it does not remove roots that another KSAT product might need. Failed setup may retain a protected diagnostic staging folder under `C:\ProgramData\KSAT Installer Staging`; do not remove it while setup or its guard is running.
