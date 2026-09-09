# Distributed assessment operations runbook

This runbook is for institution-managed Windows lab computers. The supported
release is one **KSAT Faculty Coordinator** and at most 100 installed **KSAT Lab
Clients** on the private institutional network. Students answer locally; the
coordinator receives one sealed response bundle and returns the authoritative
score.

## Change control and prerequisites

Schedule the rollout outside an assessment window. Record the four release-file
SHA-256 hashes, verified Authenticode publisher/timestamp, coordinator DNS name,
HTTPS port, backup location, and change ticket. Use a stable DNS name; do not
configure clients with an IP address that may change. Physical installer
execution, Windows Firewall changes, and machine-root CA installation require
explicit institutional authorization.

Build and inspect the release as documented in
[`WINDOWS_EXE_BUILD.md`](../WINDOWS_EXE_BUILD.md). Do not continue unless the
full test suite, executable smoke, recursive PyInstaller/Inno extraction scan,
30-client gate, 100-client gate, and outage gate all pass.

## Install and initialize the coordinator

1. On the authorized faculty/server computer, verify the installer hash against
   `release\SHA256SUMS.txt`.
2. Run `KSATCoordinatorSetup-2.0.0.exe` as Administrator. Enter the stable DNS
   hostname and approved private-network TCP port (normally 8443).
3. Start the coordinator and browse locally to `https://127.0.0.1:8443`.
4. Confirm `C:\ProgramData\KSAT Coordinator\public` contains
   `coordinator-ca.pem` and `coordinator-public.json`.
5. Copy only those two public files to approved removable media or a protected
   software-distribution share. Never distribute `secrets`, `aptitude.db`,
   session tokens or assessment packs.
6. Verify the installer-owned inbound firewall rule is private-profile only,
   names the installed coordinator executable, and matches the chosen port.

Automatic device registration deliberately has no student-entered enrollment
code. The private lab network is therefore the enrollment trust boundary: use
firewall or VLAN controls so only institution-managed lab computers can reach
the coordinator port, and never expose that port to a public or student-owned
network.

As a deployment acceptance check, confirm the coordinator URL succeeds from a
managed lab client and fails from an unmanaged or student-owned computer on a
different network segment. Record both results with the firewall/VLAN change
ticket before enabling student access.

The coordinator must be backed up before clients are enrolled. See “Backup and
restore” below.

## Install each lab client

1. Verify `KSATClientSetup-2.0.0.exe`, `coordinator-ca.pem`, and
   `coordinator-public.json` hashes through the approved channel.
2. Run the client installer as Administrator and supply the coordinator HTTPS
   URL and both public files. The installer creates the automatic LocalSystem
   `KSATLabClientAuthority` service and service-owned state directories.
3. Confirm the client opens only `http://127.0.0.1:8010`; it must create no
   inbound firewall rule. Confirm ordinary lab accounts cannot directly modify
   `identity`, `state`, or `packs`; their shortcut opens only the loopback UI.
4. Open the client. It registers its protected device identity automatically
   using the Windows computer name; there is no enrollment form or code.
5. Confirm the computer appears as active in Faculty **Devices**. A student may
   then choose **New student? Create account** on the client sign-in screen.

After an accepted submission, the student can choose **Back to assessments**
to take another launched test without signing out. The available-assessments
screen checks again every five seconds and also has a **Refresh** button.
Completed reviews load separately from the available test list; answers and
solutions still require Faculty to close the test.

For the communication-performance update, install the updated coordinator
setup on the server and the updated client setup on each lab PC, outside an
active test. Both programs changed. The existing 2.0 configuration, trust
files, student records, and saved client attempts are retained by the
installers; this update does not require recreating them.

To revoke a lost or reimaged machine, use Faculty **Devices → Revoke**, record a
reason, and verify subsequent signed requests fail. Reactivate only after the
machine identity and custody have been checked. A reimaged machine should
normally receive a new enrollment identity.

## Upgrade an existing 2.0 client state store

Client state now has an explicit version boundary. Version 1 is the original
2.0 lifecycle database without an authenticated journal/anchor; version 2 is
the LocalSystem-owned authenticated store. Ordinary service startup never
adopts a nonempty version-1 database. The installer stops the prior client and
runs a migration preflight before installing the root CA or configuring and
starting the service. A fresh or already verified version-2 store passes that
preflight without a confirmation switch.

For a computer with retained version-1 state:

1. Keep the client service stopped and copy the complete `C:\ProgramData\KSAT
   Client` tree, including SQLite WAL/SHM files, identity, cached packs, and
   configuration, to approved protected storage.
2. Record whether the client has a cached pack, an in-progress attempt, a
   sealed-pending outbox entry, or an acknowledged attempt. Do not edit SQLite.
3. Run the installer as Administrator with
   `/CONFIRMLEGACYSTATEMIGRATION=1`. The equivalent service-stopped diagnostic
   command is `KSATClient.exe --migrate-state --confirm-legacy-state` from an
   elevated console.
