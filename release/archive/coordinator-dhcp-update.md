# Coordinator DHCP startup fix — 17 September 2026

Base source: `bb3f0f70733d8438d0187d694ed5a43faee74493`, plus the TLS validation fix and tests shipped in the same commit as this document. Compatibility version remains 2.0.0; identify this update by its date and `COORDINATOR-DHCP-SHA256SUMS.txt`.

## Change

Previously, restarting with a changed DHCP address or a changed adapter list caused `Coordinator server certificate does not match current LAN addresses`. The coordinator now accepts its existing valid hostname certificate despite LAN IP changes. It does not regenerate any keys or certificates. Hostname, signature, certificate lifetime, key matching and loopback coverage remain checked. TLS clients still verify their actual URL hostname or IP normally.

Only the Windows Coordinator executable and installer were rebuilt. Windows/Ubuntu client binaries, student data, and network configuration are unchanged. The client hostname must resolve correctly to the new IP; this fix does not configure DNS. If clients use a literal IP, use the established hostname configuration procedure or explicitly renew the server certificate to cover the intended address.

## Install and recover

1. Finish active assessments and close the Coordinator.
2. Preserve a complete protected backup of `C:\ProgramData\KSAT Coordinator`.
3. Run the new `KSATCoordinatorSetup-2.0.0.exe` over the existing installation. Do not uninstall or delete data.
4. Start the Coordinator and test an existing student login from a client using the hostname.

If the original ProgramData was deleted, preserve the newly generated folder separately, then restore the **complete original backup**, including secrets, database and assessment files, while the Coordinator is stopped. Do not merge identities. Restoring the original identity normally restores existing client trust. Ordinary DHCP changes no longer require manual certificate renewal; expired certificates or deliberate hostname changes still require the existing renewal procedure. Keep any new records separately: restoring an older backup does not merge them.

## Verification

The regression test first reproduced the original error on all four changed-address cases (IPv4 change, no LAN addresses, added/removed adapter address, and IPv6 change). With the fix, startup accepts these cases and the complete security/public-file snapshot stays unchanged. Existing hostname-change, expired-certificate and corrupt/missing-key rejection tests still pass.

32 TLS/Windows-entrypoint tests and 49 packaging/client-coordinator/schema tests passed. A coordinator-only build driver exercises the frozen application over verified HTTPS twice using a certificate seeded with an old LAN address, checks unchanged security-file hashes, compares the elevated executable's application payload to the non-elevated smoke image, verifies test signatures, and extracts the installer to compare its embedded executable. Its machine-readable result is in `coordinator-dhcp-verification.json`.

Both console and actual PyInstaller windowless smoke builds passed two verified HTTPS startups each with unchanged security files. The smoke images differ from the released executable only in their elevation manifest and executable wrapper; the application payloads are compared. An initial smoke attempt timed out; the final clean run passed all four startups. A separate read-only review found no important issues. No live lab installation was modified during testing. Physical lab restart/DHCP acceptance remains required. Test signing is retained from the existing release process and is not trusted production publisher signing.
