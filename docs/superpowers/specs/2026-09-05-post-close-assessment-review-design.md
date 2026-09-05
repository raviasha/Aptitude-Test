# Post-Close Assessment Review Design

**Date:** 2026-09-05  
**Branch:** `codex/distributed-lab-assessment`  
**Scope:** Post-close review for distributed faculty assessments only. Random practice, pre-launch visibility, and simultaneous faculty launches are explicitly excluded.

## Goal

After a student submits a faculty assessment and Faculty closes it, the student can review every question in the same order they originally saw. Each review entry shows the student's selected answer, the correct answer, and the frozen solution steps. No correct answer or solution may become decryptable on a student computer before Faculty closes the assessment.

## User experience

After the coordinator accepts a submission, the client continues to show the authoritative score. While the assessment remains open, the result screen says that answer review will become available after Faculty closes the test.

Once Faculty closes the assessment, the result screen offers **Review answers**. The review presents every question in the student's original attempt order and shows:

- the original question and answer options;
- the student's choice, or **Unanswered**;
- the correct choice and its option text; and
- the frozen solution steps.

The review remains available after signing in again and may be opened from another enrolled lab computer. Only the student who submitted the attempt can retrieve that attempt's review authorization and recorded responses.

## Pack structure and cryptography

The coordinator continues to produce one immutable assessment pack shared by all eligible clients. The pack contains:

- the existing public assessment manifest, question content, options, stimuli, and assets; and
- a new `review.json.enc` entry containing correct-answer keys and solution steps indexed by immutable `question_id`.

`review.json.enc` is encrypted independently with a random per-release review key using authenticated encryption. The review key is not derived from, included with, or recoverable from the normal assessment-content key. The outer assessment content may therefore be decrypted for an active attempt without making the review compartment readable.

The coordinator stores the review key wrapped by its existing pack master key. Client binaries contain no review key or reusable secret capable of deriving one. The existing signed release envelope covers the complete pack, including the encrypted review entry, so clients reject modification or substitution.

One encrypted review record is stored per question and keyed by `question_id`, not by list position. The solution data is therefore independent of each student's randomized order and is not duplicated for different permutations.

## Randomized-order mapping

The canonical question identifiers remain frozen in the release manifest. On attempt start, the coordinator issues a signed ticket with a unique order seed. The client deterministically shuffles the canonical identifiers and durably records the resulting order.

For review, the coordinator returns the accepted attempt's ordered response records. The client iterates that stored question order and joins three sources by `question_id`:

1. question text and options from the verified assessment pack;
2. the student's selected answer from the accepted coordinator response record; and
3. the correct answer and solution steps from the decrypted review compartment.

The client rejects incomplete, duplicate, unexpected, or mismatched question identifiers. It never joins review data by array index.

## Authorization and release gate

The coordinator exposes an authenticated, device-signed review endpoint for one submitted attempt. It authorizes a response only when all of the following are true:

- the device is actively enrolled;
- the student session is valid and belongs to the attempt's student;
- the submission has been accepted by the coordinator;
- the faculty assessment has been explicitly closed; and
- the requested release and pack hashes match the attempt ticket and stored release.

Before Faculty closes the assessment, the endpoint returns a stable `review_not_released` conflict and never returns the review key, correct answers, or solution material. Merely reaching the end of the ten-minute start window does not release solutions; the existing Faculty **Close** action is the authority boundary.

After authorization, the endpoint returns a strictly typed review grant containing the release identifier and content hash, the base64-encoded assessment-content and review keys, and the student's accepted ordered answers. Both keys are transported only through the existing authenticated TLS connection after the close gate. The review key is never placed in the pre-staged pack or attempt ticket; the content key is repeated in the grant so another enrolled computer can decrypt the outer pack without possessing the original device-bound attempt ticket. The grant is bound to the authenticated attempt and contains no data for other students.

## Client flow and persistence

The loopback client result route requests the review grant from the coordinator only after the local attempt has an acknowledged submission receipt. It verifies that the grant matches the local ticket, release, content hash, student, and question coverage before decrypting `review.json.enc`.

For same-machine review, the client uses its verified cached pack. For review on another enrolled computer, the coordinator exposes a student-owned completed-assessment list; the client downloads and verifies the immutable pack before applying the selected attempt's review grant. The client stores no plaintext review material before the close gate. After authorization, the loopback process returns the assembled review to the local UI, which can continue displaying it through a temporary network interruption. A restart or later sign-in fetches and authorizes the review again instead of persisting answer keys on disk.

The client UI polls only for review availability while the acknowledged result screen is open, using bounded intervals and stopping on logout or navigation. A manual **Check for review** action remains available after transient coordinator errors.

## Immutable solution snapshot

Release preparation freezes review content at the same time as public question content and private scoring metadata. For each selected question, the release snapshot captures:

- the correct answer key;
- normalized solution steps; and
- the existing explanation as a fallback when no structured solution steps are present.

Question-bank edits after release preparation cannot change a review. The review compartment is built from this frozen snapshot, not from live question rows.

Unused prepared releases created before this feature may be upgraded only when their source questions still exist and no attempt has been issued. Used, launched, or completed legacy releases without an immutable review snapshot are never reconstructed from mutable current question-bank data. Their result screen reports that detailed review is unavailable for that older assessment.

## Failure handling

- Before close: show the waiting message and preserve the score; do not treat the gate as a failure.
- Coordinator unavailable: keep the acknowledged score visible and allow retry without losing local state.
- Missing or invalid cached pack: re-download and verify it before review.
- Invalid review grant, authentication failure, hash mismatch, decryption failure, or question-coverage mismatch: fail closed, show a stable support message, and expose no partial answers or solutions.
- Legacy release without a frozen review snapshot: show a specific unavailable message; never use current mutable question content as a substitute.
- Revoked device or expired student session: require normal enrollment or sign-in recovery before review.

## Components affected

- `ksat/protocol.py`: strict review metadata, grant, and response models.
- `ksat/coordinator/schema.py`: additive wrapped-review-key and immutable review-snapshot storage.
- `ksat/coordinator/releases.py`: freeze, encrypt, sign, validate, and migrate review material.
- `ksat/coordinator/routes.py`: student-owned completed-assessment discovery and post-close review authorization endpoints.
- `ksat/client/coordinator.py`: strict coordinator review call and validation.
- `client_app.py`: loopback availability and review routes plus pack/grant verification.
- `static/client/app.js` and `static/client/styles.css`: waiting, retry, and per-question review UI.
- Focused coordinator, client, protocol, migration, security, UI-contract, and end-to-end tests.

## Verification strategy

Implementation follows test-driven development. Tests must first demonstrate the missing behavior, then cover:

- solution material is present only as authenticated ciphertext in a prepared or launched client pack;
- the normal attempt content key cannot decrypt the review compartment;
- unique student order is preserved while review entries match by `question_id`;
- review is denied before explicit Faculty close, after another student's login, before accepted submission, and from a revoked device;
- review succeeds after close and shows selected, unanswered, correct, and solution-step states;
- changed or deleted live question-bank rows do not alter a frozen review;
- tampered pack, grant, key, hash, and question coverage fail closed;
- acknowledged result recovery and cross-device review work after restart;
- legacy releases are upgraded only when safe and otherwise report review unavailable;
- existing assessment start, local autosave, sealing, submission retry, scoring, and result tests remain green; and
- the full release verification suite and Windows packaging checks continue to pass.

## Out of scope

- Random practice.
- Showing unlaunched faculty tests to students.
- Launching multiple faculty tests simultaneously.
- Shuffling answer-option order.
- Faculty editing a solution after an assessment release is prepared.
