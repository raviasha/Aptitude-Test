# KSAT client window lifecycle

## Approved user intent

Opening the client EXE starts KSAT without manual administrator PowerShell.
Closing the last KSAT tab/window signs out and shuts down the client after a
refresh grace period. Pending answers remain durable and resume uploading on
the next launch. The user accepts that central updates and uploads cannot run
while the client is closed. Coordinator 2.1.0 and the existing connection
configuration, identity, trust files, question ordering and review rules stay
unchanged. Existing published 2.1.2 binaries must not be replaced in place.

## Proposed mechanism

- Keep the protected Windows service as the authority; do not move privileged
  state into the student's account or embed credentials in the launcher.
- Configure demand start instead of boot-time automatic start. During setup,
  grant local Users only service query/start permissions on the KSAT service,
  preserving existing administrator/SYSTEM permissions. Do not grant stop,
  configuration, delete, ownership or DACL-write rights or alter other services.
- The launcher handles stopped, starting, running and stopping service states,
  waits for local HTTP readiness, reserves a launch grace period, and only then
  opens the browser. Report a readable error instead of opening a dead page.
- Track live KSAT documents with authenticated same-origin loopback WebSockets,
  not document visibility or browser process names. A hidden/minimized tab is
  still open. Closing one of several tabs cannot close the application.
- Last disconnect starts a 15-second grace period. Reconnection cancels it.
  A launch/first-page reservation lasts 120 seconds to cover initial loading,
  setup health checks and browser startup. Server-side socket keepalive detects
  crashed browsers; it may take longer than an orderly window close.
- At committed close, serialize against session changes, sign out locally,
  checkpoint any active attempt, and allow the outbox up to 10 seconds of extra
  upload opportunity. Preserve all unacknowledged submissions, including when
  an HTTP response is lost. These are scheduling bounds, not permission to kill
  a worker while it holds state; existing graceful worker cleanup still applies.
- Reopening during the grace period cancels shutdown; reopening after shutdown
  commits waits for STOPPED and starts the service again. Never force-kill it.
- Defer automatic shutdown while installer/update maintenance owns the client.
  Existing install safety checks and update rollback health checks remain valid.

## Exam and security invariants

- Closing during an exam does not submit early, reset the deadline or erase
  answers. Checkpoint before stopping, record the existing browser-monitor gap,
  and use the existing signed-deadline recovery path on reopening. An elapsed
  deadline seals the attempt through the normal expiry path.
- Browser-presence authorization must independently enforce the existing exact
  loopback Host/Origin and CSRF secret, because HTTP middleware does not cover
  WebSocket upgrades. Cross-origin pages cannot create leases or request stop.
- A lifecycle endpoint does not provide arbitrary service/process control.
- A shutdown failure must preserve data and report diagnostics; it must not
  turn into a destructive cleanup or bypass installation locks.
- No weakened CA/hostname checks, fixed server IP, service-account password,
  new trust identity or unrelated coordinator change.

## Acceptance

Test refresh, two tabs, hidden tabs, browser crash, slow browser launch, launch
during shutdown, repeated EXE launches, standard-user start access, offline and
ambiguous-response uploads, active/expired exams, and installer/update races.
Run client/runtime/store/outbox/auth/update/installer suites and a physical
Windows pilot before distributing new binaries. Report the known unrelated
question-bank collection errors separately; do not call the whole repo green.

Implementation and verification are in scope. New binary publication requires
a later explicit instruction; the currently published 2.1.2 release stays intact.
