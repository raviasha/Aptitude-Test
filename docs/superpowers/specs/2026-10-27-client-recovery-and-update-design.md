# Client recovery and managed update reliability

## Intent and confirmed decisions

The lab pilot DESKTOP-9BKLL44 runs Client 2.1.5. Its single queued attempt is
sealed_pending / faculty_intervention_required with error
`attempt_identity_mismatch: The submitted attempt identity is invalid.` The
administrator confirmed these old blocked attempts are practice attempts that
may be archived and removed from the pending queue. Other PCs show the generic
Faculty assistance required page, but their underlying errors are not known.

After exploring a standalone workaround, the administrator proposed a new client
package that also addresses this blocker. The proposed delivery is Client 2.1.7,
Lab Package Builder 2.1.7, and Coordinator 2.2.6 for accurate managed-update status.
The administrator approved this combined design on 2026-10-27.

## Approaches and delivery constraint

Recommended: ship a new client with a bundled administrator recovery utility,
preserve queued answers during ordinary updates, and correct the Coordinator's
pilot/status display. An interactive same-server upgrade already supports retained
queued submissions, making it the bootstrap route for affected 2.1.5 machines.

A standalone repair utility could unblock only the confirmed stale practice
attempts on existing clients, but would leave the poor diagnostics and reporting
in place. A Coordinator-only change cannot override 2.1.5's local refusal before
it fetches update policy. Do not present a new update bundle as a way around that
refusal. Already-blocked PCs need one administrator-run installation of the new
package. Managed rollout to unblocked 2.1.5/2.1.6 PCs is conditional on the
legacy-updater handoff checks below; do not advertise it as verified yet.

## Client behavior

- A completed submission awaiting upload does not permanently block a signed,
  same-server update. Keep its sealed answers, retry/intervention state and
  enrollment. Preserve the active-test guard, maintenance exclusion, signatures,
  server binding, and authenticated storage checks.
- Do not treat absence of a logged-in student as proof that there is no active
  saved test. Check actual local state before replacement and under the lock.
- Align startup, retry, installer and health-check policies. Quiesce writers when
  taking preservation snapshots, and validate authenticated logical state across
  restart. An actual successful upload acknowledgment is a valid transition;
  arbitrary database-byte changes must not be accepted or blindly rolled back.
- Failed policy requests must surface a useful diagnostic rather than masquerade
  as current/no-update. Deferral messages must explain active tests and be retried
  when the client becomes eligible, without overlapping updater attempts.

## Faculty assistance and recovery

Provide a clearly named administrator recovery shortcut installed with the client.
It uses administrator elevation on the interactive desktop; the LocalSystem web
service must not attempt to display an elevation prompt in the user's session.
The student-facing page shows safe, specific problem categories and directs
faculty to the recovery utility. Export bounded diagnostic codes and versions,
not private keys, session tokens, student answers or arbitrary raw exception text.

The first supported repair is deliberately narrow:

1. Diagnose and preview queued sealed attempts marked faculty_intervention_required
   with the exact attempt_identity_mismatch error code.
2. Ask the administrator to select/confirm the old practice attempts. Explain that
   they will be archived and will not later upload or appear as graded results.
3. Refuse active tests. Hold maintenance exclusion, stop/quiesce the client and
   acquire its lifecycle lock. Revalidate state and the selected candidates.
4. Write and verify a protected recovery archive before touching live records.
   Include a consistent database snapshot, its matching integrity anchor, encrypted
   identity backup and a manifest identifying the selected attempts and hashes.
   Keep the archive under administrator-protected ProgramData, not public Temp.
5. Remove only the selected obsolete attempts and associated queue/response rows
   using the application's authenticated transaction mechanism. Preserve all
   unrelated attempts, enrollment, connection settings and Coordinator records.
   Never synthesize an acknowledgment or score. Verify the resulting store and
   retain an audit record and archive path. Restore service availability on exit.

Other Faculty assistance causes (invalid configuration, time/trust problems,
revoked enrollment or damaged authenticated storage) receive specific diagnostics
and supported next steps. They must not be treated as disposable attempts or
silently reset. Additional automatic repairs require evidence for that cause.
Do not claim that every generic Faculty assistance screen has been fixed.

## Coordinator and updater reporting

- Display the pilot's computer name and stable device identity after selection.
- Maintain last-contact information from successfully authenticated client calls;
  absence of update progress must not fabricate an offline diagnosis.
- Bind updater journal entries and completion reporting to the actual release and
  the same attempt ID used for download. A verified installed-version health check
  produces a durable receipt that the running client can report to the Coordinator.
- Preserve anti-stale-attempt checks and exact-version publication gating. Handle
  retries/restarts without double installation or false successful reports.
- A restored release retains its audit history. Do not automatically republish
  withdrawn releases or target additional PCs during development.

### First upgrade from 2.1.5/2.1.6

The running updater is a staged copy of the old executable. Replacing the
installed helper cannot change that running copy. Legacy install requests use a
different attempt ID from download reports, and legacy health journals are not
bound to a release/attempt. A newly installed version alone is not proof of a
completed managed installation.

Implement and test an explicit legacy handoff before publishing a managed bundle
for these source versions. It must reconcile the protected download state and
install request against the exact verified signed bundle and Coordinator target;
retain the original download attempt ID for Coordinator reporting; wait for the
old updater/installer to finish; and perform fresh version, identity/configuration,
authenticated-store and preservation checks. Do not promote an unbound legacy
`healthy` journal to a verified receipt or relax stale-attempt checks. If the
required evidence is missing or conflicting, show an actionable recovery status
and keep fleet publication disabled.

Cover both a fresh 2.1.5-to-new upgrade and a 2.1.6 installation whose Coordinator
row is still verifying with no health receipt. Use actual legacy updater code in
integration verification, not just the new updater or a mocked success response.
If a reliable handoff cannot be established, publish the interactive Client and
Builder first and explicitly require the one-time interactive upgrade for those
legacy source versions too; do not silently claim central rollout is supported.

## Verification and release

Use real authenticated store fixtures for archive/recovery tests. Cover eligible
practice attempts, unrelated pending and acknowledged attempts, active-test races,
changed selections, tampering, archive failure, transaction/anchor interruption,
idempotent rerun, and retained device identity/configuration. Verify old client
schema compatibility and no alteration of server results/CSV.

Test pending-submission preservation across installation and health verification,
including a successful acknowledgment during restart. Exercise actual Coordinator
API and client/updater status transitions, wrong release/attempt IDs, failed policy
requests, and pilot publication only after verified health. Test specific faculty
diagnostics without exposing protected information.

Build and sign new Client and Builder assets and the managed update bundle with
the existing release identities, plus the Coordinator installer for its reporting
fixes. Use new versioned GitHub assets and publish checksums/evidence; do not replace
old asset contents. Keep a physical lab pilot explicitly outstanding until the
administrator confirms installation, recovery and update completion on an actual
affected PC.

## Approval status

The archive-and-unblock disposition for old practice attempts is explicitly
approved. The combined scope was approved on 2026-10-27, including the one-time
installation requirement on already-blocked clients and the legacy handoff fallback.
