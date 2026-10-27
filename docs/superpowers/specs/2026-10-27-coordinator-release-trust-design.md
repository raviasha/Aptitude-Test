# Coordinator 2.2.4: automatic release-publisher trust

## Intent

Remove the separate PowerShell certificate-installation step on each Windows
Coordinator server. The reported 2.2.3 failure is an untrusted root when Windows
validates the client update installer. The current Coordinator installer does
not install the public release-signing certificate; the client installer does.

## Approaches

- Retain the manual trust script: no installer change, but keeps the per-server
  administrative step the user wants removed.
- Install pinned public publisher trust during elevated Coordinator setup:
  chosen by the user. Uses the existing administrator-approved installation and
  the existing KSAT publisher identity; works for new installs and upgrades.
- Install trust from the running Faculty web UI: not selected. Adds privileged
  certificate-store changes to normal application operation unnecessarily.

## Proposed behavior

The private-lab Coordinator installer bundles the existing public KSAT signing
certificate, never a private key. Before adding trust, validate the bundled
bytes, expected publisher thumbprint/identity, code-signing purpose and validity.
Add only that certificate to the Local Machine Root and TrustedPublisher stores.
Verify the result; leave existing entries alone. If validation or installation
fails, report an actionable setup error rather than silently disabling update
signature checks. Explain the trust setup in the installer/upgrade instructions.

Do this during new installation and upgrade, with the normal administrator/UAC
approval. Do not require a download or a separate command. Retain the shared
publisher trust on uninstall because clients or other KSAT installs may use it.
Keep generic/non-lab release build behavior explicit so other signers are not
silently trusted. Maintainer chooses the precise integration point and testing
interfaces while preserving these constraints.

## Boundaries

Release Coordinator 2.2.4 only. Client and Builder stay at 2.1.6 and their signed
update bundle stays byte-for-byte unchanged. Preserve ProgramData, accounts,
results, question banks, enrolled devices, runtime settings, update verification
keys and each lab's TLS/connection certificates. No live lab deployment, trust
store modification on the development PC, or private-key publication is part of
this work. The installer still must be run once on each server to receive it.

## Acceptance and delivery

Test absent/present trust, invalid certificate rejection, failed import handling,
version transitions and data preservation. Use isolated test stores or mocked
store boundaries instead of changing this PC's machine trust. Compile and sign
the new installer with the existing publisher, inspect its exact payload and
run frozen startup/TLS checks. Publish source, installer, checksums and measured
verification limits to GitHub. A real untrusted lab-server install remains a
field verification step unless an appropriately isolated Windows test machine
is available.

Status: approved by the user on 2026-10-27.
