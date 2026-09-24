# Coordinator-Managed Lab Client Updates

**Date:** 24 September 2026  
**Status:** Approved design

## Purpose

KSAT lab clients currently require an administrator to run a client installer on
every machine. The new system reduces routine client deployment to one action:
Faculty uploads one institution-signed client update bundle to the Faculty
Coordinator. Enrolled lab clients obtain and install the required update from
that coordinator before student sign-in.

The coordinator does not self-update through this mechanism. It receives one
normal installer upgrade to add client-update management. Existing lab clients
receive one updater-enabled bootstrap installation, deployed centrally from a
hostname list with a shared administrator account. Later client releases need no
per-machine administration.

The same client release adds an explicit confirmation dialog for manual
assessment submission, records why each submission was initiated, and replaces
raw browser-integrity codes with clear explanations for Faculty.

## Success criteria

- Faculty uploads and publishes a client release once on the coordinator.
- A required update installs before student sign-in without administrator input.
- One selected online client must install and pass health checks before Faculty
  can publish the release to all clients.
- Offline clients install the required release when they next connect.
- In-progress attempts and sealed submissions are not lost or interrupted.
- Clients accept only authentic, untampered, newer institution-approved builds.
- A failed installation restores the last known-good client and reports a useful
  diagnostic reference to Faculty.
- The one-time bootstrap can be sent from one administrator computer to a list
  of client hostnames without saving the administrator password.
- A manual assessment submission requires a second explicit confirmation.
  Timer expiry continues to submit automatically.
- Every client-emitted browser-integrity event has a canonical code, readable
  title, explanation of what was observed, and an honest confidence description.

## Scope

This design covers Windows Faculty Coordinator and Windows lab clients. It does
not add automatic coordinator updates, change assessment-pack signing, or use an
internet update service. Ubuntu client updating remains outside this release.

The release produces three separate artifacts:

1. A Faculty Coordinator installer that adds client-update management.
2. An updater-enabled lab-client installer used for the one-time bootstrap.
3. A reusable signed client-update bundle format for later client releases.

Client update bundles remain separate from coordinator installers so client and
coordinator versions can be released independently.

## Roles and trust boundaries

The offline institutional release signer authorizes software releases. Its
private key never resides on the coordinator or a lab client. The coordinator
and bootstrap installer contain only the public verification key.

The Faculty Coordinator validates, stores, and serves a release. It cannot
create or alter an authorized release. Upload and publication require an
authenticated Faculty administrator session and CSRF protection.

The installed `KSATLabClientAuthority` service runs as LocalSystem. It performs
update discovery and download over the client's existing pinned HTTPS connection
to its coordinator. A separate updater helper replaces the stopped client,
starts it again, performs a health check, and initiates rollback when necessary.
Student browser sessions cannot choose an arbitrary package, path, URL, or
version.

The one-time bootstrap deployment tool runs on an administrator-controlled
computer. It prompts for a `PSCredential`, retains it only in process memory,
and must not place the password in command lines, files, output, or logs.

## Signed client-update bundle

Use a dedicated extension such as `.ksat-client-update`. The bundle is a
deterministic archive containing exactly:

- `manifest.json`;
- the Windows client installer named by the manifest; and
- `manifest.sig`, a signature over the canonical manifest bytes.

The strict manifest contains:

- format version;
- release identifier;
- client semantic version;
- minimum source version allowed to apply the update;
- target operating system and architecture;
- installer filename, byte length, and SHA-256 digest;
- Authenticode publisher identity expected on the installer;
- publication timestamp and concise release notes; and
- a health-check timeout.

The manifest rejects unknown fields, duplicate JSON keys, noncanonical versions,
unsafe filenames, invalid sizes, expired or not-yet-valid signing certificates,
and a target version that is not newer than the installed version. Bundle
parsing applies strict member-count and uncompressed-size limits.

Both coordinator and client verify the offline release signature, archive shape,
installer digest and length, target platform, and Authenticode signature. The
client repeats every check after download; it never relies only on the
coordinator's result. Software-update signing is separate from coordinator TLS,
device identity, and assessment-release signing.

## Coordinator workflow

The coordinator adds a **Client updates** page with four states:

1. **Uploaded:** validated and stored, but offered to no clients.
2. **Pilot:** offered only to one explicitly selected enrolled device.
3. **Published:** mandatory for all enrolled clients below the target version.
4. **Withdrawn:** no new installation may begin.

Faculty uploads a bundle, reviews its version, signer, hash, size, compatibility,
and release notes, then selects one online client as the pilot. Publication is
disabled until that exact device reports a successful installation and matching
health check. Faculty then chooses **Publish to all clients**.

