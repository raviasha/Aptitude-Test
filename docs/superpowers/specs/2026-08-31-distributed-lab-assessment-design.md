# Distributed Lab Assessment Architecture

**Date:** 2026-08-31
**Status:** Approved design, awaiting written-spec review

## Context

The current application runs the web interface and SQLite database on one central lab server. Every selected answer makes a synchronous request to that server. Student authentication also refreshes a database-backed login record during requests. With about 30 simultaneous students, answer selections became slow and some requests returned a generic error.

The revised design removes the coordinator from the answer-selection path. A managed Windows lab client runs each assessment locally, saves answers locally, and sends one final response bundle to a central coordinator. The coordinator continues to own identities, assessment definitions, answer keys, scoring, results, and audit records.

The intended deployment has at most 100 concurrent students on institution-controlled lab computers connected to the institutional network. The design prioritizes responsive exams, reliable recovery, low server cost, and straightforward operations over support for untrusted personal devices or thousands of simultaneous candidates.

## Goals

- Make answer selection and question navigation independent of network and coordinator latency.
- Support up to 100 concurrent students on an ordinary faculty coordinator computer.
- Give every student the same questions in a different deterministic question order.
- Start each student's timer when that student begins, within a 10-minute launch window.
- Recover an in-progress assessment after the client application or computer restarts on the same machine.
- Preserve and automatically retry a completed submission when the network or coordinator is unavailable.
- Keep correct answers and solutions off student clients.
- Score centrally and display the score immediately after the coordinator acknowledges a submission.
- Preserve exam-integrity events and an auditable record of faculty overrides.

## Non-goals

- Moving an in-progress attempt and its answers automatically to another computer.
- Protecting the client from an attacker with administrator-level control of a lab computer.
- Supporting student-owned or unmanaged devices in the first release.
- Running the exam without network access at login and start time.
- Accepting a client-calculated score as an authoritative result.
- Scaling the first release to thousands of simultaneous students.

## Architectural decision

Use a hybrid architecture consisting of a central coordinator and an installed lab client.

```text
Faculty coordinator
  - Accounts, devices, question banks and private answer keys
  - Immutable assessment releases and launch windows
  - Attempt tickets, scoring, results and audit records
                  |
                  | HTTPS on the institutional network
                  |
Installed lab client
  - Signed/encrypted assessment-content cache
  - Local exam runner, timer and integrity controls
  - Local SQLite attempt store and durable submission outbox
```

The coordinator is the authoritative system of record, but it does not receive answer-selection or navigation requests. The client is authoritative only for the transient state of an in-progress attempt on its registered machine. The coordinator becomes authoritative for a completed attempt when it accepts the sealed response bundle.

## Components

### Coordinator

The coordinator is installed on a stable faculty-controlled Windows computer and is reachable at a stable HTTPS address on the lab network. It provides:

- faculty and student authentication;
- registered lab-device identities;
- question-bank administration;
- private answer-key and solution storage;
- immutable assessment-release creation;
- encrypted content-pack generation and distribution;
- launch-window and eligibility enforcement;
- signed attempt-ticket issuance;
- idempotent response-bundle validation and scoring;
- result, violation, and faculty-override reporting;
- backups and recovery.

The coordinator may retain SQLite for the first release. It uses WAL mode, a busy timeout, foreign-key enforcement, short transactions, and a single application-level submission writer. No external database or message broker is required for the approved 100-student limit.

### Lab client

The lab client is installed and updated by institutional IT. It is configured with the coordinator address and receives a registered device identity. It provides:

- student sign-in and assessment discovery;
- encrypted content-pack download and verification;
- signed attempt-ticket verification;
- deterministic question-order generation;
- local assessment rendering and timer enforcement;
- transactional local answer autosave;
- local exam-integrity event recording;
- crash and restart recovery on the same computer;
- permanent sealing of completed attempts;
- durable, automatic submission retry;
- immediate result display after acknowledgment.

The client uses a local SQLite database that is separate from the coordinator database. Only one active attempt is permitted for a student and assessment, and an in-progress attempt is bound to the device on which it began.

## Assessment content and releases

When faculty finalizes an assessment, the coordinator creates an immutable assessment release. The release freezes:

- the assessment name and release identifier;
- the ordered canonical list of question identifiers;
- question text, options, stimuli, and media versions;
- the duration and launch rules;
- the content-pack hash and format version.

Correct answers, explanations, and solutions are excluded from the client content pack and remain only on the coordinator.

The coordinator creates one assessment-specific encrypted content pack shared by all eligible clients. Clients can download this pack before the launch window while idle, avoiding a large start-time transfer burst. The pack is signed so that clients reject corruption or modification. Its decryption key is not delivered until an eligible student starts the launched assessment.

If a client does not already have the required pack, it downloads and verifies it before requesting the attempt start. Download time therefore does not consume the student's assessment duration.

## Attempt ticket and randomization

After the content pack is ready, the student requests an attempt during the 10-minute start window. The coordinator atomically records the start and returns a signed attempt ticket containing at least:

