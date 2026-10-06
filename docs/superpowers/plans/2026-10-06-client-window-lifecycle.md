# Client Window Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Opening KSAT starts its service; closing its last browser document safely shuts it down without losing answers.

**Architecture:** Retain the protected Windows service, switch it to demand start, and grant students narrowly scoped start/query access. Track authenticated browser connections separately from exam-integrity heartbeats. A lifecycle state machine coordinates launch reservations, last-tab grace, session closure and deferred upload cleanup.

**Tech Stack:** Python, FastAPI/Uvicorn, browser JavaScript, Windows SCM, Inno Setup; existing authenticated SQLite/outbox and installation locks.

**Spec:** `docs/superpowers/specs/2026-10-06-client-window-lifecycle.md`.

## Global Constraints

- Coordinator 2.1.0, all trust files and existing stored student data stay unchanged.
- Last-document grace: 15 seconds. Startup/launch reservation: 120 seconds. Additional outbox opportunity: 10 seconds.
- Service query/start permission only for local Users; never grant stop/configuration/delete/DACL-write access.
- No force termination, timer reset, answer deletion, new service credentials or bypass of maintenance checks.
- Hidden tabs count as open. Cross-origin and unauthenticated connections do not count.
- Central updates and pending uploads resume on next launch when fully closed.
- Do not overwrite or publish new binaries under the existing 2.1.2 names.

## Review Focus

1. Reopen in the boundary between last-tab grace and committed shutdown: cancel safely or wait/restart, never strand the launcher.
2. Refresh, two tabs and minimized/background tabs: distinguish live documents from visibility; never stop prematurely.
3. Server receives upload but response is lost: preserve the durable record and retry idempotently after reopening.
4. Closing an active exam or moving the clock backwards: deadline and checkpoint recovery cannot award extra time.
5. Setup/update owns maintenance while no tabs exist: prevent auto-close from breaking handoff, readiness or rollback.

## Task 1: deterministic browser lifetime controller

**Files:** create `ksat/client/lifecycle.py`, `tests/test_client_lifecycle.py`.

**Interfaces:** `BrowserLifetime(clock=time.monotonic)` exposes `reserve_launch() -> bool`, `connect(connection_id: str) -> bool`, `disconnect(connection_id: str) -> None`, and `begin_close(*, maintenance_busy: bool) -> bool`. All methods are synchronized. `begin_close` is irreversible and returns true once only; reservations/connections return false after closure commits.

- [x] Write deterministic-clock tests for the exact 15/120-second thresholds, duplicate disconnects, multiple connections, reservation renewal, refresh cancellation, maintenance deferral and an atomic connect-versus-close race.
- [x] Run `python -m pytest tests/test_client_lifecycle.py -q`; observe failures before implementation.
- [x] Implement the state machine without OS, HTTP, database or browser dependencies.
- [x] Rerun its tests; require all to pass.
- [x] Commit only this component and tests.

## Task 2: presence and safe server shutdown

**Files:** modify `client_app.py`, `static/client/app.js`, `static/client/index.html` only if needed, `ksat/client/windows_service.py`; create `tests/test_client_lifecycle_api.py`; extend `tests/client_ui_flow_harness.js`, `tests/test_client_runtime.py`, `tests/test_client_outbox.py`.

**Interfaces:** extend `create_client_app(services=None, *, lifetime=None, request_shutdown=None)` without changing default injected-test behavior. Add authenticated WebSocket `/api/lifecycle/presence` and CSRF-protected POST `/api/lifecycle/launch`. The launcher reads the existing root-page CSRF token before posting a reservation. The Windows service passes a shutdown callback that signals its existing stop event.

