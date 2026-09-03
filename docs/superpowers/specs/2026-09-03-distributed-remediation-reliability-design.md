# Distributed Remediation Reliability Design

## Purpose

Close the three Important findings that remained after commit `aec8312` without
weakening the protected LocalSystem client authority, authenticated state model,
load thresholds, or external-action constraints.

This design adds crash recovery for the authenticated state anchor, an explicit
versioned migration for pre-anchor 2.0 client data, and truthful external outage
and resource reporting.

## Constraints

- Do not install software, change Windows services, ACLs, certificate stores, or
  firewalls, contact external machines, or use a production signing credential.
- Preserve cached packs, valid in-progress attempts, sealed-pending outbox work,
  and acknowledged attempts during an explicitly authorized legacy migration.
- Never automatically authenticate a nonempty legacy store that an ordinary lab
  user could previously modify.
- Recovery may advance an authenticated anchor by exactly one committed journal
  entry only. Larger gaps or any cryptographic/state mismatch fail closed.
- External outage reports must be based on an observed pinned-HTTPS down/up
  transition. Unsupported server metrics are unavailable, not zero.
- Failure reports must not contain credentials, ownership tokens, exception
  messages, filesystem paths, or supplied URLs.

## Authenticated state crash recovery

The SQLite transaction continues to contain both the state mutation and its HMAC
journal entry. SQLite commits before the external anchor is replaced, so a crash
can leave the database exactly one valid entry ahead of the anchor.

On open or before a later transaction, the store validates the complete journal
chain from the zero MAC and computes the digest of the current authenticated
tables. Normal operation requires the anchor to match the tail. Recovery is
permitted only when all of these conditions hold:

1. The full journal chain is contiguous and every entry MAC validates.
2. The current authenticated-table digest exactly matches the journal tail.
3. The anchor exactly matches the immediately preceding journal entry, including
   sequence, digest, and entry MAC.
4. The journal is exactly one entry ahead of the anchor.

When all four hold, the store atomically writes the existing validated tail to
the anchor and verifies the resulting anchor. It does not add or alter a journal
entry. Any other mismatch remains the existing opaque authenticated-state error.
If anchor publication reports an error on a live store after SQLite has committed,
that store records a recovery-required state. Before its next read or write
transaction, it performs this complete recovery check and repairs only the exact
validated one-entry lag; it neither rejects that recoverable state merely because
the connection stayed open nor starts another mutation over a stale anchor.

Initial creation has one separate crash case: the database can contain only the
single `initialize` journal entry while no anchor exists. Recovery is allowed
only if that entry has sequence 1, the zero previous MAC, a valid MAC, and a
digest matching the current empty authenticated state. A missing anchor with any
user state or any later journal entry fails closed during ordinary service
startup. A confirmed administrator migration retry may recover the analogous
single `legacy_v1_migration` entry over nonempty state, but only after the full
journal HMAC, current state digest, version-2 stamp, and explicit confirmation
are validated; ordinary service startup never performs that recovery.

Tests inject an anchor-write failure after SQLite commit for a mutation and for
initial creation. They prove exact forward recovery after reopen and before the
same live store's next read or write transaction, and prove rejection of a
two-entry gap, a forged tail, a mismatched current digest, and a missing anchor
over nonempty state.

## Versioned legacy client-state migration

Client state has two explicit schema generations:

- Version 1: the compatible 2.0 SQLite lifecycle schema without an authenticated
  journal/anchor.
- Version 2: the protected-service schema with an authenticated journal/anchor.

The version is stored in SQLite `PRAGMA user_version`. Databases created by the
new protected service are version 2. Existing authenticated databases from
`aec8312` that have version 0 are verified and stamped version 2 without changing
assessment data. Unauthenticated databases remain version 1.

Opening a nonempty version-1 store with an integrity key in normal service mode
performs a read-only preflight before schema migration and raises a specific
migration-required error. It does not create a journal, anchor, or version-2
stamp. Automatic service startup therefore cannot bless legacy data.

