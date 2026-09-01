# Task 10 Report: Loopback client application and student interface

## Scope

Implemented the installed lab-client FastAPI process and dedicated browser UI without changing the central faculty/student web application.

- `ClientServices` retains the accepted five-service boundary (`identity_store`, `store`, `runtime`, `coordinator`, and `outbox`). Additive lifecycle metadata supplies the cache directory, ownership, bounded background-prefetch policy, expected coordinator key, and narrow configuration/coordinator/outbox factories.
- An unenrolled production client keeps `runtime=None` until enrollment succeeds because the accepted `AssessmentRuntime` correctly refuses an unenrolled identity. Injected tests provide their runtime directly and never read `%ProgramData%`.
- Lifespan loads and validates protected identity, restores or expires/seals the one local active attempt, starts exactly one outbox worker only after clean recovery, and starts at most one production prefetch generation when enrolled. A newly durable sealed bundle is observed centrally and wakes the current outbox once, whether sealing was reached through expiry polling, startup/recovery, or manual submit; ordinary and duplicate polls do not hot-loop.
- Prefetch captures one immutable service/generation/stop-event tuple and checks cancellation between catalog entries and before/after blocking download and prepare stages. Shutdown and coordinator replacement set that generation's stop event and synchronize with both its thread and whole-run lock for a wait derived from the coordinator request timeout. A timeout retains the old resources and configuration rather than closing or swapping beneath live work; partial startup closes owned resources only after quiescence.
- Prefetch passes the exact Task 9 `ReleaseCatalogEntry` to `download_pack()` and the exact nested Task 8 `PublicReleaseDescriptor` to `AssessmentRuntime.prepare()`. A separate prepared-ready set prevents a failed or merely cataloged pack from requesting an attempt ticket.
- Start performs one coordinator ticket request only after content is locally prepared. Answer, violation, timer, navigation, expiry, and submit paths use only local runtime/store operations. The deterministic question position is transactionally stored as `current_question_id`, restored on the same computer after restart, validated against the authenticated order, and serialized against sealing. Submit seals before waking the sole outbox transport.
- Acknowledged polling exposes and renders only the validated coordinator `SubmissionReceipt`. Pending and intervention responses never disclose raw stored/outbox errors.

## Configurable coordinator address

The user-requested coordinator address is a machine-wide persisted setting in `%ProgramData%\KSAT Client\client-config.json` under the strict field `coordinator_base_url`.

- `ClientConfig` accepts only an absolute HTTPS URL with a sane ASCII DNS/IP host, optional valid port, no credentials, path, query, fragment, whitespace, or backslash ambiguity. It normalizes host/IP spelling and removes a trailing slash.
- `ClientConfigStore` strictly parses the complete three-field configuration (`coordinator_base_url`, `trusted_ca_path`, and `coordinator_signing_public_key_b64`), writes canonical JSON through a same-directory durable temporary file plus atomic replace, and strictly re-reads the published result. A failure before or after publication restores the prior valid file.
- `POST /api/device/coordinator` is a write-only local configuration path protected by loopback Host, same-Origin, per-process CSRF, JSON schema, and explicit confirmation. It never returns the configured URL.
- Changes are rejected while either `in_progress` or `sealed_pending` work exists. For an enrolled device, the candidate HTTPS client must complete an authenticated signed-catalog round trip before persistence. Failed validation or persistence leaves the previous session/services/configuration active.
- A successful update first quiesces the prior prefetch generation, starts the replacement outbox, atomically persists the URL, clears the old in-memory student session, stops the old outbox, swaps the coordinator, creates a fresh prefetch generation, and clears old catalog/readiness state. A busy generation returns the stable retryable `prefetch_busy` problem with zero candidate/config mutation. It never reloads, regenerates, reenrolls, or replaces the DPAPI-protected device identity.
- Initial installation can call `ClientConfigStore.save()` (or write the same strict shape) without rebuilding the executable; Task 12 can use this boundary when its installer collects the hostname and installs the CA.

## Loopback and browser security

