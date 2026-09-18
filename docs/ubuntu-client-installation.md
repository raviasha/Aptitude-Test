# KSAT Ubuntu client — graphical installation

These packages target **64-bit Intel/AMD Ubuntu 18.04 and 22.04 Desktop** and the existing KSAT 2.0 Windows coordinator. **No terminal commands are required for installation or first setup.** Use one pilot PC of each Ubuntu version before wider rollout: physical-desktop acceptance is still required. A browser, a graphical package installer, and an administrator account are required. The assessment runtime is included; Ubuntu installs the standard GTK/Python desktop dependencies automatically when needed.

## Install

1. In Ubuntu **Settings → About**, check whether this PC runs Ubuntu 18.04 or 22.04. Copy the matching `KSATClient-Ubuntu-18.04-amd64.deb` or `KSATClient-Ubuntu-22.04-amd64.deb` to Downloads. Obtain it from your lab administrator; checksums are supplied for administrator verification.
2. Double-click the `.deb`, choose **Install**, and approve Ubuntu's administrator password prompt. If it opens in Archive Manager, close that window and use **right-click → Open With → Software Install** instead. A graphical GDebi Package Installer is also suitable. Ubuntu may need internet access to retrieve missing standard dependencies. Wait for installation to finish; do not extract the package as an archive.
3. From the **existing Windows coordinator**, copy only these two **public trust files** to the Ubuntu PC:
   - `C:\ProgramData\KSAT Coordinator\public\coordinator-ca.pem`
   - `C:\ProgramData\KSAT Coordinator\public\coordinator-public.json`
4. Open **Show Applications**, search for **KSAT Client Setup**, and open it. (Opening **KSAT Lab Client** before configuration also opens the wizard.) Use the two file-selection buttons to choose the `.pem` and `.json` files. Selecting metadata fills in the server HTTPS address when the address field is empty. Check that it is the address supplied by your faculty coordinator, normally `https://YOUR-SERVER-HOSTNAME:8443`.
5. Click **Configure client**, approve the graphical administrator password prompt, and wait for **KSAT is ready**. The wizard checks that the address, CA certificate, and signing-key metadata match before saving protected settings. If you cancel authorization or select mismatched files, the wizard shows an error and allows you to retry.
6. Click **Open KSAT**. On later visits, open **KSAT Lab Client** from Applications; it opens the local browser interface directly when the service is running. The service starts automatically at boot. Register/login using the normal student flow. The PC registers with the coordinator and appears under Managed lab computers.

The coordinator hostname must resolve from Ubuntu and match its certificate. Do not substitute an IP unless that IP is covered by the certificate. The wizard does not change DNS settings. Keep the coordinator running and reachable during the lab pilot.

If this PC was already configured but the client is not running, open **KSAT Client Setup → Start existing client** and approve the password prompt. This validates and starts the existing configuration without asking for files again or replacing its identity. Setup deliberately refuses a different coordinator's files on a configured PC; changing servers requires a controlled administrator migration.

The service connects to the coordinator using the supplied CA explicitly. Do not disable TLS checks or import coordinator private keys. The browser only visits the local HTTP address; this does not send plaintext credentials across the lab network.

## Upgrade and preserve data

Finish exams and confirm all results have uploaded first. Double-click the matching new `.deb` and choose **Install/Upgrade** in the graphical package installer. Package scripts stop the service synchronously and restart it if configuration exists. Existing identity, settings, attempts and cached content are retained. Do not uninstall or delete data to upgrade. The GUI package revision is `2.0.0+ubuntu2.1804` or `2.0.0+ubuntu2.2204`; the earlier `ubuntu1` packages do not include this wizard.

## Administrator backup and diagnostics (optional; not installation steps)

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

Linux identity encryption uses a root-owned wrapping key readable by the dedicated service account. Linux permissions protect it from ordinary students. It is not TPM-backed, and administrators/root remain trusted. The server protocol and Windows installer are unchanged. The GTK wizard and browser run as the desktop user; only the installed configuration/service helper runs with administrator authorization through polkit. No private key is supplied through the wizard and no system-wide CA certificate is installed.

Each package is built on its target Ubuntu version with an included Python runtime, a dependency manifest, source hashes, and an installer SHA-256 checksum. Automated WSL verification cannot replace acceptance on physical Ubuntu lab PCs.
