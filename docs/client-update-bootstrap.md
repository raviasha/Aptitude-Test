# One-time Windows client bootstrap

Use this once to place the updater-enabled KSAT client on existing lab computers. After this bootstrap, signed client releases are uploaded once in the Faculty **Client updates** page and each client updates itself before student sign-in.

## Prepare

1. Keep `KSATClientSetup-2.1.0.exe`, `KSATLabReleaseSigning.cer`, and the release `SHA256SUMS.txt` in the same folder.
2. Create `lab-hosts.txt` as UTF-8 text with one Windows hostname per line. Names are case-insensitive; duplicate or invalid names stop the run before deployment.
3. Use a Windows administrator account that is valid on the target computers. The script requests it interactively and never places its password in process arguments or reports.
4. Keep the target computers powered on and reachable by DNS and Windows remote management. A powered-off computer is reported as **Offline** and does not prevent other computers from being checked.

Run a dry run first:

```powershell
pwsh -NoProfile -File scripts/bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\KSATClientSetup-2.1.0.exe -TrustCertificate .\KSATLabReleaseSigning.cer -DryRun
```

Deploy after reviewing `bootstrap-results.json` and `bootstrap-results.csv`:

```powershell
pwsh -NoProfile -File scripts/bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\KSATClientSetup-2.1.0.exe -TrustCertificate .\KSATLabReleaseSigning.cer -ThrottleLimit 4
```

Retry only computers previously reported as **Offline** or **Failed**:

```powershell
pwsh -NoProfile -File scripts/bootstrap_windows_clients.ps1 -HostsFile .\lab-hosts.txt -Installer .\KSATClientSetup-2.1.0.exe -TrustCertificate .\KSATLabReleaseSigning.cer -RetryResults .\bootstrap-results.json
```

The script checks the local SHA-256 and pins the installer signer to the supplied certificate before contacting any client. On each reachable client it installs that exact certificate into the Local Machine Root and Trusted Publishers stores, installs the client as LocalSystem, and checks DNS, 64-bit Windows, free space, installation result, and the client `/api/build` health response. It removes the temporary task, certificate, script, and installer after every attempt. Reports contain hostnames, stages, version, timestamps, and stable error codes; they do not contain credentials.

If only one client is powered on, that client is updated and the others are marked Offline. Use the retry command when the remaining computers are available.
