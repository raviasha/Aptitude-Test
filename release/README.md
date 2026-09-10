# KSAT client-server test builds — 10 September 2026

**TEST-SIGNED — NOT FOR PRODUCTION**

These Lab Client and Faculty Coordinator builds include the exam-integrity
hardening update. They are intended for isolated testing and supervised lab
acceptance. Their ephemeral signing certificate is explicitly marked
`KSAT TEST SIGNING IDENTITY - NOT FOR PRODUCTION`; Windows does not trust it as
a production publisher. Institution signing and physical Windows acceptance
are still required before production rollout.

## Downloads

| Product | Installer | Standalone executable |
| --- | --- | --- |
| Lab Client | [KSATClientSetup-2.0.0.exe](KSATClientSetup-2.0.0.exe) | [KSATClient-2.0.0.exe](KSATClient-2.0.0.exe) |
| Faculty Coordinator | [KSATCoordinatorSetup-2.0.0.exe](KSATCoordinatorSetup-2.0.0.exe) | [KSATCoordinator-2.0.0.exe](KSATCoordinator-2.0.0.exe) |

Verify downloads against [SHA256SUMS.txt](SHA256SUMS.txt). Compatibility metadata
remains 2.0.0; the date and checksums identify this update. Update both products
between assessments.

## Changes and verification

- Immediate exam gating on observable focus, visibility, or fullscreen loss.
- Durable retries with persistent event IDs, including concurrent browser tabs.
- An independent local-service watchdog that records monitoring interruptions.
- Updated Faculty result and CSV labels.
- 251 focused tests passed. Both products passed executable smoke tests,
  signature checks for the test identity, recursive payload inspection, and
  checksum verification.

Browser-only checks cannot guarantee every Windows virtual-desktop switch.
See [coverage, remaining gaps, and physical acceptance steps](../docs/exam-integrity-hardening.md)
for the limits and native-monitor/kiosk recommendation. See
[the build and deployment guide](../WINDOWS_EXE_BUILD.md) for production signing.

Publishing these test files does not install or deploy them to any lab computer.
