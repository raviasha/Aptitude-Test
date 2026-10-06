# Client window lifecycle — release acceptance

This change is local source work, not a published replacement for Client/Builder
2.1.2. The coordinator and connection files are unchanged. Build a new version
and a matching builder only after release approval; never overwrite 2.1.2 assets.

## Expected operation

Opening the KSAT shortcut starts the installed `KSATLabClientAuthority` service
without an administrator prompt, waits for the local application, reserves a
120-second launch window, then opens the browser. Setup grants local Windows
Users only service query-status and start permissions; existing permissions and
protected student-data directories remain intact. Setup requires administrator
access and configures the service for demand start rather than automatic boot.

The application tracks each authenticated browser document independently of exam
integrity heartbeats. Hidden/minimized tabs remain open. Closing the final KSAT
tab starts a 15-second grace period. Refreshing or reopening in that period
cancels closure. After the grace period, the application checkpoints the active
exam, signs the student out, and gives queued results up to 10 additional seconds
to upload. It then waits for any in-flight upload to finish before closing storage
and the service. Unacknowledged results remain durably queued for the next launch.
A stalled operating-system/network request can delay actual exit; data safety
takes precedence over forcibly killing the process.
Transient drain failures are retried; a permanent failure requires administrator
diagnosis. Optional upload-status checks may fail without preventing safe drain
and shutdown. A request body arriving after closure commits is rejected.

Opening KSAT during committed shutdown waits for completion and restarts the
service. If startup cannot finish within 120 seconds, the launcher shows a readable
retry/repair message and does not open a broken browser page. No test deadline is
extended by closure or relaunch. Closing an exam can still trigger the existing
exam-integrity rules. Uploads and central updates cannot run while KSAT is fully
closed; they resume after the next launch.

## Automated evidence

- Deterministic lifecycle tests cover exact grace/reservation thresholds,
  concurrent connect/close, multiple tabs, refresh and maintenance deferral.
- API tests independently check WebSocket Host, Origin and CSRF, session cleanup,
  pending-result preservation, checkpoint failure and in-flight mutations.
- Runtime/outbox tests cover persisted answers, deadlines, restart/clock rules,
  offline retries and a server receipt whose response is lost.
- JavaScript presence tests and the existing UI harness cover reconnect and page
  lifecycle behavior. A real Uvicorn HTTP/WebSocket test uses an ephemeral port,
  disposable state and fake coordinator/storage to exercise two tabs, refresh,
  login, final close, process-loop exit and reopening signed out. This is not a
  physical-browser or Windows SCM installation test.
- Native Windows descriptor tests preserve SYSTEM/administrator/custom/deny ACEs,
  reject null DACLs, add query/start only and prove idempotence. SCM calls are
  mocked to avoid altering this packaging PC's real installed service.
- Native generic/lab installer compile/extract and handoff tests run with
  `KSAT_ISCC` and `KSAT_INNOEXTRACT` configured.

Final verification on 2026-10-06 at source commit `d5aefb8`:

- `python -m pytest tests -v -o faulthandler_timeout=90`: **810 passed,
  34 skipped, 596 subtests passed**, 37 existing deprecation warnings,
  795.50 seconds. Native Inno/extractor paths were enabled.
- Launcher/installer/packaging gate: **108 passed**, 7 subtests.
- Final review regression/real-transport gate: **12 passed**, 5 subtests.
- One independent source review at `3dd848e` found two Important issues: delayed
  request-body admission and shutdown-monitor error cleanup. Both were reproduced
  with failing tests, fixed in `d5aefb8`, and verified by the full run above. No
  Critical/Minor findings or declined-to-judge items. There was no second review.
- A preliminary run had one disposable HTTPS fixture-startup timeout (805 passes).
  That outage test passed both alone and in the final full run; no production
  workaround was added for the fixture timeout.

Bare repository-wide pytest also collects unrelated question-bank tool tests; its
pre-existing missing PDF/schema dependencies and duplicate module names must be
reported separately, not mistaken for passing application tests.

The repository-wide collection failures reproduced on 2026-10-06 are:

- `data-engineering/python_vision_calibration/tests/test_baseline.py`
- `data-engineering/python_vision_calibration/tests/test_cli.py`
- `data-engineering/textbook_chapters/tests/test_build.py`
- `data-engineering/textbook_chapters/tests/test_chapter02.py`
- `data-engineering/textbook_chapters/tests/test_chapter03.py`
- `data-engineering/textbook_chapters/tests/test_chapter04.py`
- `data-engineering/textbook_chapters/tests/test_vision_pipeline.py`
- `scripts/test_compile_logical_recovery_results.py`
- `scripts/test_generate_v2_marker_overrides.py`
- `scripts/test_run_logical_boundary_recovery.py`

Missing packages are `pdfplumber`, `pypdf` and `jsonschema`; the textbook build
test also collides with another collected `test_build` module. These unrelated
tools were not changed or installed for the client lifecycle task.

## Required one-PC Windows pilot before release

Use a spare lab PC or disposable VM, not a live exam PC. Keep its old signed
installer and an administrator-made backup of the existing client data. Do not
delete ProgramData to troubleshoot and do not interrupt an active exam/upload.

1. Install the newly versioned pilot client as administrator over the old client.
   Check that the URL, trust, device identity and saved records are retained.
   Repeat with both the generic installer and a generated lab package.
2. Inspect `Get-Service KSATLabClientAuthority` and `sc.exe sdshow
   KSATLabClientAuthority`: demand/manual startup; local Users gain query-status
   and start only. Check a standard account can start/query but cannot stop,
   reconfigure, delete or change service permissions. A pre-existing deny must
   not be silently removed. Do not change the DACL by hand as a workaround.
3. Sign into Windows with a standard student account. Open the KSAT shortcut:
   no UAC prompt; login page appears only after readiness. Try two launchers
   simultaneously, then refresh, open a second tab, minimize and restore.
4. Close one tab: the other remains usable. Close the final tab: after 15 seconds
   plus upload/cleanup time, the service becomes Stopped. Reopen before grace
   ends, during shutdown, and after it stops; all paths must recover. Reopening
   after completed closure must show login, not the previous student's session.
5. Reboot: the demand-start client service stays stopped until KSAT is opened.
   The coordinator must remain reachable using the previously tested hostname
   fallback. No coordinator restart or connection-file replacement is needed.
6. On disposable test accounts, close during an exam, reopen before its deadline,
   then repeat after expiry. Verify answers remain, remaining time does not grow,
   and existing integrity rules hold. Repeat after a backward clock adjustment.
7. Disconnect the lab network, submit a test, close KSAT, reconnect and reopen.
   Verify the queued result uploads once logically, including a simulated lost
   response after server receipt. Do not expose answers before faculty closes.
8. Exercise central-update installation and rollback with no browser and with
   the final tab closing concurrently. Maintenance must own the handoff and the
   service must not auto-close during protected work. Confirm health checks work
   without opening a browser. Check rollback to the previous signed client.
9. If a step fails, keep setup/service diagnostics and stop pilot rollout. A
   passing unit suite does not replace this standard-user permission pilot.