- attempt identifier;
- student identifier;
- registered device identifier;
- assessment release identifier and content hash;
- issued and started timestamps;
- absolute deadline;
- deterministic question-order seed;
- content decryption material or a reference to protected material;
- ticket version and signature.

Every student receives the same canonical question identifiers. The client uses the unique seed and a specified shuffle algorithm to derive that student's question order. Answer choices are not shuffled. The coordinator stores the seed and algorithm version so faculty can reproduce the exact order later.

The start request fails when the student is ineligible, the launch window has closed, the device is not registered, the required release is unavailable, or an existing attempt disallows another start.

## Timer semantics

- Faculty launch opens a 10-minute start window.
- A student's duration starts only when the coordinator issues that student's attempt ticket.
- The deadline is `started_at + configured_duration` using coordinator time.
- The client anchors the received deadline to a monotonic local clock so wall-clock changes do not reset or extend the timer.
- Restarting the application or computer restores the same absolute deadline.
- At expiry, the client immediately seals the current saved answers and enters submission mode.
- Faculty can void a failed attempt and authorize a new attempt, but the action and reason remain in the audit record.

Independent start times naturally spread submission traffic. Burst handling remains mandatory because many students may still submit early together or reach similar deadlines.

## Local attempt lifecycle

The client persists an attempt through explicit states:

1. `prepared`: the content pack is present and verified, but no attempt has started.
2. `in_progress`: a valid ticket exists, the timer is running, and answers can change.
3. `sealed_pending`: submission or expiry has permanently locked the answers and created an outbox record.
4. `acknowledged`: the coordinator has stored the result and returned the authoritative score.

Selecting or clearing an answer writes a small local transaction. The interface updates without waiting for a network request. Navigation and exam-integrity events are local operations. A restart reconstructs the current question, saved answers, remaining time, and recorded events from the local database.

Once an attempt is sealed, neither restarting the client nor losing the network permits answers to be changed. The client may display the sealed answers as read-only while it waits to upload.

## Submission protocol

The sealed response bundle contains:

- attempt and assessment-release identifiers;
- the signed attempt ticket;
- student and device identifiers;
- content-pack hash and question-order algorithm version;
- each question identifier and selected answer, including unanswered questions;
- client start, seal, and relevant integrity timestamps;
- the exam-integrity event log;
- response-bundle format version;
- a device-backed signature or integrity proof.

The bundle does not contain a score. The coordinator validates eligibility, ticket signature, device binding, release and content hashes, question membership, option values, timer rules, bundle integrity, and prior submission status before accepting it.

Scoring compares the submitted answers with the coordinator's private answer key. The coordinator stores the responses, score, violations, and submission metadata in one transaction. It then returns an acknowledgment containing the authoritative score and result summary, which the client displays immediately.

The attempt identifier is the idempotency key. Repeating an accepted submission returns the previously stored result and cannot create a second result or change the first one.

## Submission-burst handling

A 100-student simultaneous submission is a required operating case.

- Bundles contain structured answers and events, not question media, so request bodies remain small.
- Signature, schema, and scoring work occurs outside the SQLite write transaction.
- Validated results pass through one bounded in-process submission writer.
- Each result is stored in one short transaction.
- SQLite WAL mode and a busy timeout prevent transient readers or maintenance tasks from causing immediate lock errors.
- The client retains the sealed outbox record until it receives acknowledgment.
- Timeouts, connection failures, and explicit busy responses use bounded exponential backoff with randomized jitter.
- Retries are safe because submission is idempotent.

Students may see a short `Submitting...` state during a worst-case burst, but the assessment remains locked and durable. A client must never convert a timeout into a generic terminal failure or discard the submission.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Client application closes | Resume the local in-progress attempt on the same computer with the original deadline. |
| Computer restarts | Resume from local storage on the same computer with the original deadline. |
| Network fails during exam | Continue the assessment locally without interruption. |
| Network or coordinator fails at submission | Seal locally, queue durably, and retry automatically until acknowledged. |
| Coordinator restarts | Clients continue locally; start and submission requests retry after service recovery. |
| Submission response is lost | Retry the same bundle and receive the already-recorded result. |
| Content pack is corrupt or has the wrong signature | Refuse to start and redownload the pack. |
| Local attempt data is corrupt | Refuse silent recovery, preserve diagnostics, and require faculty intervention. |
| Original computer permanently fails | Faculty voids the attempt and explicitly authorizes a fresh attempt on another computer; partial work is not transferred. |
| Timer expires while offline | Seal immediately using the original deadline and upload when connectivity returns. |

## Security model

The design assumes institution-managed Windows computers, ordinary student accounts without administrator access, and IT control over installation and updates.

