# Ubuntu graphical setup build — 18 September 2026

## Delivered scope

The amd64 Ubuntu 18.04 and 22.04 packages have been rebuilt with a GTK 3 first-run
wizard and Applications menu entries. Package revisions are
`2.0.0+ubuntu2.1804` and `2.0.0+ubuntu2.2204`. Install through Software Install,
then open **KSAT Client Setup**: choose the coordinator HTTPS address, public CA
PEM, and public metadata JSON; approve the administrator prompt; open KSAT.
There are no mandatory terminal steps in the user installation flow.

The assessment runtime remains bundled. The desktop wizard uses Ubuntu's system
Python 3 and GTK packages, declared as Debian dependencies. It is compatible
with Ubuntu 18.04's Python 3.6 and GTK 3.22. Missing standard dependencies may
require internet access. This is not an offline, dependency-free installer.
The existing Windows coordinator, Windows clients, and server-only 1.3.4
installers are unchanged.

Only the fixed installed helper is elevated through polkit; the GUI, file
choosers, and browser run as the desktop user. Both polkit actions require
administrator authentication in an active local session, with no retained
authorization. Setup uses the existing full CA/signing-key/URL validator before
stopping a service or creating live configuration. Invalid input leaves the
existing service/state untouched. A configured PC refuses different coordinator
settings; existing-client startup preserves configuration, identity and attempts.
Retry finalizes file permissions after an interrupted first setup without
rewriting the files or replacing their keys.

## Verification

- **223 client tests passed on each target Ubuntu version**, including seven
  Linux identity/configuration/recovery tests. The interruption regression was
  observed failing before the permissions fix and passing afterwards.
- **Seven GTK widget tests passed on each target**, exercising real file-chooser
  widgets, metadata URL autofill, invalid/missing selections, visible validation
  errors, cancelled authorization, service-readiness failure, and existing-client
  startup. These tests mock the external elevation process and readiness boundary;
  they do **not** establish that a real desktop password dialog has been accepted.
- **38 Windows entrypoint/identity tests passed**, with two subtests; the seven
  Linux GTK tests were correctly skipped on Windows. Existing framework
  deprecation warnings remain.
- Both target-native PyInstaller/Debian builds succeeded. Installed-package
  acceptance exercised enrollment over HTTPS, login, content download, answer
  saving, service restart/recovery, acknowledged submission, released answer
  review, and reinstall with unchanged identity/configuration/key and retained
  results. The same-package reinstall is performed with `dpkg -i`, since APT may
  skip a local artifact whose version is already installed.
- The GUI was rendered and visually inspected under Xvfb on both Ubuntu versions.
  Desktop entries validate; the setup entry's two menu categories produce only an
  informational desktop-file-validator hint. Polkit policy registration was
  inspected in the installed environment.
- Package checksums are supplied beside each `.deb`. Embedded and adjacent build
  manifests record source hashes, Python/SQLite versions, and dependencies.

Build provenance uses base commit `e538fa4363f02b4c902b0c269cbb344d83aa5e0e`
plus the GUI changes recorded by each manifest's source hashes. The base commit
alone does not describe the delivered source. The packaged source hashes are
checked against the working source before delivery.

All installed-package checks use the existing **disposable WSL build
distributions**, not live lab computers. Earlier test-only `/var/lib/ksat` and
`/etc/ksat-client` trees were moved, not deleted, into dated
`/opt/ksat-package-check-gui*` backup folders inside those distributions.
No Windows production data was used or changed.

## Remaining physical-desktop acceptance

These remain **pilot builds**, not production-certified releases. On one physical
Ubuntu Desktop PC of each supported version, verify the entire click-only path:

1. Open the `.deb` with Software Install and accept its password dialog.
2. Open KSAT Client Setup from Applications; select both files and confirm the URL.
3. Accept the real polkit password dialog, then open the browser with Open KSAT.
4. Repeat with cancelled authorization and wrong files; confirm clear errors and retry.
5. Reboot, launch as an ordinary student, complete an exam, and verify faculty results.
6. Check fullscreen/tab/desktop switching and offline/restart/reinstall recovery.

WSL/Xvfb and mocked elevation cannot replace those desktop/package-manager and
authentication-agent checks. This change does not resolve the previously
documented load-spread/restart-fixture limitations of the larger protocol suite;
no full-repository green-suite claim is made. See the earlier
[pilot verification report](ubuntu-client-build-verification.md) and
[click-by-click installation guide](ubuntu-client-installation.md).

## Maintenance references

The privilege boundary follows the official [polkit pkexec documentation](https://polkit.pages.freedesktop.org/polkit/pkexec.1.html):
policies bind the installed executable path and its first argument, and the GUI
disables the terminal fallback authentication agent. File selectors use
[GTK 3 FileChooserButton](https://gnome.pages.gitlab.gnome.org/gtk/gtk3/class.FileChooserButton.html).
