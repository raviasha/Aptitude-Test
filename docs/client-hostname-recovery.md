# Client 2.1.2: manual update for `.local` lookup failures

Use this release when the configured name such as `desktop-i7nsd5h.local`
does not resolve, but `DESKTOP-I7NSD5H` resolves to the correct coordinator
and its TCP port 8443 is reachable. Client 2.1.2 can use the short Windows
hostname to connect while still requiring the original `.local` TLS
certificate and trusted CA. It does not store the current server IP.

Coordinator **2.1.0 stays unchanged**. Do not change the existing URL, regenerate
certificates, uninstall KSAT, or delete its ProgramData folders for this fix.

## Existing client PCs: use the standard installer

1. Finish tests and confirm pending answers have uploaded. Do not bypass an
   installation safety warning. Keep the coordinator running.
2. Back up the existing KSAT client data using your normal IT procedure while
   the client is stopped, if a backup is required. Do not move or erase it.
3. Run the signed **KSATClientSetup-2.1.2.exe** on one pilot client and approve
   Windows administrator access. Install over the existing installation.
4. Existing configuration is retained: there should be no need to enter a URL
   or select the connection files again. If setup asks for them unexpectedly,
   stop and check that you are upgrading the intended installation.
5. Open the client, sign in, take/submit a pilot test, and confirm the result on
   the coordinator. Check other launched assessments and post-close review.
6. Restart the server and pilot PC. Launch the coordinator on the server
   (unless IT has separately configured automatic startup), then test sign-in
   and submission again. Only after this pilot passes, install on the other PCs.

For a legacy **2.1.0** client, setup may ask IT to stop
`KSATLabClientAuthority`. Do this only after tests and pending uploads finish.
Current clients have an installation safety handoff; closing the browser alone
does not stop their Windows service. Never restart the service manually while
setup is still running.

If pending uploads cannot finish because the old client cannot resolve the
server, ask IT to restore resolution of the original `.local` name temporarily
to the current server address. Finish the uploads, then retry setup. Do not
delete saved answers or bypass the safety check to install this fix.

Confirm the installed client version on a student PC:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8010/api/build' -TimeoutSec 10
```

Expected version: `2.1.2`.

## New PCs: optional preconfigured lab package

Run **KSATLabPackageBuilder-2.1.2.exe** on the designated signing PC. Use the
same server HTTPS URL and the two public connection files. It creates a new
`KSATClientSetup-<lab-name>-2.1.2.exe`; the receipt must say builder `2.1.2`
and client `2.1.2`. Older generated EXEs do not change automatically.
The builder's connection test uses the same certificate-preserving fallback.

The standard installer also supports new PCs, but asks for those connection
inputs. Neither route requires changing or reinstalling the coordinator.

## Limits and central updates

- Every PC must have a unique hostname. The short server name must resolve to
  the actual server. This fix cannot repair duplicate names, firewall blocks,
  a stopped coordinator, a wrong clock, or invalid certificates.
- Fallback is restricted to `single-label.local` HTTPS names and an actual DNS
  lookup failure. TLS failures and connection refusals do not trigger it.
- HTTP Host and TLS verification/SNI retain the original configured name.
  Certificate verification is never disabled. `.local` traffic connects
  directly on the lab network rather than through environment proxy settings.
- The client remembers only a short-name preference in memory after success.
  New connections resolve that name again. It never saves a fixed IP or edits
  the Windows hosts file.
- `Test-NetConnection server.local` may still report failure: it does not use
  KSAT's fallback. Test the short hostname and the client application too.
- The existing signed central-update feature remains available. However, a
  disconnected older client cannot download the fix from the server. This
  release is installed manually; no new central-update bundle is supplied.

Pilot acceptance is still required on the physical lab PCs. Automated TLS,
packaging, and isolated executable checks do not prove a specific lab's DNS or
Windows service permissions.
