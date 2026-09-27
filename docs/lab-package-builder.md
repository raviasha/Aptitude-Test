# KSAT Lab Package Builder 1.0.0

This separate Windows application creates one signed, preconfigured client installer per lab. It contains the approved **2.1.1 client**, updater and installation guard. The Coordinator stays at **2.1.0**. Python and the repository are not required to run the builder.

## One-time setup on the packaging PC

Use the designated trusted Windows packaging PC. The current signing identity and Microsoft SignTool have already been provisioned on that PC. The private key is not distributed with this application.

Required tools:

- [Inno Setup](https://jrsoftware.org/isinfo.php), specifically **6.7.3**, with the appropriate licence for your use.
- Microsoft Windows SDK **SignTool**. Microsoft's [documented setup options](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations#download-and-install-signtool) include the SDK BuildTools NuGet package.
- [innoextract](https://github.com/dscharrer/innoextract) with support for the installed Inno 6.7 compiler. An older stable extractor may not support this format.
- The authorized code-signing identity in Windows **CurrentUser\My** or **LocalMachine\My**, with accessible private key and the approved public trust installed. Provisioning is an IT operation, not a per-lab input. Do not email/upload a PFX, password or private key.

Open `KSATLabPackageBuilder-1.0.0.exe`. Under **One-time setup**, confirm the three tool paths and certificate store, then click **Check setup**. The current private-lab publisher is `CN=KSAT LAB RELEASE SIGNING`, thumbprint `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`. The builder cannot silently switch to a different key. It remembers tool and certificate selections only, not lab connection files.

## Create a lab installer

1. Start the Coordinator in that lab. Use a **unique server hostname** that every client can resolve. Duplicate computer names must be corrected by lab IT; a package cannot repair DNS or firewall settings.
2. Obtain the Coordinator's exported `coordinator-ca.pem` and `coordinator-public.json`. These are **public connection files**, not private keys. Never select the Coordinator's private CA/server/signing key files.
3. In the builder enter the **lab name**, **HTTPS URL**, both **public files**, and an existing **output folder**. Prefer a local NTFS folder. Selecting metadata fills a blank URL but never overwrites one you edited.
4. Click **Test connection** when the packaging PC can reach the lab. The check verifies TLS and the compatible Coordinator version; it does not enrol a device or create a student account.
5. If building away from the lab, explicitly select the offline acknowledgement. Public-file validation still runs. An offline package does not prove the lab clients can resolve/reach the server.
6. Click **Validate and Create Installer**. Wait through validation, compilation, signing and inspection. Cancellation waits safely for a bounded tool operation and prevents publication.
7. Keep the generated `.json` receipt, which contains the installer checksum. Distribute **only** `KSATClientSetup-<lab-name>-2.1.1.exe` to student PCs in that lab. Existing output files are never overwritten.

The generated EXE includes the public connection profile and publisher certificate. It does not include device identities, student records, databases, cached tests, passwords or private keys. The final installer signature covers its configuration as well as its client binaries.

## Installation and updates

Follow [the student-PC installation guide](lab-client-installation.md). Test one or two disposable/pilot PCs before a whole-lab rollout; see [the acceptance record](lab-package-builder-acceptance.md).

Same-server upgrades retain existing client settings and device data. A different effective URL, CA or protocol key stops installation; this builder does not migrate a client to a different Coordinator. Active tests and unacknowledged uploads prevent replacement. Normal downgrades are refused; the central updater's protected recovery journal authorizes only its exact cached rollback installer.

After the 2.1.1 bootstrap, subsequent compatible generic signed central updates remain lab-neutral and preserve the lab settings. You do not need to recreate every lab installer for each future update. This tool does not publish a central update or update the Coordinator.

## Troubleshooting

- **Public files or URL invalid:** select both exports from the same Coordinator and confirm URL/port and PC clock. Do not weaken certificate validation.
- **Signing setup incomplete:** use the designated PC, correct certificate store and SignTool path; ask IT to restore access to the existing authorized identity.
- **Compilation failed:** use Inno 6.7.3 and check free space and output permissions.
- **Verification failed:** check the compatible innoextract build. No unsigned/unverified output is reported as successful.
- **Connection unverified:** check server hostname uniqueness, DNS, network profile/firewall and clock. Do not substitute a rotating IP address in a hostname-bound profile.
- **Installation blocked:** finish active tests and pending uploads. For old running clients see the legacy note in the installation guide. Corrupt/legacy state needs IT review, not deletion.
- **Installation appears stuck:** do not manually restart the client service while setup is still running. The safety lock intentionally lasts until setup commits, cancels or exits. Have IT close the stalled installer before retrying.

Private-lab signatures can still cause a first-launch Windows/SmartScreen warning. Installing trust inside the EXE cannot retroactively make its first launch trusted. IT should verify the download channel, signer and checksum. A successful signature is not antivirus clearance; do not disable protection to make a build/install succeed.

Uninstall preserves student data and shared public certificates. The helper records trust it added; it does not remove roots that another KSAT product might need. Failed setup may retain a protected diagnostic staging folder under `C:\ProgramData\KSAT Installer Staging`; do not remove it while setup or its guard is running.
