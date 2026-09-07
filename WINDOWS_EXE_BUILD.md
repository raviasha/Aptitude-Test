# KSAT 2.0 Windows build and deployment guide

KSAT 2.0 has two products. Install **KSAT Faculty Coordinator** on the faculty/server computer and **KSAT Lab Client** on each lab computer. The client always serves its UI on `127.0.0.1:8010`; only coordinator API traffic crosses the private lab network.

## Build prerequisites

- 64-bit Windows 10/11 or Windows Server
- Python 3.10 or newer with `pip` and `venv`
- Node.js for the JavaScript syntax checks in the canonical test suite
- Inno Setup 6.7.3 (`ISCC.exe`); the release inspection gate requires an extractable Inno 6.x payload
- `innoextract.exe` with Inno 6.7 support (set `INNOEXTRACT_EXE` when it is not on `PATH`)
- An institution-controlled Authenticode PFX with code-signing usage, its password, the exact certificate subject to pin as publisher, and an approved HTTPS timestamp service

From an ordinary Command Prompt in the repository root, configure the signing
identity and build tools, then run the fail-closed build. Keep the PFX and
password out of the repository and build logs. The publisher value must exactly
match the PFX certificate subject:

```bat
set "ISCC_EXE=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
set "INNOEXTRACT_EXE=C:\BuildTools\innoextract.exe"
set "KSAT_SIGNING_PFX=D:\Protected\institution-code-signing.pfx"
set "KSAT_SIGNING_PFX_PASSWORD=<supply through the protected build environment>"
set "KSAT_SIGNING_PUBLISHER=CN=Example Institution, O=Example Institution, C=IN"
set "KSAT_SIGNING_TIMESTAMP_URL=https://timestamp.example.edu"
build-windows.bat
```

`build-windows.bat` refuses to begin without all four signing inputs.
`KSAT_BUILD_PYTHON` may point to an existing build-environment Python. The
script installs only `requirements.txt` plus PyInstaller, cleans only the
explicit `build\windows`/`dist` outputs, builds isolated one-file images, signs
and verifies both inner executables before publishing or embedding them, runs
disposable TLS/loopback smoke tests, compiles and signs both installers,
recursively decompresses and scans PyInstaller and Inno payloads, verifies the
pinned publisher, certificate thumbprint, trusted signature and timestamp,
compares embedded executable bytes, and only then writes hashes. Missing,
untrusted, wrongly published, untimestamped, or invalid signatures stop the
release. The elevated coordinator image cannot be launched by a non-elevated
automated process; the build therefore also launches a non-UAC image and
requires its complete decompressed application-payload manifest to match the
shipped UAC image exactly.

The release directory contains:

```text
release\KSATCoordinator-2.0.0.exe
release\KSATClient-2.0.0.exe
release\KSATCoordinatorSetup-2.0.0.exe
release\KSATClientSetup-2.0.0.exe
release\SHA256SUMS.txt
```

Verify a delivered file before use:

```powershell
Get-FileHash .\release\KSATCoordinatorSetup-2.0.0.exe -Algorithm SHA256
Get-Content .\release\SHA256SUMS.txt
Get-AuthenticodeSignature .\release\KSATCoordinatorSetup-2.0.0.exe |
  Format-List Status,StatusMessage,SignerCertificate,TimeStamperCertificate
```

For source/test verification only, set `KSAT_RELEASE_TEST_SIGNING=1` and invoke
`python scripts\windows_release.py all --test-signing` with the normal build-tool
arguments. This creates an ephemeral self-signed test identity, exercises the
same sign/verify/order gates, and deletes the identity when the command exits.
Its publisher is visibly marked `NOT FOR PRODUCTION`, it has no timestamp, and
its artifacts must never be distributed. A production release remains blocked
until the institution supplies the four protected signing inputs above.

## Initial coordinator setup

Task 13 is the authorization gate for physically running installers, changing firewall rules, or installing a CA. During an approved deployment, run `KSATCoordinatorSetup-2.0.0.exe` as Administrator. Choose a stable DNS name that every lab computer can resolve and keep the default HTTPS port 8443 unless IT has reserved another private-network port.

The installer writes only the program under Program Files. Runtime data is retained under:

```text
C:\ProgramData\KSAT Coordinator
```

The first launch creates private protocol keys, the pack key, browser-session secret, client-session signing secret, a private local CA, and its server key under the administrator/SYSTEM-only `secrets` directory. An older enrollment value may remain for upgrade compatibility, but current clients neither request nor use it. Never copy or email the secrets directory. Give lab IT only these read-only public files:

```text
C:\ProgramData\KSAT Coordinator\public\coordinator-ca.pem
C:\ProgramData\KSAT Coordinator\public\coordinator-public.json
```

The installer creates one private-profile inbound rule for the selected TCP port and atomically records its exact rule/program/port/direction/action/protocol/profile ownership. Upgrades validate the complete canonical persisted coordinator configuration and keep the firewall on its actual port. Before update or uninstall, the installer requires one live rule to match every recorded attribute exactly; it refuses to alter a missing, duplicated, or modified rule. The faculty UI opens locally at `https://127.0.0.1:<port>`; student machines never browse the coordinator UI directly.

Silent first install example:

```bat
KSATCoordinatorSetup-2.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /HOSTNAME=ksat-server.example.edu /PORT=8443
```

## Initial client setup

