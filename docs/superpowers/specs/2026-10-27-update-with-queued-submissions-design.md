# Allow client updates with queued submissions

This preliminary proposal is superseded by
`2026-10-27-client-recovery-and-update-design.md`, following the administrator's
approval to archive old practice attempts and request for a new client package
with Faculty assistance recovery. Neither proposal changes installed binaries.

## Request and evidence

The lab administrator wants to deploy updates after manually ensuring no student
is taking a test. The pilot DESKTOP-9BKLL44 runs 2.1.5 and reports
`deferred_pending_submission`. Its saved queue may be old; no evidence establishes
that the queued answers are disposable. Do not delete or acknowledge them to
unblock installation.

## Proposed behavior

Allow a signed, same-Coordinator Windows client update while completed submissions
remain queued, including submissions marked for faculty intervention. Retain the
automatic active-test check and the installation lock that prevents a test from
starting during replacement. Preserve device identity, connection configuration,
answers, sealed bundles, integrity records, and submission retry/intervention
state. Normal upload retry behavior continues after installation.

The client startup decision, privileged installer preflight and post-installation
health verification must agree on this policy. Keeping a queued submission is
healthy; losing or modifying a sealed submission is not. A genuine Coordinator
receipt delivered during the update is a valid transition and must not trigger a
false rollback merely because database bytes or the integrity anchor changed.
The implementation must compare authenticated logical records across the update
or coordinate a quiescent handoff, not simply disable data-preservation checks.

## Approaches

Recommended: permit pending submissions while preserving them and retaining
active-test and integrity safeguards. This implements the requested behavior
without treating an unattended PC as proof its saved data is expendable.

An immediate alternative already exists: interactive 2.1.6 installation preserves
pending submissions. This unblocks the current PC, but leaves the automatic-update
policy unchanged. Deleting the queue or disabling every safety check is outside
the request and unnecessary.

## Delivery constraint

The refusal occurs inside installed Client 2.1.5 before it fetches update policy.
A Coordinator-only update cannot change it. Already-blocked PCs require a one-time
administrator-run replacement client installation (or successful queue delivery).
Future automatic updates use the new policy once that client is installed.
Publish a newly numbered Client and matching Builder and signed update bundle;
do not replace the existing 2.1.6 assets with changed bytes.

## Verification

Exercise real authenticated queued state through replacement and health checks;
assert sealed answers and queue metadata survive. Cover retryable and faculty-
intervention queues, successful acknowledgment during restart, active-test races,
tampered state, wrong server, signature rejection, install failure and rollback.
Report automated verification separately from an actual lab-PC installation.

## Current status

Design proposed; no product code or release artifact changed. Two existing tests
passed on 2026-10-27: interactive installation preserves queued submissions through
configuration, while the current unattended policy still blocks them. This is
evidence for the immediate manual-upgrade option, not validation of the proposed
automatic-update change.
