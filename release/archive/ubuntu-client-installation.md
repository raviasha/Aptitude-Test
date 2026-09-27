# KSAT Ubuntu client pilot

These packages target **64-bit Intel/AMD Ubuntu 18.04 and 22.04** and the existing KSAT 2.0 Windows coordinator. They are test builds. Use one pilot PC of each Ubuntu version before wider rollout. They do not require Python or development tools on student PCs. A working graphical browser is required.

## Install

1. Copy the matching `.deb` and its `.sha256` file to the Ubuntu PC. In that directory run `sha256sum -c KSATClient-Ubuntu-22.04-amd64.deb.sha256` (substitute `18.04` for that version).
2. Install the matching package:

   ```sh
   sudo apt install ./KSATClient-Ubuntu-22.04-amd64.deb
   ```

   APT may download missing standard Ubuntu dependencies. The application runtime is included in the package.

3. From the **existing Windows coordinator**, copy only these two public files to the Ubuntu PC:
   - `C:\ProgramData\KSAT Coordinator\public\coordinator-ca.pem`
   - `C:\ProgramData\KSAT Coordinator\public\coordinator-public.json`
4. Configure once, replacing the sample hostname with the existing coordinator hostname. It must resolve from Ubuntu and match the coordinator certificate. Do not substitute an IP unless that IP is covered by the certificate.

   ```sh
   sudo /opt/ksat-client/KSATClient --configure \
     --base-url https://YOUR-SERVER-HOSTNAME:8443 \
     --ca ./coordinator-ca.pem \
     --metadata ./coordinator-public.json
   ```

5. Open **KSAT Lab Client** from Ubuntu's Applications menu. Alternatively open `http://127.0.0.1:8010/` in the browser on that PC. The service starts automatically at boot. Register/login using the normal student flow. The PC registers with the coordinator and appears under Managed lab computers.

The service connects to the coordinator using the supplied CA explicitly. Do not disable TLS checks or import coordinator private keys. The browser only visits the local HTTP address; this does not send plaintext credentials across the lab network.

## Upgrade and preserve data

Finish exams and confirm all results have uploaded first. Reinstall the matching new `.deb` with the same APT command. Package scripts stop the service synchronously and restart it if configuration exists. Existing identity, settings, attempts and cached content are retained. Do not rerun configuration to change servers; the existing validated configuration is deliberately reused.

To back up an idle client, stop `ksat-client` using `sudo systemctl stop ksat-client`, then have the administrator copy **both** `/var/lib/ksat` and `/etc/ksat-client` into one protected snapshot with ownership and modes preserved. Restart with `sudo systemctl start ksat-client` afterwards. Ordinary users cannot read these private folders. Do not clone them between PCs: each machine must generate its own identity.

Uninstalling, including package purge, deliberately retains these folders and the service account. Deleting either directory manually can destroy local attempts or make them unreadable. Pending submissions remain on the original PC until acknowledged by the server.

## Verify before lab use

On each Ubuntu version, check login, question text/images, saving and navigation, timed expiry, submission, Faculty results, and answer review after Faculty closes the test. Check restart recovery on the same PC and a brief network interruption followed by successful upload. Reinstall while idle and verify identity and configuration are unchanged.

Check fullscreen departure, tab/window switching, lock/unlock and desktop switching on the actual Ubuntu desktop/browser. Browser monitoring is not operating-system lockdown and must not be assumed to detect every desktop transition. Students should use ordinary accounts without sudo access. Use a browser and Ubuntu security maintenance appropriate to the institution; 18.04 compatibility is not a claim that an unmaintained OS/browser is safe.

Read-only diagnostics:

```sh
systemctl status ksat-client --no-pager
sudo journalctl -u ksat-client -n 50 --no-pager
sudo -u ksat-client /opt/ksat-client/KSATClient --validate-config
ss -ltn | grep 8010
```

Port 8010 must listen only on 127.0.0.1. Only the coordinator HTTPS port must be reachable across the lab. Do not expose the client service to the network.

## Security and build scope

Linux identity encryption uses a root-owned wrapping key readable by the dedicated service account. Linux permissions protect it from ordinary students. It is not TPM-backed, and administrators/root remain trusted. The server protocol and Windows installer are unchanged.

Each package is built on its target Ubuntu version with an included Python runtime, a dependency manifest, source hashes, and an installer SHA-256 checksum. Automated WSL verification cannot replace acceptance on physical Ubuntu lab PCs.