- [x] Write failing tests: reject foreign/missing Origin, invalid Host and missing/wrong CSRF; accept two authorized sockets; closing one retains service; closing the last requests exactly one stop after grace; launch returns 409 once close has committed.
- [x] Write browser tests that establish presence before state/login, keep it while hidden, reconnect on pageshow/network recovery, and show a relaunch message after a committed stop. Existing integrity events and heartbeat remain distinct and unchanged.
- [x] Write integration tests: sign-out on close, active-attempt checkpoint and deadline recovery, expiry while closed, offline outbox durability, lost-response duplicate retry, and closure deferred by maintenance ownership.
- [x] Run the new tests and observe each required behavior missing before implementation.
- [x] Implement the WebSocket boundary with independent exact Host/Origin checks and a CSRF subprotocol value; never log or echo the secret. Confirm Uvicorn's packaged WebSocket implementation is available before selecting it; do not silently fall back to unsafe unauthenticated presence.
- [x] Wire the lifetime monitor into lifespan. At closure commit, prevent new student mutations, serialize session logout, record/checkpoint runtime state, grant 10 seconds of outbox opportunity, and signal graceful service shutdown. If checkpointing fails, preserve the running authority and surface diagnostics; never erase or reset state.
- [x] Respect existing maintenance/process locks through the close decision and teardown. Test blocked and concurrent installer paths, not merely the reported update stage.
- [x] Run `python -m pytest tests/test_client_lifecycle.py tests/test_client_lifecycle_api.py tests/test_client_app_api.py tests/test_client_auth_api.py tests/test_client_ui_contract.py tests/test_client_runtime.py tests/test_client_outbox.py -q`.
- [x] Commit only these lifecycle/UI/test changes.

## Task 3: normal-user launcher and installer access

**Files:** create `ksat/client/launcher.py`, `tests/test_client_launcher.py`; modify `client_app.py`, `installer/KSATClient.iss`; extend `tests/test_lab_installer.py`, `tests/test_windows_packaging.py`.

**Interfaces:** `ensure_client_running(*, timeout_seconds=120.0) -> None` uses Windows SCM for the single constant `KSATLabClientAuthority`, queries state, starts it when stopped, waits through STOP_PENDING/START_PENDING and reserves a launch lease after local readiness. No command interpolation or administrator credentials. Default EXE/`--open-client` calls it before opening the browser and shows a readable Windows error on failure.

- [x] Write failing tests for stopped/running/starting/stopping, already-started races, two simultaneous launchers, wrong local listener, denied start, missing installation and bounded timeout. A failed health/lease check must not open the browser.
- [x] Write native disposable-service/descriptor tests proving local Users gain query/start only, existing administrator/SYSTEM rights are retained, permissions are idempotent, and only the named service is changed. Never use the real KSAT service for destructive tests.
- [x] Implement the SCM wrapper with native Windows APIs and exact-service handles. Retry state races within the 120-second budget; fail clearly without a UAC prompt if the installation has not provisioned access.
- [x] Configure `start= demand` in the installer. Preserve the existing service DACL and add only missing required start/query rights. Do not weaken filesystem ACLs or authentication. Configure access before the normal setup health check.
- [x] Run launcher, native descriptor, installer and packaging tests; include health-check startup without any browser, maintenance stop/restart, generic/lab modes and upgrade retention.
- [x] Commit only launcher/installer/test changes.

## Task 4: end-to-end verification and handoff

**Files:** add `docs/client-window-lifecycle.md`; update this plan's checkboxes and relevant acceptance documentation.

- [x] Run `python -m pytest tests -v -o faulthandler_timeout=90` with native Inno/extractor paths configured. Run bare `python -m pytest -q` too; enumerate the known question-bank collection failures rather than hiding them.
- [x] Run an isolated real-Uvicorn browser/HTTP test covering open, refresh, multiple tabs, close, stop, reopen, offline pending result and sign-in again. Use disposable state and never install over the packaging PC's real service.
- [x] Obtain one independent code review of service permissions, WebSocket authentication, shutdown races and durable exam state. Fix evidenced issues with regression tests.
- [x] Document the one-PC Windows standard-user pilot, including administrator installation, no-admin launch, service state after last-tab close, reboot behavior, pending uploads and update/rollback safety.
- [x] Report exact automated results and remaining physical-pilot gaps. Do not build/publish or replace the existing release without an explicit follow-up instruction.

Final automated results: 810 application tests passed, 34 skipped, 596 subtests;
two independent-review findings fixed with RED-to-GREEN regressions. The ten
pre-existing question-bank collection errors and required real-browser/standard-
user Windows pilot are recorded in `docs/client-window-lifecycle.md`. Source is
committed locally; binaries and GitHub remain unchanged.

## Execution choice

Recommended: **Native** execution in the current isolated worktree, followed by one independent review. These tasks share lifecycle/maintenance state; keeping implementation in one session avoids competing changes in `client_app.py`.