The dashboard reports each hostname as Updated, Downloading, Waiting, Offline,
or Failed. A failed status includes a stable diagnostic reference, stage, target
version, attempt time, and safe retry action. It does not expose secrets or raw
credentials.

Withdrawing a release prevents downloads and installations that have not
started. It does not downgrade clients that already installed it. Recovery from
a bad published release uses a new, higher-version corrective bundle.

Coordinator endpoints provide signed-device update discovery, range-capable
artifact download, and idempotent progress reporting. They reuse enrolled device
authentication and the pinned HTTPS channel. Downloads are read-only and served
only from coordinator-owned update storage. Update audit records are retained.

## Client update lifecycle

The client service checks update policy at startup and before presenting student
sign-in. A published release whose target version is newer is mandatory. The UI
shows only maintenance status: Checking, Downloading, Verifying, Installing and
restarting, or Update complete. There is no Skip action.

Clients start download after a bounded randomized delay to prevent a simultaneous
lab-wide surge. Downloads use a protected staging directory, a partial filename,
bounded retries, and range resume. A completed file is atomically renamed only
after length, digest, release signature, and Authenticode verification pass.

The service writes a canonical update request into protected state, copies the
updater helper to an execution path that the installer will not replace, launches
it, and exits. The helper:

1. verifies the request and installer again;
2. waits for the client service and browser authority to stop;
3. retains the last known-good signed installer and update record;
4. runs the new installer silently with restart suppressed;
5. starts the client service;
6. checks the loopback build endpoint, target version, device identity,
   coordinator configuration, and authenticated state-store availability; and
7. records success or reinstalls the last known-good version and reports failure.

Update operations are idempotent across restart and power loss. Startup recovery
continues from the durable stage record. Temporary and superseded artifacts are
deleted only after health verification and retention of one last known-good
installer.

## Assessment safety

A required update normally blocks sign-in, but it never supersedes saved student
work:

- An in-progress attempt resumes on the installed version. Updating waits until
  that attempt is submitted and acknowledged.
- A sealed pending submission continues its existing retry workflow. Updating
  waits for coordinator acknowledgment.
- A completed or idle client updates before another student can sign in.

The coordinator may mark an obsolete client version as mandatory, but it cannot
remotely terminate an active attempt. Update state and assessment state remain
separate durable state machines.

## One-time remote bootstrap

The bootstrap deployment tool accepts the updater-enabled installer, its signed
manifest, and a UTF-8 hostname list. It prompts once for the shared administrator
username and password using Windows protected credential input.

For each hostname it performs a non-mutating preflight: DNS resolution, host
reachability, supported Windows architecture, remote administration availability,
free disk space, existing KSAT service and version, and installer signature/hash
verification. The tool then copies the installer to a protected temporary path,
creates a one-time SYSTEM installation task through authenticated Windows remote
management, polls for completion, verifies the new version, device identity and
service health, removes the task and temporary files, and records the result.

The tool limits concurrency and can retry only Offline or Failed hosts. Its CSV
and JSON reports contain hostnames, stages, versions, timestamps, and diagnostic
codes. They never contain the password, reusable tokens, or protected client
identity data. A dry-run mode performs all preflight checks without copying or
executing software.

When the preferred Windows remote-management transport is unavailable, the tool
may use built-in authenticated SMB plus Task Scheduler RPC as a fallback, while
retaining the same no-password-in-command-line rule. A machine unavailable over
both transports remains on the retry list rather than weakening its security
configuration automatically.

## Manual submission confirmation

The client treats submission causes explicitly:

- `manual_confirmed`;
- `timer_expired`; and
- `sealed_recovery`.

Selecting **Submit assessment** manually opens a modal showing answered and
unanswered counts and stating that answers cannot be changed after submission.
**Continue assessment** receives initial focus. Escape closes the modal. Only
activating the modal's separate **Submit assessment** control can produce a
manual submission. Form default actions, Enter outside that control, key repeat,
and double clicks cannot bypass the confirmation.

Timer expiry still seals and submits without a dialog. Recovery may retransmit a
previously sealed bundle without asking again. The submission cause is stored in
local attempt history and included in coordinator audit data so reported
automatic submissions can be distinguished from expiry and recovery.

## Browser-integrity event explanations

The current client emits `contextmenu` when the browser reports a context-menu
request, while the coordinator's readable-label mapping expects `context_menu`.
Distributed submissions therefore can display the raw `contextmenu` code as a
violation. A right-click can be accidental, and KSAT already prevents the menu
from opening, so it is not meaningful evidence of misconduct. Other emitted
actions, including drag/drop, print attempts, and blocked shortcuts, also lack
complete faculty-facing explanations.

