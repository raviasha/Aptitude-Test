# KSAT 2.1 private-lab release record

Release built 24 September 2026 for the controlled KSAT lab. The persistent
release publisher is `CN=KSAT LAB RELEASE SIGNING`, thumbprint
`13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`, valid through 23 September 2031.
The private PFX, password, and update-signing key remain outside the repository
under administrator/SYSTEM-only ACLs. Only the public certificate is shipped.

## Verification

| Gate | Result |
|---|---|
| Complete application suite | 641 tests passed; 32 skipped |
| Focused signing, packaging, update-protocol and updater suite | 50 tests passed |
| Windows hostname bootstrap test | Passed |
| Frozen application smoke | Coordinator 2.1.0, verified TLS, client `device_setup`, IPv4 loopback only, UAC payload equivalent |
| Artifact inspection | Exact signer pinned; PyInstaller and Inno payloads recursively extracted; embedded update key verified; managed-update signature parsed and checked |
| Release checksums | Recomputed after successful inspection |

Because the certificate is private and self-signed, Windows reports the files
as untrusted until `KSATLabReleaseSigning.cer` is installed in Local Machine
Root and Trusted Publishers. The trust/bootstrap scripts require the exact
publisher and thumbprint. After trust is installed, normal Authenticode
validation succeeds without weakening the client updater's checks.

## Release artifacts

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `Install-KSATLabReleaseTrust.ps1` | 1,074 | `948fae4b9e1b49988124994b557443c89a37982a1a6fb29fcaad3517dbecc43a` |
| `KSATLabReleaseSigning.cer` | 1,037 | `82a458843d5f028fee881e8b46ca0db5dbaa69a4a77f2d5b18bc94818c392096` |
| `KSATCoordinator-2.1.0.exe` | 26,581,632 | `628c565cac0c4469e001427615927cb6c3940e6246e596c8f31bee5d5c7f07f0` |
| `KSATCoordinatorSetup-2.1.0.exe` | 28,238,128 | `d217254f11fef7469917704ce7074b8b6c810e0373b58118004c744ab088854f` |
| `KSATClient-2.1.0.exe` | 26,472,208 | `ec197f85e95c9169413e3922491452791527ba8ee22cf46b14ab9e8b91437783` |
| `KSATClientSetup-2.1.0.exe` | 51,746,968 | `1b9d3cc9fcae7abead029beed4f2dbe31f57d7f70e9d13dc6bab7406f4d10c86` |
| `KSATClientUpdate-2.1.0.ksat-client-update` | 51,747,947 | `6a1341031bfdb0e79320a54de6aa2b6606bfcbe7b957196a195701f45a97a6ec` |

The managed update targets 2.1.0 and accepts installed source versions from
2.0.0. Test-only 2.1 artifacts have been removed. Physical lab acceptance is
recorded during the one-client pilot before publishing the update to the
remaining clients.