- Production Uvicorn binds only `127.0.0.1:8010`.
- Every request rejects non-loopback/DNS-rebinding Host values. Every mutation requires the normalized Origin scheme/host/port to equal the normalized loopback Host exactly as well as the unpredictable process bootstrap CSRF token. `localhost`/`127.0.0.1` cross-pairs, malformed/opaque/missing origins, and Host tricks are rejected before route execution.
- Mutation bodies must be bounded JSON with strict, extra-forbidden Pydantic fields. Malformed, oversized, hostile-origin, hostile-host, and cross-attempt requests are rejected before side effects.
- Dynamic and static responses use `no-store`, restrictive CSP, no-sniff, deny-frame, no-referrer, permissions, opener, and resource-policy headers. No CORS is enabled.
- The browser receives no coordinator URL, CA path, device/content keys, bearer token, signed bundle, answer metadata, raw exception, or raw retry error. Unknown failures receive only an opaque `KSAT-XXXXXXXXXX` reference.
- Question and option text use DOM `textContent`, never untrusted HTML insertion. The UI provides immediate optimistic selection while retaining `Saving locally…` until the deferred local write resolves; success changes it to `Saved locally`, while failure rolls back the prior selection and retains a visible/assertive error. Polling cannot overwrite an in-flight save. Previous/Next persist position before settling navigation, restore on failure, and restart on the stored question.
- Under the controller-approved narrow Task 8 extension, `AssessmentRuntime.public_asset()` returns an immutable `PublicAsset` only for the exact active attempt and a canonical reference already present in its authenticated, schema-validated public pack. Pack open/recovery confines canonical names, requires an exact question/manifest/ZIP reference set, verifies hash-to-bytes, applies an image MIME allowlist and per/aggregate size bounds, and retains bytes only in trusted runtime memory.
- The loopback image route is exact-attempt-bound and returns only those verified bytes with the declared image content type, `nosniff`, `no-store`, same-origin resource policy, and an asset-specific sandbox CSP. Cross-attempt, absent, tampered, malformed, encoded-traversal, and unreferenced asset requests fail closed.
- Question, image-stimulus, and option media are rendered with `createElement('img')` and a strict local asset URL builder. Remote, data, traversal, malformed reference, and coordinator URL forms are never assigned to the DOM.
- Copy/cut/paste, context menu, drag/drop, print, shortcuts, focus loss, visibility loss, and full-screen exit are blocked/recorded only through the local violation route. Runtime throttling retains ownership of event deduplication.

## TDD evidence

Initial RED:

- `python -m unittest tests.test_client_app_api tests.test_client_ui_contract -v`
- 16 tests discovered: 13 intended missing-module/static errors and 3 JavaScript behavior checks skipped before the client artifacts existed.

Additional RED/GREEN cycles reproduced and fixed:

- hostile Origin/Host and cross-attempt mutations;
- offline and duplicate submit ordering (local seal before outbox wake, zero coordinator answer calls);
- corrupt recovery, intervention state, and authoritative acknowledgment polling;
- exact Task 9 entry / Task 8 descriptor preservation and failed-pack non-readiness;
- malformed, oversized, and extra JSON;
- two distinct HTTPS URLs, invalid HTTP/credential/query/host/port values, and missing configuration;
- initial configuration save, restart persistence, safe update, active/sealed update rejection, candidate connectivity failure, and unchanged identity;
- atomic replace failure and post-publication sync failure restoring the old configuration;
- partial startup single-close behavior, production transport cleanup, and SQLite corruption preventing outbox startup;
- a cataloged-but-unprepared release requesting a coordinator ticket.
- missing runtime asset access and unbound image routes/UI, followed by exact attempt binding, immutable bytes, safe MIME/size/hash validation, question/option rendering, traversal rejection, and restart recovery.

Fix Cycle 1 RED reproduced all five review findings before implementation: expiry polling sealed without waking a sleeping worker; prefetch cancellation/generation and timeout tests exposed unsafe lifecycle ownership; the store/runtime/API had no durable position; independently allowlisted Origin/Host cross-pairs were accepted; and a deferred browser write lost its visible saving state. The initial targeted RED run reported 10 failures and 3 errors across 12 top-level tests/subtests. Minimal GREEN changes then covered:

- one idempotent durable-bundle observation path, including explicit recovery and URL-swapped workers;
- five blocking/cancellation/swap/timeout prefetch lifecycle tests with no leaked thread or mixed old/new catalog;
- schema migration, restart restoration, corrupt/unknown position rejection, and concurrent position/seal serialization;
- normalized default-port/casing/IPv6 same-origin acceptance plus malformed, opaque, cross-pair, and Host-trick rejection with zero mutation;
- a deferred JavaScript promise proving optimistic selection plus persistent saving/saved/error and rollback behavior.

## Verification

- Fresh Fix Cycle 1 API/UI/runtime/store/outbox run with bundled Node enabled: 136 tests, OK in 8.284s.
- Fresh affected coordinator/auth/release/start/submission modules: 287 substantive tests, OK; the root feedback module separately ran 7 tests, OK (294 affected tests total).
- Fresh canonical discovery with bundled Node enabled: `python -m unittest discover -q` — 377 tests, OK in 62.445s.
- `python -m py_compile client_app.py ksat/client/runtime.py ksat/client/store.py tests/test_client_app_api.py tests/test_client_ui_contract.py tests/test_client_runtime.py tests/test_client_store.py` — exit 0.
- Bundled `node.exe --check static/client/app.js` — exit 0.
- `git diff --check` and the static/config leakage scan completed without an error or browser-visible coordinator value.

## Concerns

- Task 12 must apply the restrictive `%ProgramData%\KSAT Client` ACL, install the trusted CA, and use `ClientConfigStore.save()` or its exact strict file shape for first installation.
- The isolated verification environment emitted only Starlette's dependency-level deprecation notice about its `httpx` TestClient import; all 377 canonical tests passed and the Fix Cycle lifecycle tests did not hang.
- The Task 8 runtime change is intentionally narrow: it exposes no path, key, answer metadata, decrypted pack, or mutable buffer. `client_app.py` does not reopen or decrypt pack state.