- Use HTTPS for credentials, tickets, content keys, submissions, and results.
- Register each client installation with a device identity whose private material is protected by Windows facilities such as the certificate store or DPAPI.
- Sign content packs, attempt tickets, and application updates.
- Encrypt pre-staged assessment content and release its key only after an authorized start.
- Never distribute answer keys, correct-answer flags, explanations, or solutions to clients during faculty assessments.
- Bind attempts and response bundles to the student, device, release, and content hash.
- Retain a tamper-evident exam-integrity log and faculty-override audit trail.
- Enforce one authoritative result per attempt on the coordinator.

Client-side controls cannot provide a security guarantee against someone with administrator access or the ability to modify the operating system. That threat is outside the approved first-release scope and is mitigated operationally through managed lab accounts and IT policy.

## Conceptual data boundaries

The coordinator needs durable records equivalent to:

- `devices`: registered lab machines and credential status;
- `assessment_releases`: immutable release metadata, question list, hashes, duration, and shuffle version;
- `content_packs`: encrypted pack location, hash, signature, and release association;
- `attempts`: student, device, release, seed, start, deadline, state, and faculty overrides;
- `submissions`: the immutable accepted bundle and idempotency key;
- `responses`: question-level submitted choices and scoring result;
- `exam_violations`: accepted client events;
- `audit_events`: launch, void, retry authorization, and administrative changes.

The client needs local records equivalent to:

- `cached_content_packs`: encrypted pack metadata and verification status;
- `local_attempts`: ticket, release, order seed, deadline, state, and recovery metadata;
- `local_responses`: locally saved choices;
- `local_integrity_events`: locally recorded exam events;
- `submission_outbox`: sealed bundle, retry state, and coordinator acknowledgment.

Exact table and endpoint names are implementation decisions, but these ownership boundaries are architectural requirements.

## Faculty and student experience

Faculty continues to import question banks, create an assessment, choose its duration, and launch it. The coordinator shows the 10-minute start window and statuses for eligible, started, submitted, and voided attempts. It does not need live per-answer progress.

Students open the installed client, sign in, and wait for the launched assessment. The client ensures the encrypted content is present before enabling Start. After Start, the assessment behaves locally: answer selections are instant, progress is autosaved, and the timer is specific to that student. At completion, the client shows submission progress and then the server-calculated score.

Generic messages such as `Some error occurred` are not acceptable. Errors must distinguish at least authentication, start-window closure, content download or verification, device registration, local recovery, queued submission, rejected submission, and coordinator availability. Recoverable conditions must state that work is safe and what the client will do next.

## Capacity and operating cost

For the approved maximum of 100 concurrent students, an ordinary faculty computer can host the coordinator because:

- encrypted content is pre-staged and served as a shared static artifact;
- attempt start exchanges are small;
- answer selection generates no coordinator traffic;
- each student sends one small final response bundle plus safe retries;
- scoring is linear in the number of submitted questions;
- database writes are short and explicitly serialized.

The first release does not require PostgreSQL, Redis, a message broker, autoscaling, or paid cloud infrastructure. If future deployments require several thousand truly simultaneous submissions or multiple coordinator instances, the same protocol can be retained while moving authoritative data to PostgreSQL and adding a durable submission queue.

## Verification and release gates

### Functional tests

- The same release always contains the same question identifiers.
- Different seeds produce different question orders, while the same seed and shuffle version reproduce the same order.
- Option order never changes.
- Content packs contain no answer keys, solutions, or correct-answer metadata.
- Invalid pack signatures, ticket signatures, device bindings, hashes, or answer values are rejected.
- Local answer changes survive application and computer restarts.
- Restart does not extend the timer.
- Expiry seals the attempt even when offline.
- A sealed attempt cannot be edited.
- Duplicate uploads return the original result.
- Network loss and coordinator restart do not lose submissions.
- Faculty void and fresh-attempt authorization leave an audit trail.

### Performance tests

- Local answer selection and navigation complete within 100 ms under normal lab hardware.
- One hundred cached clients obtain attempt tickets during the launch window without errors.
- One hundred simultaneous valid bundles are accepted exactly once and return scores without lock errors.
- A coordinator outage during a 100-client submission burst recovers through client retries without lost or duplicated results.
- Coordinator CPU, memory, disk, and queue depth are recorded during the load test to establish operating headroom.

### Rollout

1. Produce separate coordinator and lab-client installers from the existing codebase.
2. Preserve and migrate existing faculty accounts, students, question banks, tests, attempts, and results.
3. Pilot signed content packs, device registration, and local attempts on a small set of lab computers.
4. Repeat the original approximately 30-student assessment and confirm that answer selection is local and responsive.
5. Run the automated 100-client start and simultaneous-submission tests on the intended coordinator hardware.
6. Roll out to the full lab only after recovery, security, and capacity release gates pass.

## Acceptance criteria

The design is successfully implemented when 100 managed lab clients can start within the launch window, answer entirely locally, recover on the same computer, seal and queue submissions through outages, receive immediate coordinator-scored results after acknowledgment, and complete a simultaneous-submission load test without lost, duplicated, or corrupted attempts. The coordinator must meet this requirement without paid cloud scaling services or continuously processing answer-click traffic.