Define one versioned integrity-event catalogue used by client validation,
coordinator ingestion, result views, and CSV exports. Each entry contains:

- a canonical stable code;
- a short faculty-facing title;
- a plain description of exactly what the browser or client observed;
- an evidence class: direct blocked action, visibility state change, or technical
  monitoring anomaly; and
- a caution where the event does not establish intent.

The client continues preventing the browser context menu during an active
assessment, but it stops recording right-click or `contextmenu` as an integrity
event. Historical `contextmenu` and `context_menu` records remain in storage for
audit continuity, are classified as **Blocked browser menu request
(informational)**, and are excluded from violation counts, integrity flags, and
the primary Faculty results view. They may appear only in expandable technical
history or a diagnostic export.

`browser_monitor_gap` explains that the page stopped sending monitoring
heartbeats for more than the configured interval and that browser suspension,
reload, device load, or connectivity can cause it; its cause remains unverified.

The client emits only canonical catalogue codes. The coordinator accepts the
legacy aliases `contextmenu`, `context_menu`, and `fullscreen_exit` for existing
records and maps them without rewriting historical timestamps. Context-menu
aliases map to the non-violation informational entry. Unknown future codes
display **Unrecognized integrity event** in the main view; the raw code is
available only under technical details.

Faculty results group events occurring in the same short browser-state
transition as one incident while retaining every timestamp in expandable
details. The primary view shows the readable title, occurrence time, explanation,
and evidence class. It uses “integrity event” rather than presenting every event
as proof of misconduct. CSV export includes canonical code, title, explanation,
evidence class, occurrence time, and original code when an alias was received.

## Failure behavior

- Invalid bundle: coordinator rejects upload without publishing any data.
- Download interruption: client keeps the current installation and resumes.
- Signature, hash, publisher, platform, or downgrade failure: client deletes the
  staged artifact, blocks sign-in, and reports a diagnostic.
- Installer failure: updater restores the last known-good version.
- Failed post-install health check: updater rolls back and reports which check
  failed.
- Coordinator unavailable: an idle outdated client waits at maintenance status;
  an active attempt remains usable under the assessment-safety rules.
- Pilot failure: release remains unavailable to all other clients.
- Client offline during publication: it updates on its next connection.

## Testing and acceptance

Automated tests cover canonical bundle generation, strict parsing, signature and
Authenticode validation, mutation, truncation, archive limits, incompatible
architecture, downgrade and replay rejection, upload/pilot/publish/withdraw
authorization, device-scoped downloads, range resume, status idempotency, and
dashboard summaries.

Client state-machine tests cover idle update, offline recovery, interrupted
download, restart at every durable stage, active attempt deferral, pending
submission deferral, installer failure, health-check failure, rollback, retained
identity/configuration/state, and cleanup. UI tests cover mandatory maintenance,
manual confirmation focus and keyboard behavior, duplicate activation, timer
expiry, and submission-cause reporting. Integrity tests enumerate every event the
client can emit and fail when any event or legacy alias lacks a catalogue entry,
plain explanation, evidence class, API rendering, and CSV rendering. They verify
that no new context-menu action is recorded, historical context-menu codes are
excluded from violation counts and primary results, and monitoring gaps remain
explicitly unverified.

The remote bootstrap tool is tested with fake transports for credential and
failure handling, then on disposable Windows virtual machines. Acceptance uses
one real designated client as pilot. It verifies remote bootstrap, automatic
installation, service restart, unchanged device identity and coordinator trust,
retained state, and dashboard status. After pilot success, additional clients are
tested as initially offline and then reconnected. A load test covers up to 100
clients checking, downloading with randomized starts, and reporting status
without affecting assessment submission traffic.

Production artifacts must pass the existing executable/installer inspection and
institutional signing process. Test-signed artifacts remain limited to isolated
acceptance and are not distributable.

## Operational rollout

1. Back up coordinator and one representative client state.
2. Install the coordinator release that adds client-update management.
3. Build and institution-sign the updater-enabled client bootstrap release.
4. Run the remote bootstrap tool in dry-run mode against the hostname list.
5. Resolve unreachable hosts, then deploy with bounded concurrency.
6. Confirm every reachable host reports the updater-enabled version and retained
   identity.
7. For the next client release, upload its signed bundle to the coordinator.
8. Keep one chosen client online, run it as pilot, and inspect its health result.
9. Publish to all clients. Offline clients update at their next startup.

This workflow is the permanent client-release process after the one-time
bootstrap.