4. Retain the count-only JSON summary and confirm its cached-pack,
   in-progress, sealed-pending, acknowledged, and outbox counts match the
   inventory. The migration preserves the exact lifecycle records, appends one
   authenticated migration entry, and stamps schema version 2.

The confirmation is not a validation bypass. SQLite integrity/foreign-key
checks, cached-pack fields, every attempt snapshot, sealed bundle, receipt, and
outbox relationship must all validate first. Without confirmation, a nonempty
version-1 database is left unchanged and installation stops. Invalid or
partially authenticated state also stops installation; retain it for forensic
review and restore the complete backup with the prior software rather than
starting the new service. If power loss occurs after the migration transaction
but before its anchor is published, ordinary startup still fails closed. Rerun
the same confirmed migration command as Administrator; it can publish only the
single HMAC-valid migration tail whose state digest and version-2 stamp match.

## Prepare and launch an assessment

1. Import and validate the question bank. Correct answers and solutions remain
   coordinator-side.
2. Create the faculty assessment. Review question count, duration (one minute
   per question), and immutable release status.
3. Allow clients to prefetch the single shared encrypted pack before students
   begin. A prepared pack is reusable by all enrolled clients. Its review
   compartment is separately encrypted, and the review key is never disclosed
   before Faculty closes the assessment.
4. Select **Launch** once. The launch creates a 10-minute start window. Each
   student receives the same question identifiers in a deterministic per-ticket
   question order; options are not shuffled. Each student’s timer begins when
   that student’s ticket is issued.
5. Monitor eligible, started, submitted, voided, queued, and intervention
   counts. Do not duplicate/relaunch a release after any ticket has been issued;
   use **Duplicate** to create a new immutable release.
6. Submission acknowledgment reveals the score only. When the assessment is
   finished, select **Close** to release answer reviews. The launch-window clock
   expiring does not release solutions. Closing is irreversible from a secrecy
   perspective because authorized students can retain review information once
   displayed.

Answer selection, navigation, autosave, integrity events, and timer updates are
local and must remain responsive during a coordinator outage. A student can
resume only on the same computer and keeps the original deadline.

After Faculty closes an assessment, only students with an accepted submission
can review it. A submitting student may sign in again on another enrolled lab
client; the coordinator reauthorizes access and releases the frozen review keys
over the authenticated TLS connection. The client shows questions in that
student's saved randomized order, their selected or unanswered state, the
correct choice, and frozen solution steps. It does not store decrypted reviews
in the client database. Releases created before the review compartment was
introduced remain usable for scores but show “Detailed review unavailable.”

## Submission queues and outage recovery

After submit or expiry, the client must show `sealed_pending` and “answers are
safe.” The sealed attempt is no longer editable. The outbox retries with the
same idempotency identity until the coordinator acknowledges it; a lost HTTP
acknowledgment is safe because replay returns the same receipt.

If the coordinator fails:

1. Do not delete client state, cached packs, or ProgramData.
2. Confirm affected clients show `sealed_pending` rather than an editable
   attempt.
3. Restore the coordinator service using the same database, secrets, releases,
   and hostname certificate.
4. Leave clients running or restart them normally. Outboxes drain
   automatically.
5. Confirm the faculty queue returns to zero and each client displays the score
   only after acknowledgment.
6. If a client shows Faculty intervention, collect the diagnostic reference and
   logs; do not edit SQLite by hand.

## Machine failure, void, and one replacement attempt

If the original lab computer is unusable, Faculty opens the exact attempt,
selects **Void**, supplies a bounded reason, and explicitly authorizes one
retake. Accepted submissions require a second explicit confirmation because
evidence already exists. The audit event records the faculty actor, reason,
attempt, and authorization. The next start consumes the one replacement
authorization atomically. Do not move an in-progress local database to another
computer.

## Backup and restore

Stop the coordinator and copy the entire
`C:\ProgramData\KSAT Coordinator` tree to protected offline storage. Include the
database, WAL state after a clean stop, releases, question assets, runtime
configuration, public export, protocol signing key, pack key, local CA/server
certificate, browser/session secrets, and enrollment authority. Restrict the
backup to the same administrators who can operate the coordinator. Never back
up only `aptitude.db` or restore it with keys from another date.

For restore, keep the failed tree for diagnosis, restore one complete matching
backup while stopped, validate `PRAGMA integrity_check`, validate the saved
runtime configuration, start on the same hostname/port, and verify the public
CA/signing fingerprint before reopening the lab. Test restore regularly on an
authorized disposable machine.

## Certificate, key, and device-registration operations

Back up before renewal. Stop the coordinator and run the documented
`--renew-certificate --confirm-renewal` command as Administrator. Renewal keeps
the local CA and protocol signing key and replaces only the hostname server
key/certificate. A deliberate CA replacement is a trust migration: reinstall
and validate the new public bundle on every client before service resumes.

Client device identities register automatically on first connection. Faculty can
revoke a computer whose installation is no longer trusted and reactivate it only
after verifying the machine. Never rotate protocol signing or pack keys
independently of a complete controlled backup/restore plan.

