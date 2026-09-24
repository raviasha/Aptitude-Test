# KSAT 2.1 lab upgrade — sequential steps

Download or clone the repository on the coordinator computer, then open
PowerShell in the repository root. The binaries and certificate are under
`release`; the deployment script is under `scripts`.

## 1. Verify the downloaded files

Open PowerShell in that folder and run:

```powershell
Get-Content .\release\SHA256SUMS.txt
Get-FileHash .\release\KSATLabReleaseSigning.cer -Algorithm SHA256
Get-FileHash .\release\KSATCoordinatorSetup-2.1.0.exe -Algorithm SHA256
Get-FileHash .\release\KSATClientSetup-2.1.0.exe -Algorithm SHA256
Get-FileHash .\release\KSATClientUpdate-2.1.0.ksat-client-update -Algorithm SHA256
```

The values must match `SHA256SUMS.txt`. The certificate SHA-256 is
`82a458843d5f028fee881e8b46ca0db5dbaa69a4a77f2d5b18bc94818c392096`.

## 2. Back up and update the coordinator

Close KSAT Coordinator and copy the complete
`C:\ProgramData\KSAT Coordinator` folder to protected backup storage. Then open
PowerShell **as Administrator** and run:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\release\Install-KSATLabReleaseTrust.ps1 -Certificate .\release\KSATLabReleaseSigning.cer -ExpectedThumbprint 13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908
.\release\KSATCoordinatorSetup-2.1.0.exe
```

Install over the existing coordinator. Do not uninstall it or remove
ProgramData. Launch it and confirm the Faculty page opens and existing data is
present.

## 3. Prepare the client list

Create `lab-hosts.txt` with one Windows computer name per line. Enable Windows
Remote Management on the lab network if it is not already enabled. Use the same
administrator account on the target clients when the script prompts.

## 4. Dry-run the client bootstrap

From the repository root, in PowerShell:

```powershell
pwsh -NoProfile -File .\scripts\bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\release\KSATClientSetup-2.1.0.exe -TrustCertificate .\release\KSATLabReleaseSigning.cer -DryRun
```

Review `bootstrap-results.json`. Powered-off clients appear as `Offline`; this
does not prevent an available client from being checked.

## 5. Update one pilot client

Put only one hostname in `pilot-host.txt`, then run:

```powershell
pwsh -NoProfile -File .\scripts\bootstrap_windows_clients.ps1 -HostsFile .\pilot-host.txt -Installer .\release\KSATClientSetup-2.1.0.exe -TrustCertificate .\release\KSATLabReleaseSigning.cer -ThrottleLimit 1
```

On that client, open KSAT, confirm it reaches the coordinator, sign in with a
test student, confirm the submit-confirmation popup, cancel it once, then submit
the test. Confirm the Faculty integrity log gives a readable reason for any
browser-context event you deliberately trigger.

## 6. Bootstrap the remaining clients

Restore all hostnames to `lab-hosts.txt` and run:

```powershell
pwsh -NoProfile -File .\scripts\bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\release\KSATClientSetup-2.1.0.exe -TrustCertificate .\release\KSATLabReleaseSigning.cer -ThrottleLimit 4
```

Retry powered-off or failed machines later with:

```powershell
pwsh -NoProfile -File .\scripts\bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\release\KSATClientSetup-2.1.0.exe -TrustCertificate .\release\KSATLabReleaseSigning.cer -RetryResults .\bootstrap-results.json
```

## 7. Confirm that future central updates are enabled

After the bootstrap, every successfully updated client already runs 2.1.0 and
has the managed updater. Do not publish
`KSATClientUpdate-2.1.0.ksat-client-update` to those clients again; that bundle
is retained as release evidence and for controlled update-path testing.

Starting with the next client version after 2.1.0, open **Client updates** in
Faculty and upload the newer signed `.ksat-client-update` bundle. Select one
client as the pilot, wait for its successful health report, then publish the
release. A client with an active assessment waits until the attempt and any
pending submission are safe. Every idle client installs the mandatory update
before the next student sign-in. The coordinator does not need to be
reinstalled merely to distribute a client update.
