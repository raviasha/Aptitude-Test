# KSAT client-server test builds — 11 September 2026

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

Built from source revision `a3220fa0bd3542c5484fef5fdc6827d4215af945`.

Browser-only checks cannot guarantee every Windows virtual-desktop switch.
See [coverage, remaining gaps, and physical acceptance steps](../docs/exam-integrity-hardening.md)
for the limits and native-monitor/kiosk recommendation. See
[the build and deployment guide](../WINDOWS_EXE_BUILD.md) for production signing.

Publishing these test files does not install or deploy them to any lab computer.
