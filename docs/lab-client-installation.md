# Install the preconfigured KSAT lab client

Use the single EXE supplied by faculty **for your lab**. You do not need to type a server URL, select certificate files, install Python or run a separate trust script.

1. Finish any active test. Upload results first when possible; if the old client cannot connect, faculty may proceed with the interactive upgrade without clearing its queue. Ask faculty before changing a PC used for an examination.
2. Run `KSATClientSetup-<your-lab>-2.1.5.exe` (pilot release: test one PC first).
3. An authorized administrator must approve the Windows prompt. Students without administrator permission need lab IT to approve it.
4. Confirm the displayed lab name and Coordinator address, then click **Install**.
5. When setup finishes, choose **Open KSAT Lab Client**. Sign in or create a student login through the normal client screen.

Setup installs the connection profile, public trust, client service and updater. Each fresh PC creates its own device identity. It does not copy another student's device records into your PC.

Version 2.1.5 allows an interactive same-server upgrade with saved pending submissions.
It preserves the database, queued answers, identity and connection settings; it does
not mark uploads as received or discard them. Reopen the client after installation
to resume eligible upload retries. An upgrade does not guarantee a connection or
resolve submissions already requiring faculty intervention. Silent/unattended
updates still defer while submissions are pending. The 2.1.4 false-downgrade fix
is included; do not change the registry version or delete data.

For another lab, faculty must regenerate the installer with **that lab's** server
URL and two public connection files. A fresh PC can use that installer; an
existing PC configured for a different coordinator is deliberately not switched
silently. Contact faculty for a separate, data-safe lab migration if needed.

In 2.1.4 the shortcut starts the service when needed; ordinary launches do not
require administrator approval. Closing the final KSAT tab signs out and safely
stops the client after a 15-second grace period and upload cleanup. Other open or
minimized KSAT tabs keep it running. Saved answers and unacknowledged uploads are
retained, and exam deadlines continue while closed. Pending uploads and central
updates resume when KSAT is next opened. See the [pilot checklist](client-window-lifecycle.md).

If the Coordinator is temporarily offline, a successful local installation can remain disconnected. Reconnect and retry; do not uninstall or delete stored data to fix a network problem. Contact faculty if setup displays an error instead of completing.

## Existing 2.1.0 or older clients

The older running client does not have the new installation-safety handshake. Setup refuses to stop it automatically. **Lab IT must first confirm that no test or pending upload remains, then stop the KSAT Lab Client Authority service and rerun setup.** This exception is for the initial legacy upgrade. A later handshake-enabled client can be quiesced safely by the installer.

A different-lab package, active assessment, unreadable state or normal downgrade is blocked. Pending uploads block silent installations, but no longer block interactive 2.1.5 installations. Do not delete `C:\ProgramData\KSAT Client`. Existing profiles and records are deliberately preserved.

A private publisher can still show a Windows warning on first launch. Ask IT to verify the signer and checksum. Never disable antivirus or accept an unexpected publisher merely to continue.
