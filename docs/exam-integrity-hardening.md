# Client-server exam integrity

This change applies to the KSAT 2.0 installed Lab Client and Faculty Coordinator.
The legacy central-browser application is not the assessment client being changed.

## What is recorded

| Situation | Client behavior |
| --- | --- |
| Exit browser API fullscreen (including Escape) | Record `fullscreen_exited`; immediately hide questions behind the resume gate. |
| Change tabs or minimize | Record `visibility_hidden` when the page becomes hidden. |
| Change windows, Alt+Tab, or leave the exam's focus while still fullscreen | Record `focus_lost` on window blur, independently of fullscreen. |
| A browser event is missed | Once per second, sample visibility, `document.hasFocus()`, and fullscreen state. |
| Navigate away, close, or suspend the page | Best-effort `pagehide`/`freeze` events, durable pending browser records, and service-side monitoring-gap detection. |
| Reload or reopen the same attempt | Replay pending records using their original IDs and record `browser_page_reloaded` when prior browser state exists. |
| Browser reports stop | The installed client service records `browser_monitor_gap` after 15 seconds at startup, then after more than 10 seconds without a heartbeat. One record per uninterrupted gap. |
| Client service restarts during an active exam | Record `browser_monitor_restarted` during recovery. |

Each observed transition gets its own persistent ID. Retransmitting that ID does
not increase the count, including after a service restart. A second rapid
departure gets a new ID and is not suppressed by the old two-second debounce.
Different signals (for example, blur and hidden) can describe the same action;
the count is a count of integrity signals, not necessarily separate incidents.
Legacy callers without IDs retain their existing debounce.

Pending records are stored in browser local storage before POSTing to the local
authority, sent with keepalive, and retried after failures and on return/online.
Submission waits for pending records to be acknowledged. A browser-storage
failure blocks continuation and asks for Faculty assistance. The local service
stores accepted records in its authenticated SQLite state and includes them in
the signed response bundle. Coordinator outages do not prevent local recording;
the existing sealed outbox delivers results and violations after reconnection.
The coordinator dashboard and CSV give the new event types readable labels.

The assessment clock continues while the gate is displayed. The independent
service watchdog also advances/seals an expired attempt when the page is closed
or frozen. An event that could not reach the service before automatic sealing
cannot be appended to an already signed submission. A service monitoring-gap
record provides evidence only if the missing-report threshold was exceeded.
A shorter interruption immediately before expiry can therefore lose an
undelivered event without producing a gap. A gap is labelled as an
interruption with an unverified cause, not as proof of a desktop switch.

## Browser limits and the stronger deployment option

JavaScript has no supported API identifying the current Windows virtual desktop.
A desktop switch is caught when Windows/browser exposes blur, hidden, fullscreen
loss, or suspended reporting. If none changes and JavaScript keeps reporting, a
web page cannot prove that switch occurred. A state sampler also cannot recover
a very brief transition if the browser delivered no event and it ended between
samples. Browser code/storage can be modified by someone with sufficient access.
These controls cannot honestly guarantee every OS-level departure.

For that requirement, use a managed exam kiosk or add a signed native helper in
the student's interactive Windows session. It would bind the actual exam window
to its process, monitor foreground-window and virtual-desktop changes, minimized
state and session locking, and authenticate its reports to the protected client
service. A LocalSystem service runs in session 0 and is not itself a reliable
observer of the student's interactive desktop. Browser tab detection is still
needed alongside the native monitor. That helper/kiosk deployment is not part
of this browser-and-authority hardening update.

Windows references: [interactive services and session isolation](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services),
[native virtual-desktop membership API](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ivirtualdesktopmanager-iswindowoncurrentvirtualdesktop).

Browser lifecycle reference: [Chrome Page Lifecycle API](https://developer.chrome.com/docs/web-platform/page-lifecycle-api).

## Updating and acceptance

Update the coordinator and every installed client together, between assessments.
The new event-ID column is additive: an existing authenticated database is
verified before migration and the changed state is authenticated atomically.
Attempt records, responses, and sealed bundles are retained. No protocol version
change is required. Existing deployment metadata continues to use version 2.0.0.

Build through the existing signing/inspection process in
`WINDOWS_EXE_BUILD.md`. Test-signed builds are only for isolated acceptance;
production distribution still requires the institution's signing identity.

On the actual lab Windows/browser versions, run a disposable assessment and:

1. Test Escape, Ctrl+Tab/Ctrl+Shift+Tab, Alt+Tab, minimize/restore, Win+D,
   Win+Ctrl+Left/Right, Win+Tab, and workstation lock/unlock.
2. Confirm questions hide immediately on observable loss, the timer continues,
   and returning requires the resume button. Repeat a switch twice rapidly.
3. Close/reopen and reload the browser. Leave it closed for at least 16 seconds
   and verify the service records the monitoring gap without browser requests.
4. Interrupt local POST responses; restore them and confirm queued IDs produce
   one stored event each. Disconnect the coordinator and verify local records
   still reach Faculty with the eventual signed submission.
5. Submit while a violation request is outstanding and confirm it is saved
   before sealing. Check the final Faculty result and CSV labels.
6. Confirm sign-in, practice/review screens, and completed attempts do not
   produce new exam violations. Test upgrade using a copy of existing client
   data before fleet rollout.

Automated browser-event tests simulate events and state transitions; they do not
replace physical acceptance of Windows virtual-desktop behavior.

## Verification on 10 September 2026

- Final focused run: 251 tests passed across runtime, authenticated storage,
  browser UI, local API, coordinator submission, and registration/CSV reporting.
- Broader distributed suite: 538 tests passed before the final review fixes;
  the affected modules were rerun in the final focused run.
- Root discovery: 618 tests completed with one Windows temporary-file cleanup
  error in the external HTTPS outage test (`.coordinator.lock` remained open).
  That test passed when rerun alone. The full root suite was not rerun afterward.
- Independent code review found a submission/poll race and a concurrent-tab
  queue overwrite. Both were reproduced in regression tests, fixed, and checked
  again by the reviewer; no remaining actionable findings were reported.
- A real runtime/store integration test confirms the client service writes a
  monitoring-gap event without any browser HTTP requests.

The current changes are on `codex/harden-client-exam-integrity`, based on
`bf98a24`. Test evidence is under `build/integrity-*.log` in this worktree.

Both test-signed executable and installer builds passed the existing release
pipeline: verified HTTPS startup, loopback-only client listener, UAC/non-UAC
payload equivalence, signature checks, recursive PyInstaller/Inno payload scans,
and hash generation. The staged production sources matched the tested files.
The binaries in `release/` were checked against the generated SHA-256 manifest.
They replace the previous client-server test builds at the existing download
paths; the prior binaries remain in Git history.

- [Lab Client test installer](../release/KSATClientSetup-2.0.0.exe)
- [Faculty Coordinator test installer](../release/KSATCoordinatorSetup-2.0.0.exe)
- [Artifact checksums](../release/SHA256SUMS.txt)

These files use an ephemeral certificate marked **NOT FOR PRODUCTION**. No
installer was run on the development machine and no lab deployment occurred.
Institution signing credentials and physical Windows acceptance remain required
before production rollout.