During an approved Task 13 rollout, run `KSATClientSetup-2.0.0.exe` as Administrator on each lab computer. Supply:

- the exact coordinator URL recorded in `coordinator-public.json`;
- `coordinator-ca.pem`;
- `coordinator-public.json`.

The installer validates the CA hash, CA-to-signing-key binding, signing-key fingerprint, and HTTPS URL before atomically creating:

```text
C:\ProgramData\KSAT Client\client-config.json
```

The immutable CA/signing-key trust and separate `coordinator-url.json` remain in
the administrator-write/user-read root. The installer creates the
`KSATLabClientAuthority` automatic LocalSystem service. Identity,
attempts/outbox, authenticated state anchors, and cached packs are kept in
`identity`, `state`, and `packs` directories writable only by Administrators,
SYSTEM, and that service SID; ordinary lab accounts receive no direct write
access. Desktop/start-menu shortcuts only open the constrained loopback UI,
whose service owns signing, runtime, persistence, and outbox operations. The
installer opens no inbound firewall port. On every install or repair it
independently verifies the saved configuration and machine-root CA. It records
the exact thumbprint only when it actually adds a certificate, so uninstall
removes neither a pre-existing CA nor a later replacement that it did not add.
On first launch the client automatically registers its protected device identity
with the coordinator using the Windows computer name. Students do not enter a
computer label or enrollment code.

Silent first install example (quote all paths and account names):

```bat
KSATClientSetup-2.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /COORDINATORURL=https://ksat-server.example.edu:8443 /CAFILE="D:\KSAT\coordinator-ca.pem" /METADATAFILE="D:\KSAT\coordinator-public.json"
```

An upgrade with an existing valid configuration reuses it and reapplies the
service-owned data-directory ACLs.

## Change the saved coordinator URL

The student-facing client cannot change its machine configuration and returns
`administrator_required` before it stops or swaps any service. From an
**Administrator** Command Prompt, stop the authority service, update the URL,
then start it again:

```bat
sc.exe stop KSATLabClientAuthority
"C:\Program Files\KSAT Client\KSATClient.exe" --update-config --base-url https://ksat-new.example.edu:8443
sc.exe start KSATLabClientAuthority
```

The administrator workflow acquires the same OS lifecycle lock held by the running client, so it first proves the client is stopped. It then refuses to run while an attempt or submission is pending, preserves the CA/signing-key trust and device identity, performs a real TLS `/api/build` probe even before enrollment, and performs a signed catalog probe after enrollment before atomically saving only the URL. Any post-publication failure restores and verifies the exact prior URL. The new URL is reused after restart; rebuilding the executable is not required. The hostname must already be covered by the coordinator certificate and resolve on the lab network.

## Certificate renewal

Back up the coordinator first, stop it, and run the elevated installed executable only after confirming the hostname/port are unchanged:

```bat
"C:\Program Files\KSAT Coordinator\KSATCoordinator.exe" --renew-certificate --confirm-renewal
```

Renewal first acquires the same OS lifecycle lock held by the coordinator and therefore refuses while it is running. It preserves the protocol-signing key, pack key, legacy compatibility data, and local CA; it replaces only the server key/certificate and refreshes the public export. If IT deliberately replaces the CA, redistribute and validate the new public bundle on every client before service resumes. Trust rotation is never automatic.

## Backup, upgrade, rollback, and uninstall

Before an upgrade, stop the coordinator and copy the entire `C:\ProgramData\KSAT Coordinator` directory to protected offline storage. For a pre-2.0 installation, also retain `C:\ProgramData\Aptitude Lab` and run the documented Task 11 compatibility upgrade against a copy before moving approved data into the 2.0 coordinator root. Do not copy only `aptitude.db`: releases, question assets, and keys are part of the backup.

Running a newer installer with the same product AppId replaces program files but preserves all ProgramData. Client upgrades likewise preserve config, identity, attempts/outbox, and packs. Uninstall removes program files and only installer-owned firewall/CA entries; it deliberately retains ProgramData.

For rollback, stop the app, preserve the failed data tree for diagnosis, restore the complete matching backup, install the previously approved executable version, and validate SQLite integrity and the public trust export before reopening the lab. Never mix a database backup with different secrets or release artifacts.

## Troubleshooting

- **Client says configuration is invalid:** confirm the URL exactly matches `coordinator-public.json`, both public files came from the same coordinator, and neither file was edited.
- **Hostname/certificate error:** confirm DNS resolves to the coordinator and the URL hostname is present in the certificate SAN; do not bypass TLS verification.
- **Coordinator will not start:** check that no other coordinator owns the process lock, port 8443 is free, and the private files are complete. Missing/corrupt private material is intentionally not regenerated.
- **Client cannot connect:** check private-profile firewall scope, DNS, port reachability, and system time. The client itself must still listen only on `127.0.0.1:8010`.
- **Saved URL cannot be changed:** finish or resolve any active/sealed-pending attempt first; the guard is intentional.
- **Upgrade concern:** restore/test a complete backup on a spare machine. Do not delete ProgramData or manually recreate keys.

Build and smoke tests compile/inspect only. They do not execute `certutil`, create firewall rules, run installers, or alter the development host trust store.

The exact installation, enrollment, monitoring, outage, backup/restore, upgrade,
30/100-client load-gate, and diagnostic procedures are in the
[distributed-assessment operations runbook](docs/distributed-assessment-operations.md).