An administrator invokes the dedicated client migration operation with an exact
confirmation flag. The operation acquires the existing client process lock, uses
the protected machine identity to derive the integrity key, and acquires a SQLite
`BEGIN IMMEDIATE` write reservation before it validates the legacy database for
adoption:

- `PRAGMA quick_check` and `foreign_key_check` must be clean.
- Every cached-pack row has a canonical release UUID, SHA-256 hash, absolute
  path, boolean verification value, and valid UTC timestamp.
- Every attempt, response, integrity event, sealed bundle, and receipt passes the
  existing strict cross-record lifecycle validation.
- Every outbox row has a valid retry/time/status/error shape and exactly matches
  a sealed-pending attempt bundle.
- No unsupported schema version or existing partial journal/anchor is accepted
  as legacy.

The same immediate transaction performs semantic validation, applies every
idempotent schema change through individual transactional statements, appends a
`legacy_v1_migration` entry over the exact resulting state, and stamps version 2.
There is no `executescript` or intermediate commit, so a concurrent writer cannot
change validated state before it is authenticated and any database-stage failure
rolls the whole adoption back. The external anchor is published only after that
SQLite commit and uses the exact one-entry crash-recovery semantics. The migration
then reopens/verifies the store and returns a redaction-safe summary containing
counts only. Cached packs, in-progress attempts, sealed-pending outbox entries,
and acknowledged attempts remain byte-for-byte database records.

The Windows installer runs the migration preflight before service configuration.
Fresh/version-2 stores pass without a confirmation. A nonempty version-1 store
blocks installation unless the administrator supplied the documented explicit
legacy-state confirmation switch. Operators must stop the prior client, back up
the complete client ProgramData tree, review the retained attempt/outbox state,
then run the confirmed migration. Invalid state remains untouched for forensic
recovery with the old installation/backup; the new service is not started.

## External outage checkpoints and truthful metrics

External outage mode requires an operator checkpoint mechanism. The CLI pauses at
the outage boundary and requires the exact response `STOPPED`; the operator must
stop the supplied coordinator. A short, pinned-CA HTTPS probe must then observe a
transport-level outage. The harness performs the offline submission attempt only
after that observation.

The CLI then requires the exact response `STARTED`; the operator restarts the
same coordinator. The harness polls the pinned-CA build endpoint to a bounded
deadline and proceeds only after a successful HTTPS response. Reports set
`coordinator_service_restarted` only after both the observed down and observed up
conditions. Tests inject checkpoints that control the existing disposable HTTPS
coordinator process; no external machine is contacted.

External mode has no process handle or trusted server telemetry channel.
Therefore coordinator CPU, RSS, SQLite bytes, and maximum writer queue depth are
reported as JSON `null` with an explicit `metrics_availability` value of
`unavailable`. Isolated subprocess mode retains its real measurements.

The top-level runner writes a report for threshold failures and all expected
runtime/setup/checkpoint/cleanup failures. Unexpected failure reports use the
constant code `gate_execution_failed`, retain only safe requested dimensions and
mode flags, mark completion false, and never serialize the exception or secret
inputs. Argument-parser failures that occur before a report destination is
accepted remain ordinary CLI usage errors.

## Testing

Implementation follows three independent RED/GREEN cycles:

1. Client-store crash tests for post-commit anchor failure, initial anchor
   creation failure, and all forbidden recovery shapes.
2. Migration/store/entrypoint/installer tests covering version preflight,
   administrator confirmation, preservation of all four legacy record classes,
   validation failure with zero mutation, a concurrent writer blocked from the
   post-validation adoption window, and recoverable migration anchor failure.
3. External load tests proving real down/up observation, rejection when the
   checkpoint does not change reachability, null/unavailable metrics, and a
   redacted report for setup, checkpoint, and cleanup exceptions.

Final verification runs the focused client store/runtime/entrypoint/packaging and
distributed end-to-end suites, Python and JavaScript syntax checks, and diff
validation. The accepted outage-performance miss is documented but not optimized
or rerun in this remediation.