## Upgrade dry-run and historical preservation

Close active assessments and stop the coordinator. Record row counts for
`students`, `questions`, `tests`, `attempts`, `responses`, `exam_violations`,
`devices`, `assessment_releases`, and `submissions`; record `PRAGMA
integrity_check`. Run:

```powershell
python scripts/upgrade_distributed_assessments.py `
  "C:\ProgramData\Aptitude Lab\aptitude.db" `
  "C:\ProgramData\Aptitude Lab" --dry-run
```

This compatibility command targets the protected pre-2.0 data tree. Run it
against a complete disposable copy first; after approval, move the upgraded
data into the KSAT 2.0 coordinator root only through the documented controlled
upgrade. The dry-run may create only the coordinator OS lock file; database, keys,
packs, configuration, and backups must remain byte-identical. Review the report,
take a complete backup, then remove `--dry-run`. Repeat the integrity check and
all historical row counts. Stop and restore if any pre-existing row disappears
or changes unexpectedly.

## Reproducible load and outage gates

Fixture creation is disabled unless `KSAT_LOAD_TEST=1`. The automated isolated
gate starts a separate disposable loopback HTTPS coordinator, pins its generated
CA from a file, and loads every simulated machine through the production client
configuration/service factory. It uses production request signing/parsing,
coordinator routes, SQLite/WAL, client loopback API/runtime/store/outbox, and the
single submission writer; it never modifies a certificate store or firewall.

```powershell
$env:KSAT_LOAD_TEST = "1"
python scripts/load_distributed_assessment.py --isolated --clients 30 `
  --questions 100 --start-spread-seconds 30 --submission-spread-seconds 0 `
  --report load-report-30.json
python scripts/load_distributed_assessment.py --isolated --clients 100 `
  --questions 100 --start-spread-seconds 30 --submission-spread-seconds 0 `
  --report load-report-100.json
python scripts/load_distributed_assessment.py --isolated --outage --clients 100 `
  --questions 100 --start-spread-seconds 30 --submission-spread-seconds 0 `
  --report load-report-outage-100.json
Remove-Item Env:KSAT_LOAD_TEST
```

Every run uses a unique `LOAD-<uuid>` namespace and deletes only records proven
reachable from that namespace in one final transaction. Accept only reports
with 100 acknowledged authoritative results (or 30 for reproduction), zero
missing attempts, duplicates, corruption/signature errors, and `database is
locked` errors; local answer p99 must be below 100 ms and submission ack p95
below 10 seconds; cleanup residual rows must be zero. Latency percentiles use
the documented Hyndman-Fan type 7 linear-interpolation estimator (the default
in R and NumPy), and every report retains its sample count and maximum so an
outlier remains visible. Command-line gates always enforce both latency limits;
only explicitly marked small in-process smoke tests may disable latency
enforcement, while still reporting the measured threshold result and maximum.

External mode is an explicitly authorized server-and-client procedure; the
harness never runs a service-control command locally or remotely. Open a
separate Administrator console on the authorized coordinator before starting.
Supply the coordinator HTTPS origin, exported CA file, administrator username,
and credential environment-variable names, and include `--external
--authorize-external --outage --outage-control operator-checkpoint`. At the
outage boundary:

1. In the separate server console, stop the coordinator service and wait for it
   to finish. Return to the load harness and type exactly `STOPPED`.
2. The harness probes `/api/build` with the supplied file-pinned CA. It proceeds
   with offline submission only after observing a transport-level outage; a
   still-reachable HTTPS service fails the gate.
3. In the separate server console, start the same coordinator using the same
   database and keys. Return to the harness and type exactly `STARTED`.
4. The harness polls the same pinned-HTTPS build endpoint to a bounded deadline.
   It marks the service restarted only after successful recovery, then retries
   the locally sealed outboxes and performs owned-fixture cleanup.

Because external mode has neither a coordinator process handle nor a trusted
telemetry channel, its coordinator CPU, RSS, SQLite-byte, and writer-queue
fields are JSON `null` and `metrics_availability` is `unavailable`. Zero would
incorrectly claim a real measurement. Any setup, checkpoint, execution, or
cleanup failure produces a constant-code redacted report; exception messages,
URLs, credentials, usernames, paths, and ownership tokens are not serialized.

An authorized physical acceptance test remains separate: install both packages
on disposable Windows machines, verify the machine-root CA and firewall effects,
enroll one client, and repeat one coordinator outage/restart. Never describe the
isolated gate as proof that those operating-system changes were performed.

## Diagnostic collection

Record the UTC time, coordinator version, client version, diagnostic reference,
device label (not private key), assessment/release ID, attempt ID, and visible
state. Collect Windows Event Viewer application entries, coordinator/client
application logs, the load report, SQLite integrity result, disk-free space,
DNS resolution, and TCP reachability. Redact passwords,
cookies, bearer/session tokens, private keys, response bundles, database rows,
and question content before sharing. Preserve original files through the
institution’s protected incident channel.
