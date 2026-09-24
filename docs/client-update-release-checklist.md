# KSAT 2.1 client-update release checklist

This record covers the verification-only build made on 24 September 2026. Every
artifact named here uses the ephemeral publisher
`CN=KSAT TEST SIGNING IDENTITY - NOT FOR PRODUCTION`. It has no timestamp and
must not be distributed or installed in the lab.

## Automated verification

| Gate | Result |
|---|---|
| Application suite | 636 tests passed; 32 skipped |
| Legacy registration, media, feedback, visual-bank and completed-package suite | 84 tests passed |
| Textbook chapter pipeline suite | 140 tests passed |
| Recurring-decimal JavaScript regression | Passed |
| Windows hostname bootstrap test | Passed |
| Client-update end-to-end and Windows packaging/entrypoint suite | 52 tests passed |
| Frozen application smoke | Coordinator 2.1.0, verified TLS, client `device_setup`, IPv4 loopback only, UAC payload equivalent |
| Artifact inspection | Authenticode checked; PyInstaller and Inno payloads recursively extracted/scanned; embedded update key found in coordinator, client and updater; client-update bundle parsed and signature checked |

The two-client end-to-end test covers pilot gating, health-gated publication,
an offline client reconnecting, mandatory pre-login update policy, active
attempt and sealed-submission deferral, identity/config/state preservation, and
100 concurrent policy reads during submission traffic.

## Test artifact evidence

Build environment: Windows 11 build 26200, Python 3.14.2, PyInstaller 6.22.3,
Inno Setup 6.7.3 and innoextract 1.12-dev (`e561d8c`, Inno 6.7 support).

Signer thumbprint for both installers:
`ADF1625E77B7F7E23A14890FED3A13A0C8FA8066`.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `KSATCoordinator-2.1.0.exe` | 26,581,880 | `a62c4401fb0858433bfc377f880e0fc7ee19e49e86c3b1017481c30612c58e72` |
| `KSATCoordinatorSetup-2.1.0.exe` | 28,238,032 | `eb6d7e1429e044b90472aa980f8afe9197028a55b02874bae0fd53dd4c797735` |
| `KSATClient-2.1.0.exe` | 26,470,992 | `8a57ba6f4e68310f339b64959859ddb5f329f65d41a06d547d8fc0bf9a8418f0` |
| `KSATClientSetup-2.1.0.exe` | 51,746,192 | `102afc161c2e51a4ab8243522d7d8fc37542d34559b83fb1e2c3e374de319166` |
| `KSATClientUpdate-2.1.0-TEST-ONLY.ksat-client-update` | 51,747,208 | `cfe50a7b5071d3c868bd9f111010431c4e523a94877455c075f8638ba9529634` |

The acceptance bundle targets 2.1.0 from a minimum source version of 2.0.0.
Its Ed25519 private key and Authenticode PFX were generated in a temporary
directory and deleted when the build exited. Only the matching public update
key was embedded in the test executables.

## Production release gates

- Rebuild with the institution Authenticode PFX, exact publisher pin, approved
  HTTPS timestamp service, and institution update-signing public key.
- Build client update bundles separately with the offline institution update
  private key.
- Repeat the disposable coordinator plus two-client VM acceptance procedure in
  the implementation plan, including interrupted download/install and rollback.
- Complete physical-lab pilot acceptance before broad deployment.
- Replace this test evidence with production hashes, signer identity,
  timestamp evidence, VM versions, pilot results and the approved change record.

This test build proves the automated release path; it does not establish
production readiness or physical-lab acceptance.
