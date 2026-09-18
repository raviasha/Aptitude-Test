# KSAT client-server test builds — Coordinator update 17 September 2026

## Ubuntu graphical installers — 18 September 2026

- [Ubuntu 18.04 amd64](KSATClient-Ubuntu-18.04-amd64.deb)
- [Ubuntu 22.04 amd64](KSATClient-Ubuntu-22.04-amd64.deb)

Package revision `2.0.0+ubuntu2`: install the matching `.deb` through Ubuntu's
graphical package installer, then open **KSAT Client Setup** from Applications.
Choose the server HTTPS address, `coordinator-ca.pem`, and `coordinator-public.json`,
approve the administrator password prompt, and click **Open KSAT**. No terminal
commands are needed for installation or first setup. Standard Ubuntu desktop
dependencies may require internet access. This updates only the Ubuntu clients;
the Windows installers and server-only 1.3.4 installer are unchanged.

Read the [graphical installation guide](../docs/ubuntu-client-installation.md)
and [verification/remaining acceptance checks](../docs/ubuntu-gui-build-verification.md).
The adjacent `.sha256` and `.build-info.json` files identify these pilot packages.

## Coordinator DHCP startup fix — 17 September 2026

The Coordinator installer and standalone executable in this folder now allow
DHCP/adapter IP changes when the configured hostname and existing certificate
remain valid. They no longer require the certificate's issuance-time LAN IPs
to exactly equal the current adapter addresses. Hostname, signature, expiry,
private-key matching, and loopback certificate checks remain enabled.

**Update only the server:** close the Coordinator and run
[KSATCoordinatorSetup-2.0.0.exe](KSATCoordinatorSetup-2.0.0.exe) over the existing
installation. Do not uninstall or delete ProgramData. Windows and Ubuntu client
binaries are unchanged. Clients should use the stable hostname, which must
resolve to the server's current IP. Literal-IP connections are not automatically
updated or newly covered by the certificate.

If ProgramData was already deleted, stop the server, preserve its newly created
data folder separately, and restore the complete original `KSAT Coordinator`
backup before starting this updated version. Do not merge old and new security
files. The executable update cannot reconstruct deleted accounts or keys.

See [DHCP update verification and recovery](coordinator-dhcp-update.md) and
[coordinator-only checksums](COORDINATOR-DHCP-SHA256SUMS.txt). These remain
**test-signed pilot binaries**, not institution-signed production releases.
The older complete-package ZIP linked below does not include this DHCP fix;
use the Coordinator installer directly from this folder.

**TEST-SIGNED — NOT FOR PRODUCTION**

These Lab Client and Faculty Coordinator builds include the exam-integrity
hardening update, the faculty exam-timer correction, and the refreshed faculty
workspace. They are intended for isolated testing and supervised lab
acceptance. Their ephemeral signing certificate is explicitly marked
`KSAT TEST SIGNING IDENTITY - NOT FOR PRODUCTION`; Windows does not trust it as
a production publisher. Institution signing and physical Windows acceptance
are still required before production rollout.

## Downloads

[Download the complete test package](https://github.com/raviasha/Aptitude-Test/releases/tag/v2.0.0-test-20260910-faculty-timer)
for both installers, both standalone executables, the installation and user guides, and
checksums. Question banks are distributed separately.

| Product | Installer | Standalone executable |
| --- | --- | --- |
| Lab Client | [KSATClientSetup-2.0.0.exe](KSATClientSetup-2.0.0.exe) | [KSATClient-2.0.0.exe](KSATClient-2.0.0.exe) |
| Faculty Coordinator | [KSATCoordinatorSetup-2.0.0.exe](KSATCoordinatorSetup-2.0.0.exe) | [KSATCoordinator-2.0.0.exe](KSATCoordinator-2.0.0.exe) |

Two-page quick installation guide: [PDF](KSAT_Quick_Installation_Guide.pdf)
or [Word](KSAT_Quick_Installation_Guide.docx). It covers server selection, checks
from two student PCs, installation, question banks, and a short pilot. It assumes
departments receive the correct files through the supplied Google Drive folder.

Two-page user guide: [PDF](KSAT_Quick_User_Guide.pdf) or
[Word](KSAT_Quick_User_Guide.docx). One page covers faculty operations, including
extensions and results; the other covers students taking and submitting a test.

[Checksums](SHA256SUMS.txt) remain available for maintainers. Compatibility
metadata remains 2.0.0; the date and checksums identify this update. Apply updates between
assessments. The faculty timer correction requires only the Coordinator update
if clients already have the 10 September integrity update. The Client was
rebuilt for a complete distribution; its behavior is unchanged by the timer fix.
Install both products for a new lab or when upgrading from an older client.

## Changes and verification

- Immediate exam gating on observable focus, visibility, or fullscreen loss.
- Durable retries with persistent event IDs, including concurrent browser tabs.
- An independent local-service watchdog that records monitoring interruptions.
- Updated Faculty result and CSV labels.
- Faculty now shows exam time left, a range for different student deadlines,
  and whole-assessment minutes added, separately from the start window.
  Extensions appear immediately after the action and survive page refresh.
- Timer synchronization ignores responses for replaced pages and prevents
  overlapping background requests from restoring an older timer value.
- Refreshed the faculty workspace with clearer navigation, responsive layout,
  searchable question-bank management, and ZIP-only question-bank importing.
- Coordinator upgrades safely adopt a matching legacy KSAT firewall rule when
  its ownership marker is missing, while still rejecting unrelated rules.
- Updated the faculty identity to Prof R Ravi Shankar and improved login
  branding and spacing around the institutional header.
- 545 distributed tests passed before the final polling adjustment; 97 focused
  and legacy checks passed on the final source, including its polling regression.
- Both products passed executable smoke checks, test-signature verification,
  recursive installer payload inspection, and SHA-256 verification. The smoke
  confirmed coordinator TLS, a loopback-only client, and equivalent application
  payloads between the elevated Coordinator and its automated smoke probe.

The unchanged Windows client was built from source revision
`a3220fa0bd3542c5484fef5fdc6827d4215af945`. The Coordinator now includes the
17 September DHCP fix described above; see its separate verification notes.

Browser-only checks cannot guarantee every Windows virtual-desktop switch.
See [coverage, remaining gaps, and physical acceptance steps](../docs/exam-integrity-hardening.md)
for the limits and native-monitor/kiosk recommendation. See
[the build and deployment guide](../WINDOWS_EXE_BUILD.md) for production signing.

Publishing these test files does not install or deploy them to any lab computer.
