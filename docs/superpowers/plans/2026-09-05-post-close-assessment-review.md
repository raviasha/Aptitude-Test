# Post-Close Assessment Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a student review the exact questions, their choices, correct choices, and frozen solution steps only after the student's submission is accepted and Faculty explicitly closes the distributed assessment.

**Architecture:** Release pack format v2 adds a separately AES-GCM-encrypted `review.json.enc` entry inside the existing encrypted and signed pack. The coordinator retains a separately wrapped review key and releases both pack keys only through a student-owned, device-signed, authenticated review grant after explicit close; the client joins public questions, accepted responses, and review records by `question_id` in the stored attempt order.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, SQLite/WAL, `cryptography` AES-GCM and Ed25519, `httpx`, vanilla JavaScript/CSS, `unittest`, Node.js UI contract tests

**Spec:** `docs/superpowers/specs/2026-09-05-post-close-assessment-review-design.md`

## Global Constraints

- Scope is post-close review for distributed faculty assessments only; random practice, pre-launch visibility, simultaneous faculty launches, and answer-option shuffling remain unchanged.
- No correct answer, solution step, content key from the review path, or review key may be returned before Faculty explicitly closes the assessment.
- A review is available only for an accepted submission owned by the currently authenticated student on an active enrolled device.
- Review records and accepted responses are joined by immutable `question_id`, never array position.
- The review must use the exact public pack and frozen private material for that release; current mutable question-bank rows are never a fallback for used releases.
- Pack format v1 remains readable for recovery and scoring, but cannot provide detailed review without frozen review material.
- Review data is re-authorized after restart or later sign-in and is not persisted as plaintext in the client database.
- All wire models reject extra fields, duplicate identifiers, malformed base64, invalid option keys, non-finite values, and inconsistent release/attempt linkage.

---

## File map

- `ksat/protocol.py`: version constants and strict review wire/content models.
- `ksat/coordinator/schema.py`: additive columns for wrapped review keys and frozen solution steps.
- `ksat/coordinator/releases.py`: review-key wrapping, review ciphertext construction, v1/v2 pack validation, and immutable snapshot insertion.
- `app.py`: convert selected source rows into frozen review records before release creation.
- `ksat/coordinator/reviews.py`: student-owned completed-attempt discovery and close-gated review-grant issuance.
- `ksat/coordinator/routes.py`: signed authenticated HTTP routes for review discovery and grants.
- `ksat/client/coordinator.py`: strict typed coordinator review calls.
- `ksat/client/runtime.py`: v1/v2 pack parsing, review-compartment decryption, and question-ID joins.
- `client_app.py`: loopback review discovery/detail routes and pack preparation.
- `static/client/app.js`: waiting, completed-assessment, and per-question review states.
- `static/client/styles.css`: review-state presentation.
- `tests/test_protocol.py`, `tests/test_assessment_releases.py`, `tests/test_coordinator_schema.py`, `tests/test_assessment_review_api.py`, `tests/test_client_coordinator.py`, `tests/test_client_runtime.py`, `tests/test_client_app_api.py`, `tests/test_client_ui_contract.py`, `tests/test_distributed_end_to_end.py`: focused and end-to-end regressions.
- `README.md`, `docs/distributed-assessment-operations.md`: Faculty close and student review operating instructions.

---

### Task 1: Versioned review protocol contracts

**Files:**
- Modify: `ksat/protocol.py:15-20,390-565`
- Modify: `tests/test_protocol.py`

**Interfaces:**
- Consumes: existing `ProtocolModel`, `canonical_json`, strict Pydantic validation, and base64 conventions.
- Produces: `CURRENT_PACK_FORMAT_VERSION`, `SUPPORTED_PACK_FORMAT_VERSIONS`, `FrozenReviewQuestion`, `ReviewContent`, `ReviewResponseEntry`, `CompletedAssessmentSummary`, `AssessmentReviewGrant`, `ReviewedQuestion`, and `AssessmentReview`.

- [ ] **Step 1: Write failing protocol tests**

Add tests that construct valid review content/grants, reject duplicate question IDs, reject blank solution steps, reject invalid answer keys, and verify that pack formats 1 and 2 are supported while new manifests default to 2:

```python
def test_review_contracts_are_strict_and_question_ids_are_unique(self):
    content = ReviewContent(questions=[
        FrozenReviewQuestion(
            question_id=7,
            correct_answer="B",
            solution_steps=["Add the two values.", "The result is 4."],
        )
    ])
    self.assertEqual(7, content.questions[0].question_id)
    with self.assertRaises(ValueError):
        ReviewContent(questions=[content.questions[0], content.questions[0]])
    with self.assertRaises(ValueError):
        FrozenReviewQuestion(question_id=7, correct_answer="Z", solution_steps=["step"])
    with self.assertRaises(ValueError):
        FrozenReviewQuestion(question_id=7, correct_answer="B", solution_steps=[" "])

def test_new_manifests_use_v2_while_v1_remains_supported(self):
    self.assertEqual(2, CURRENT_PACK_FORMAT_VERSION)
    self.assertEqual(frozenset({1, 2}), SUPPORTED_PACK_FORMAT_VERSIONS)
    manifest = ReleaseManifest(
        release_id="33333333-3333-4333-8333-333333333333",
        test_id=7,
        test_name="Aptitude",
        duration_seconds=120,
        canonical_question_ids=[3, 7],
    )
    self.assertEqual(2, manifest.pack_format_version)
```

Name the production change that makes each test pass: adding the strict models and version constants. Assert on model behavior, not mocks.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_protocol -v
```

Expected: FAIL because the review models and v2 constants do not exist.

- [ ] **Step 3: Implement the strict contracts**

In `ksat/protocol.py`, retain `PROTOCOL_VERSION = 1`, replace the single pack-version assumption, and add models after `PublicQuestion`/submission models:

```python
CURRENT_PACK_FORMAT_VERSION = 2
PACK_FORMAT_VERSION = CURRENT_PACK_FORMAT_VERSION
SUPPORTED_PACK_FORMAT_VERSIONS = frozenset({1, CURRENT_PACK_FORMAT_VERSION})
AnswerKey = Literal["A", "B", "C", "D", "E"]

class FrozenReviewQuestion(ProtocolModel):
    question_id: int = Field(gt=0)
    correct_answer: AnswerKey
    solution_steps: list[str] = Field(min_length=1, max_length=100)

    @field_validator("solution_steps")
    @classmethod
    def non_blank_steps(cls, value: list[str]) -> list[str]:
        if any(not isinstance(step, str) or not step.strip() or len(step) > 10_000 for step in value):
            raise ValueError("Review solution steps must be non-empty bounded strings.")
        return value

class ReviewContent(ProtocolModel):
    questions: list[FrozenReviewQuestion] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_questions(self) -> "ReviewContent":
        ids = [item.question_id for item in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("Review question identifiers must be unique.")
        return self

class ReviewResponseEntry(ProtocolModel):
    question_id: int = Field(gt=0)
    question_order: int = Field(ge=0)
    selected_answer: AnswerKey | None

class CompletedAssessmentSummary(ProtocolModel):
    attempt_id: str
    release_id: str
    test_id: int = Field(gt=0)
    test_name: str = Field(min_length=1, max_length=200)
    accepted_at: datetime
    score: int = Field(ge=0)
    total_questions: int = Field(gt=0, le=500)
    percentage: float = Field(ge=0, le=100)
    review_state: Literal["waiting", "available", "unavailable"]

class AssessmentReviewGrant(ProtocolModel):
    attempt_id: str
    student_id: str = Field(min_length=1, max_length=100)
    release_id: str
    content_hash: str
    content_key_b64: str
    review_key_b64: str
    responses: list[ReviewResponseEntry] = Field(min_length=1, max_length=500)

class ReviewedQuestion(ProtocolModel):
    question: PublicQuestion
    selected_answer: AnswerKey | None
    correct_answer: AnswerKey
    solution_steps: list[str] = Field(min_length=1, max_length=100)

class AssessmentReview(ProtocolModel):
    attempt_id: str
    release_id: str
    test_name: str
    questions: list[ReviewedQuestion] = Field(min_length=1, max_length=500)
```

Add model validators for canonical UUID strings, 64-character lowercase SHA-256 hashes, exact 32-byte base64 keys, aware timestamps, finite percentages, contiguous zero-based `question_order`, and unique response/question identifiers. `ReleaseManifest.pack_format_version` defaults to `CURRENT_PACK_FORMAT_VERSION`.

- [ ] **Step 4: Run protocol tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_protocol -v
```

Expected: all protocol tests PASS.

- [ ] **Step 5: Commit the protocol contracts**

```powershell
git add ksat/protocol.py tests/test_protocol.py
git commit -m "feat: define assessment review protocol"
```

---

### Task 2: Build and validate the encrypted review compartment

**Files:**
- Modify: `ksat/coordinator/schema.py:45-140`
- Modify: `ksat/coordinator/releases.py:30-105,430-900`
- Modify: `tests/test_coordinator_schema.py`
- Modify: `tests/test_assessment_releases.py`

**Interfaces:**
- Consumes: Task 1 `FrozenReviewQuestion`, `ReviewContent`, `CURRENT_PACK_FORMAT_VERSION`, `SUPPORTED_PACK_FORMAT_VERSIONS`; existing `encrypt_pack`, `decrypt_pack`, release signing, content-key wrapping, and canonical ZIP helpers.
- Produces: `wrap_release_review_key(master_key: bytes, release_id: str, review_key: bytes) -> str`, `unwrap_release_review_key(master_key: bytes, release_id: str, wrapped_b64: str) -> bytes`, and `prepare_release(..., review_questions: Sequence[FrozenReviewQuestion] | None = None) -> ReleaseSummary`.

- [ ] **Step 1: Write failing additive-schema tests**

Assert that migration is idempotent and adds nullable legacy-compatible fields:

```python
def test_review_schema_is_additive_and_idempotent(self):
    migrate_distributed_schema(self.connection)
    migrate_distributed_schema(self.connection)
    release_columns = {
        row[1] for row in self.connection.execute("PRAGMA table_info(assessment_releases)")
    }
    question_columns = {
        row[1] for row in self.connection.execute("PRAGMA table_info(release_questions)")
    }
    self.assertIn("wrapped_review_key_b64", release_columns)
    self.assertIn("solution_steps_json", question_columns)
```

- [ ] **Step 2: Write failing pack-security and immutability tests**

Extend the release fixture with two `FrozenReviewQuestion` values. After unwrapping only the content key, open the outer ZIP and assert that `review.json.enc` exists, is not valid JSON, and contains none of the plaintext step strings. Then unwrap the review key and assert that decryption with AAD `f"{release_id}:review:v1"` yields canonical `ReviewContent`. Also assert that the content key cannot decrypt the compartment, modified ciphertext fails authentication, repeated preparation preserves identical bytes, and source-row edits cannot affect the frozen review.

```python
with zipfile.ZipFile(io.BytesIO(outer_plaintext)) as archive:
    encrypted_review = archive.read("review.json.enc")
    self.assertNotIn(b"The result is 4", encrypted_review)
    self.assertRaises(json.JSONDecodeError, json.loads, encrypted_review)
review_key = unwrap_release_review_key(
    self.master_key, release.release_id, release_row["wrapped_review_key_b64"]
)
review = ReviewContent.model_validate_json(
    decrypt_pack(review_key, f"{release.release_id}:review:v1", encrypted_review),
    strict=True,
)
self.assertEqual([3, 7], [item.question_id for item in review.questions])
with self.assertRaises(ValueError):
    decrypt_pack(content_key, f"{release.release_id}:review:v1", encrypted_review)
```

- [ ] **Step 3: Run schema and release tests and verify RED**

Run:

```powershell
python -m unittest tests.test_coordinator_schema tests.test_assessment_releases -v
```

Expected: FAIL because review columns, key helpers, and the encrypted pack entry are absent.

- [ ] **Step 4: Add additive review storage**

In `migrate_distributed_schema`, add:

```python
_ensure_column(connection, "assessment_releases", "wrapped_review_key_b64 TEXT")
_ensure_column(connection, "release_questions", "solution_steps_json TEXT")
```

Do not make either field `NOT NULL`; v1 releases need to remain readable and explicitly review-unavailable.

- [ ] **Step 5: Implement domain-separated key wrapping and pack construction**

Use distinct authenticated-encryption contexts:

```python
def _review_aad(release_id: str) -> str:
    return f"{_require_stored_release_id(release_id)}:review:v1"

def _review_key_aad(release_id: str) -> str:
    return f"{_require_stored_release_id(release_id)}:review-key:v1"

def wrap_release_review_key(master_key: bytes, release_id: str, review_key: bytes) -> str:
    return base64.b64encode(
        encrypt_pack(_require_key(master_key, "Pack master key"), _review_key_aad(release_id),
                     _require_key(review_key, "Release review key"))
    ).decode("ascii")

def unwrap_release_review_key(master_key: bytes, release_id: str, wrapped_b64: str) -> bytes:
    wrapped = _strict_base64(
        wrapped_b64, length=_WRAPPED_KEY_ENVELOPE_BYTES,
        message="Wrapped release review key is invalid.",
    )
    return _require_key(
        decrypt_pack(_require_key(master_key, "Pack master key"), _review_key_aad(release_id), wrapped),
        "Release review key",
    )
```

When `review_questions` is provided, validate exact ID coverage/order against public questions, create canonical `ReviewContent`, encrypt it with a fresh 32-byte review key, add `review.json.enc` after `questions.json`, wrap the review key, and insert the solution snapshots in the same savepoint as the release row. Set manifest pack format 2. When `review_questions is None`, keep the v1 entry layout and null review fields for legacy fixtures/callers.

- [ ] **Step 6: Make pack inspection explicitly version-aware**

For version 1, require exactly `manifest.json`, `questions.json`, and manifest assets. For version 2, require `manifest.json`, `questions.json`, `review.json.enc`, and assets. Validate the v2 ciphertext by unwrapping the stored review key, decrypting with `_review_aad(release_id)`, strict-validating `ReviewContent`, and comparing IDs exactly with `manifest.canonical_question_ids`. Continue rejecting private markers from `questions.json`; do not apply the public-marker scan to opaque `review.json.enc` bytes.

All coordinator and client checks that previously used `manifest.pack_format_version == PACK_FORMAT_VERSION` must use membership in `SUPPORTED_PACK_FORMAT_VERSIONS`, while new releases use version 2.

- [ ] **Step 7: Run focused tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_coordinator_schema tests.test_assessment_releases tests.test_protocol -v
```

Expected: all focused tests PASS, including v1 compatibility and v2 confidentiality/tamper tests.

- [ ] **Step 8: Commit encrypted review packaging**

```powershell
git add ksat/coordinator/schema.py ksat/coordinator/releases.py tests/test_coordinator_schema.py tests/test_assessment_releases.py
git commit -m "feat: encrypt frozen assessment reviews"
```

---

### Task 3: Freeze production solution material at release creation

**Files:**
- Modify: `app.py:1720-1840,2120-2225`
- Modify: `tests/test_assessment_releases.py`
- Modify: `tests/test_distributed_attempt_start.py`

**Interfaces:**
- Consumes: Task 2 `prepare_release(..., review_questions=...)`; existing `display_solution_steps`, `clean_display_text`, `sample_questions`, and `public_release_material`.
- Produces: `frozen_review_material(selected: Sequence[sqlite3.Row]) -> list[FrozenReviewQuestion]` and review-enabled production/load-test releases.

- [ ] **Step 1: Write failing production-wiring tests**

Create a faculty assessment whose source question has repaired solution steps and an explanation fallback. Prepare it, mutate `questions.correct_answer`, `questions.solution_steps`, and `questions.explanation`, then decrypt the frozen review and assert the original values remain. Add a second assertion that a blank step list uses one cleaned explanation string rather than becoming empty.

```python
frozen = app.frozen_review_material(selected_rows)
self.assertEqual("B", frozen[0].correct_answer)
self.assertEqual(["First frozen step.", "Second frozen step."], frozen[0].solution_steps)
self.assertEqual(["Fallback explanation."], frozen[1].solution_steps)
```

- [ ] **Step 2: Run the production-wiring tests and verify RED**

Run:

```powershell
python -m unittest tests.test_assessment_releases tests.test_distributed_attempt_start -v
```

Expected: FAIL because `frozen_review_material` and the `review_questions` call are missing.

- [ ] **Step 3: Implement normalized frozen review material**

Add this behavior beside `public_release_material`:

```python
def frozen_review_material(selected: Sequence[sqlite3.Row]) -> list[FrozenReviewQuestion]:
    reviews = []
    for row in selected:
        steps = display_solution_steps(row["question_text"], row["solution_steps"], row["source_key"])
        if not steps:
            explanation = clean_display_text(row["explanation"]).strip()
            steps = [explanation or "No solution steps were supplied for this question."]
        reviews.append(FrozenReviewQuestion(
            question_id=row["question_id"],
            correct_answer=row["correct_answer"],
            solution_steps=steps,
        ))
    return reviews
```

Pass `review_questions=frozen_review_material(selected)` from `prepare_faculty_release`. Supply deterministic synthetic solution steps in `_create_load_test` so the load path also produces v2 packs. Keep legacy `prepare_release` test helpers valid through the optional parameter.

- [ ] **Step 4: Run focused release/start tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_assessment_releases tests.test_distributed_attempt_start -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit production snapshot wiring**

```powershell
git add app.py tests/test_assessment_releases.py tests/test_distributed_attempt_start.py
git commit -m "feat: freeze faculty solution material"
```

---

### Task 4: Authorize reviews only after accepted submission and explicit close

**Files:**
- Create: `ksat/coordinator/reviews.py`
- Create: `tests/test_assessment_review_api.py`
- Modify: `ksat/coordinator/routes.py:1-125,620-810`

**Interfaces:**
- Consumes: Task 1 review models; Task 2 content/review key unwrapping; existing `_verified_device`, `_verified_student`, coordinator config, submissions, responses, and `tests.launched` close state.
- Produces: `ReviewProblem`, `list_completed_assessments(connection, *, student_id) -> list[CompletedAssessmentSummary]`, `issue_review_grant(connection, *, attempt_id, student_id, pack_master_key) -> AssessmentReviewGrant`, `GET /api/client/v1/reviews`, and `GET /api/client/v1/reviews/{attempt_id}`.

- [ ] **Step 1: Write a real coordinator API fixture**

In `tests/test_assessment_review_api.py`, follow the existing signed-device fixture from `test_distributed_attempt_start.py`: create two students, two devices, one v2 release, one accepted submission with ordered responses, and the real FastAPI coordinator router. Use device proof plus a bearer token; patch only `utc_now` where deterministic time is needed.

- [ ] **Step 2: Write failing close-gate and ownership tests**

Cover these exact cases:

```python
def test_review_waits_for_explicit_faculty_close(self):
    before = self.device_get(f"/api/client/v1/reviews/{self.attempt_id}", student_id="S100")
    self.assertEqual(409, before.status_code)
    self.assertEqual("review_not_released", before.json()["detail"]["code"])
    with app.db() as connection:
        connection.execute("UPDATE tests SET launched=0 WHERE test_id=?", (self.test_id,))
    after = self.device_get(f"/api/client/v1/reviews/{self.attempt_id}", student_id="S100")
    self.assertEqual(200, after.status_code, after.text)
    self.assertEqual([7, 3], [item["question_id"] for item in after.json()["responses"]])

def test_review_rejects_other_student_unaccepted_and_revoked_device(self):
    self.assertEqual(404, self.device_get(
        f"/api/client/v1/reviews/{self.attempt_id}", student_id="S101"
    ).status_code)
    self.assertEqual(409, self.device_get(
        f"/api/client/v1/reviews/{self.unsubmitted_attempt_id}", student_id="S100"
    ).status_code)
    self.revoke_device("device-a")
    self.assertEqual(403, self.device_get(
        f"/api/client/v1/reviews/{self.attempt_id}", student_id="S100"
    ).status_code)
```

Also assert that the pre-close body contains neither key field nor any solution text, and that v1 completed attempts list with `review_state="unavailable"`.

- [ ] **Step 3: Run the API tests and verify RED**

Run:

```powershell
python -m unittest tests.test_assessment_review_api -v
```

Expected: FAIL because the review service and routes do not exist.

- [ ] **Step 4: Implement the focused review service**

`list_completed_assessments` selects only `attempts.status='submitted'` joined to `submissions`, the owning student, tests, and releases. It derives state exactly as follows:

```python
review_state = (
    "unavailable" if row["wrapped_review_key_b64"] is None
    else "waiting" if row["launched"]
    else "available"
)
```

`issue_review_grant` uses one query constrained by `a.attempt_id=? AND a.student_id=?`, returns 404 `review_not_found` for non-ownership, returns 409 `submission_not_accepted` unless both the attempt and submission are accepted, returns 409 `review_unavailable` for missing frozen review material, and returns 409 `review_not_released` while `tests.launched=1`. Only after all gates pass may it unwrap keys and read ordered responses.

Validate that response rows are contiguous zero-based, cover the manifest question IDs exactly once, and use permitted options from frozen scoring rows. Return:

```python
AssessmentReviewGrant(
    attempt_id=row["attempt_id"],
    student_id=row["student_id"],
    release_id=row["release_id"],
    content_hash=row["content_hash"],
    content_key_b64=base64.b64encode(content_key).decode("ascii"),
    review_key_b64=base64.b64encode(review_key).decode("ascii"),
    responses=response_entries,
)
```

- [ ] **Step 5: Add authenticated device-signed routes**

Both GET routes must call `_verified_device`, then `_verified_student`, and map `ReviewProblem.detail()` to the established strict `{code,message,retryable}` HTTP detail. They must not accept student or device identity in query/body fields.

```python
@router.get("/reviews", response_model=list[CompletedAssessmentSummary])
async def completed_reviews(request: Request):
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        device_id = await _verified_device(request, connection)
        student_id = _verified_student(request, config, device_id)
        return list_completed_assessments(connection, student_id=student_id)
    finally:
        connection.close()
```

Use the same authentication/error structure for `/reviews/{attempt_id}` and pass `config.pack_master_key` to `issue_review_grant`.

- [ ] **Step 6: Run review and existing auth/submission tests**

Run:

```powershell
python -m unittest tests.test_assessment_review_api tests.test_client_auth_api tests.test_distributed_submission -v
```

Expected: all tests PASS and no pre-close response exposes key or solution fields.

- [ ] **Step 7: Commit coordinator review authorization**

```powershell
git add ksat/coordinator/reviews.py ksat/coordinator/routes.py tests/test_assessment_review_api.py
git commit -m "feat: authorize post-close assessment reviews"
```

---

### Task 5: Parse coordinator review responses strictly on the client

**Files:**
- Modify: `ksat/client/coordinator.py:20-155,540-610`
- Modify: `tests/test_client_coordinator.py`

**Interfaces:**
- Consumes: Task 1 `CompletedAssessmentSummary` and `AssessmentReviewGrant`; existing device-proof and bearer request machinery.
- Produces: `CoordinatorClient.completed_reviews() -> list[CompletedAssessmentSummary]` and `CoordinatorClient.review(attempt_id: str) -> AssessmentReviewGrant`.

- [ ] **Step 1: Write failing strict-response tests**

Use the real `httpx.MockTransport` request verifier. Assert that both routes use device proof plus the current bearer token; valid models parse; duplicate attempts, extra keys, malformed base64, wrong student/release/hash, redirects, and noncanonical UUIDs fail with `invalid_coordinator_response`.

```python
client.login("S100", "secret")
summaries = client.completed_reviews()
self.assertEqual(self.attempt_id, summaries[0].attempt_id)
grant = client.review(self.attempt_id)
self.assertEqual(self.release_id, grant.release_id)
self.assertEqual([7, 3], [item.question_id for item in grant.responses])
```

- [ ] **Step 2: Run the coordinator-client tests and verify RED**

Run:

```powershell
python -m unittest tests.test_client_coordinator -v
```

Expected: FAIL because `completed_reviews` and `review` are undefined.

- [ ] **Step 3: Implement typed review calls**

Follow the existing strict `assessments()` pattern:

```python
def completed_reviews(self) -> list[CompletedAssessmentSummary]:
    body = self._request_bytes("GET", f"{_API_PREFIX}/reviews", bearer=True)
    value = _decode_json(body, "invalid_coordinator_response")
    if not isinstance(value, list):
        raise CoordinatorProblem("invalid_coordinator_response", "The coordinator returned an invalid response.", False)
    reviews = [
        CompletedAssessmentSummary.model_validate_json(canonical_json(item), strict=True)
        for item in value
    ]
    if len({item.attempt_id for item in reviews}) != len(reviews):
        raise CoordinatorProblem("invalid_coordinator_response", "The coordinator returned an invalid response.", False)
    return reviews

def review(self, attempt_id: str) -> AssessmentReviewGrant:
    return self._typed(
        AssessmentReviewGrant,
        self._request_bytes("GET", f"{_API_PREFIX}/reviews/{attempt_id}", bearer=True),
        exact_keys={
            "attempt_id", "student_id", "release_id", "content_hash",
            "content_key_b64", "review_key_b64", "responses",
        },
    )
```

Validate the attempt UUID before constructing the path and confirm the grant student matches `self._session.student_id`.

- [ ] **Step 4: Run the coordinator-client tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_client_coordinator -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit client transport support**

```powershell
git add ksat/client/coordinator.py tests/test_client_coordinator.py
git commit -m "feat: fetch authorized assessment reviews"
```

---

### Task 6: Decrypt and assemble reviews by question ID

**Files:**
- Modify: `ksat/client/runtime.py:20-105,137-210,575-690`
- Modify: `tests/test_client_runtime.py`

**Interfaces:**
- Consumes: Task 1 review models; Task 2 v2 pack layout and `_review_aad` string contract; Task 5 `AssessmentReviewGrant`; existing verified-pack hash, outer decryption, strict JSON, and public-question validation.
- Produces: `AssessmentRuntime.open_review(pack_path: Path, grant: AssessmentReviewGrant, *, student_id: str, test_name: str) -> AssessmentReview`.

- [ ] **Step 1: Write failing order, decryption, and tamper tests**

Build a real v2 pack with canonical questions `[3, 7]` but a grant response order `[7, 3]`. Assert review output order is `[7, 3]`, question 7 receives question 7's solution, selected `None` remains unanswered, and selected/correct keys map to their option text in the resulting `PublicQuestion.options`.

Add subtests for wrong content key, wrong review key, altered ciphertext, mismatched content hash, duplicate/missing response IDs, unexpected review IDs, invalid selected options, wrong student, and v1 pack. Each must raise `ValueError` without returning partial review data.

- [ ] **Step 2: Run runtime tests and verify RED**

Run:

```powershell
python -m unittest tests.test_client_runtime -v
```

Expected: FAIL because v2 parsing and `open_review` are absent.

- [ ] **Step 3: Make public pack parsing v1/v2-aware without reading solutions during exams**

Change `_open_pack` to recognize the exact versioned entry list and return the opaque review ciphertext as a fourth value:

```python
expected = ["manifest.json", "questions.json"]
if manifest.pack_format_version == 2:
    expected.append("review.json.enc")
expected.extend(manifest.asset_names)
if names != expected:
    raise ValueError("Assessment pack entries do not match its manifest.")
review_ciphertext = archive.read("review.json.enc") if manifest.pack_format_version == 2 else None
```

Normal `start()` and recovery paths ignore the opaque fourth value and never receive a review key. Accept only versions in `SUPPORTED_PACK_FORMAT_VERSIONS`.

- [ ] **Step 4: Implement fail-closed review assembly**

`open_review` must:

1. validate the logged-in student against `grant.student_id`;
2. call `_open_pack` with `grant.content_key_b64` and verify release/hash linkage;
3. require pack format 2 and non-null review ciphertext;
4. decode the 32-byte review key strictly;
5. decrypt with AAD `f"{grant.release_id}:review:v1"`;
6. strict-validate `ReviewContent` and canonical JSON;
7. require response IDs and frozen-review IDs to equal manifest IDs exactly;
8. iterate `grant.responses` sorted by `question_order`; and
9. build `ReviewedQuestion` values by dictionary lookup on `question_id`.

```python
review_by_id = {item.question_id: item for item in review_content.questions}
question_by_id = public_questions
questions = [
    ReviewedQuestion(
        question=question_by_id[item.question_id],
        selected_answer=item.selected_answer,
        correct_answer=review_by_id[item.question_id].correct_answer,
        solution_steps=review_by_id[item.question_id].solution_steps,
    )
    for item in sorted(grant.responses, key=lambda item: item.question_order)
]
return AssessmentReview(
    attempt_id=grant.attempt_id,
    release_id=grant.release_id,
    test_name=test_name,
    questions=questions,
)
```

Before returning, verify every non-null selected answer and correct answer exists in that question's option map.

- [ ] **Step 5: Run runtime, pack, and protocol tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_client_runtime tests.test_assessment_releases tests.test_protocol -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit local review assembly**

```powershell
git add ksat/client/runtime.py tests/test_client_runtime.py
git commit -m "feat: assemble reviews in student question order"
```

---

### Task 7: Expose review discovery and detail through the loopback client

**Files:**
- Modify: `client_app.py:710-735,1450-1540,2000-2185`
- Modify: `tests/test_client_app_api.py`

**Interfaces:**
- Consumes: Task 5 coordinator methods; Task 6 `AssessmentRuntime.open_review`; existing `_ClientContext.prefetch`, verified pack store, session, and public error mapping.
- Produces: `GET /api/reviews` and `GET /api/reviews/{attempt_id}` on the loopback client.

- [ ] **Step 1: Extend test fakes and write failing loopback tests**

Add `completed_reviews()` and `review(attempt_id)` to `FakeCoordinator`, plus `open_review(...)` to `FakeRuntime`. Test:

- waiting/available/unavailable summaries pass through without key fields;
- detail requires a logged-in session;
- waiting maps `review_not_released` without changing the acknowledged score;
- available detail prefetches a missing pack, uses the verified exact hash, calls runtime assembly once, and returns questions;
- mismatched grant student/release/hash fails before runtime decryption; and
- coordinator outage returns a retryable public problem without deleting local state.

```python
listing = self.client.get("/api/reviews", headers={"Host": "127.0.0.1:8010"})
self.assertEqual("waiting", listing.json()["reviews"][0]["review_state"])
detail = self.client.get(
    f"/api/reviews/{ATTEMPT_ID}", headers={"Host": "127.0.0.1:8010"}
)
self.assertEqual(200, detail.status_code, detail.text)
self.assertEqual([7, 3], [item["question"]["question_id"] for item in detail.json()["questions"]])
```

- [ ] **Step 2: Run loopback API tests and verify RED**

Run:

```powershell
python -m unittest tests.test_client_app_api -v
```

Expected: FAIL with 404 for the new loopback routes.

- [ ] **Step 3: Implement loopback summary and review routes**

`GET /api/reviews` requires configured services and a coordinator session, then returns `{"reviews": [...]}` with JSON-safe summaries.

`GET /api/reviews/{attempt_id}` validates the UUID, fetches the grant, confirms its student matches the current session, refreshes the release catalog if the release is not in `context.catalog`, calls `context.prefetch(grant.release_id)` when the exact hash is not verified, obtains the verified pack path, finds the matching completed summary for the immutable test name, and calls:

```python
review = current.runtime.open_review(
    pack_path,
    grant,
    student_id=current.coordinator.session.student_id,
    test_name=summary.test_name,
)
return _jsonable(review)
```

Map `review_not_released` as a normal 409 with the stable message “Review will be available after Faculty closes the assessment.” Map `review_unavailable` to “Detailed review is unavailable for this older assessment.” Preserve the existing diagnostic-reference behavior for corrupt/tampered data.

- [ ] **Step 4: Run loopback and coordinator-client tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_client_app_api tests.test_client_coordinator -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit loopback review APIs**

```powershell
git add client_app.py tests/test_client_app_api.py
git commit -m "feat: expose student review APIs"
```

---

### Task 8: Render waiting, history, and detailed review states safely

**Files:**
- Modify: `static/client/app.js:1-35,250-305,450-535`
- Modify: `static/client/styles.css`
- Modify: `tests/test_client_ui_contract.py`

**Interfaces:**
- Consumes: Task 7 loopback `/api/reviews` and `/api/reviews/{attempt_id}` payloads; existing `setSafeText`, `renderStimulus`, `assetUrl`, status announcements, and DOM-only rendering policy.
- Produces: completed-assessment cards, close-waiting state, manual/automatic availability checks, and accessible per-question review rendering.

- [ ] **Step 1: Write failing JavaScript behavior contracts**

Use the existing Node harness to require exported pure helpers. Test `reviewChoiceState(selected, correct)` for correct, incorrect, and unanswered states; test `reviewPollDelay` is bounded at 5 seconds; test solution steps remain text; and source-scan for review routes plus the continued absence of `innerHTML` and `insertAdjacentHTML`.

```javascript
const ui = require(process.argv[1]);
process.stdout.write(JSON.stringify({
  correct: ui.reviewChoiceState('B', 'B'),
  incorrect: ui.reviewChoiceState('A', 'B'),
  unanswered: ui.reviewChoiceState(null, 'B'),
  delay: ui.reviewPollDelay
}));
```

Expected values:

```python
self.assertEqual({"state": "correct", "selected": "B", "correct": "B"}, result["correct"])
self.assertEqual({"state": "incorrect", "selected": "A", "correct": "B"}, result["incorrect"])
self.assertEqual({"state": "unanswered", "selected": None, "correct": "B"}, result["unanswered"])
self.assertEqual(5000, result["delay"])
```

- [ ] **Step 2: Run UI contract tests and verify RED**

Run:

```powershell
python -m unittest tests.test_client_ui_contract -v
```

Expected: FAIL because review helpers and routes are absent.

- [ ] **Step 3: Add safe completed-review discovery**

After login, `loadAssessments()` fetches `/api/assessments` and `/api/reviews`. Render completed assessments below active choices. Each card shows score and one of:

- disabled “Waiting for Faculty to close” plus a **Check again** action for `waiting`;
- **Review answers** for `available`; or
- disabled “Detailed review unavailable” for `unavailable`.

On an acknowledged result, preserve the score and start a five-second availability timer. Stop it on logout, navigation, or once available/unavailable is known. Never request review detail while state is `waiting`.

- [ ] **Step 4: Render the per-question review with DOM APIs**

For every item returned by `/api/reviews/{attempt_id}`:

```javascript
function reviewChoiceState(selected, correct) {
  return {
    state: selected === null ? 'unanswered' : selected === correct ? 'correct' : 'incorrect',
    selected,
    correct,
  };
}
```

Create elements with `document.createElement`; assign all question text, option text, choice labels, and solution steps through `setSafeText`. Reuse safe stimulus/media renderers. Add visible labels “Your choice,” “Correct choice,” and “Solution steps.” Mark the student's option and correct option independently so an incorrect response displays both. Provide **Back to completed assessments** without entering fullscreen or enabling exam-integrity hooks.

- [ ] **Step 5: Add accessible review styling**

Add `.review-list`, `.review-question`, `.review-option`, `.review-option.student-choice`, `.review-option.correct-choice`, `.review-state-unanswered`, and `.solution-steps` rules. Do not rely on color alone: include text badges and preserve keyboard/zoom layouts at the existing mobile breakpoint.

- [ ] **Step 6: Run UI and loopback tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_client_ui_contract tests.test_client_app_api -v
```

Expected: all tests PASS with no unsafe HTML sink introduced.

- [ ] **Step 7: Commit the review UI**

```powershell
git add static/client/app.js static/client/styles.css tests/test_client_ui_contract.py
git commit -m "feat: show post-close answer reviews"
```

---

### Task 9: Verify cross-device history, legacy behavior, and end-to-end security

**Files:**
- Modify: `tests/test_distributed_end_to_end.py`
- Modify: `tests/test_distributed_migration.py`
- Modify: `README.md`
- Modify: `docs/distributed-assessment-operations.md`

**Interfaces:**
- Consumes: Tasks 1-8 complete feature.
- Produces: end-to-end proof, migration coverage, and operator/student documentation.

- [ ] **Step 1: Write failing end-to-end and migration tests**

Add a real coordinator plus two real local client stores. Student S100 completes on device A. Before Faculty close, both device A and a newly logged-in device B receive `review_not_released`. After calling the real Faculty close route, device A and device B both obtain reviews in the original `[question_id, ...]` attempt order, with identical correct/solution data and the accepted selected answers.

Add tamper branches for the inner review ciphertext and both grant keys. Assert neither client emits partial review JSON. Add a v1 database/pack fixture and assert migration preserves row counts, attempt/result access, and `review_state="unavailable"`.

- [ ] **Step 2: Run end-to-end/migration tests and verify RED if coverage exposes a gap**

Run:

```powershell
python -m unittest tests.test_distributed_end_to_end tests.test_distributed_migration -v
```

Expected before the final integration adjustments: at least one new end-to-end assertion FAILS for missing cross-device catalog preparation or legacy-state mapping. If all pass, confirm the tests exercise real pack encryption, real coordinator authorization, and two distinct client stores before proceeding.

- [ ] **Step 3: Make only integration changes required by the failing tests**

Keep fixes within the established interfaces: refresh the catalog before cross-device prefetch, preserve v1 parsing through `SUPPORTED_PACK_FORMAT_VERSIONS`, and map absent `wrapped_review_key_b64` to `unavailable`. Do not reconstruct reviews for used v1 releases from current `questions` rows.

- [ ] **Step 4: Document the close/review workflow**

Update the README and operations guide with these exact operational facts:

- submission shows a score immediately but not answers;
- Faculty must use **Close** to release review access;
- start-window expiry alone does not release solutions;
- only submitting students can review;
- review can be reopened after login on an enrolled client;
- old assessments without a frozen review compartment report detailed review unavailable; and
- closing is irreversible for review secrecy because authorized students can retain what they see.

- [ ] **Step 5: Run the complete Python and UI suite**

Run:

```powershell
python -m unittest discover -v
```

Expected: every test PASS with no errors or warnings attributable to the feature.

- [ ] **Step 6: Run repository integrity checks**

Run:

```powershell
git diff --check
python -m compileall -q app.py client_app.py ksat tests
```

Expected: both commands exit 0 with no output indicating whitespace or syntax errors.

- [ ] **Step 7: Run the Windows packaging verification path**

Follow `WINDOWS_EXE_BUILD.md` using the documented verification-only test-signing mode. Confirm both coordinator and client executables build, smoke tests pass, payload scans find no secrets or source-only data, and generated artifacts are clearly marked non-distributable unless institution-controlled production signing inputs are supplied.

- [ ] **Step 8: Commit end-to-end coverage and documentation**

```powershell
git add tests/test_distributed_end_to_end.py tests/test_distributed_migration.py README.md docs/distributed-assessment-operations.md
git commit -m "test: verify post-close review workflow"
```

- [ ] **Step 9: Perform final verification before completion**

Invoke `superpowers:verification-before-completion`, rerun the focused review tests plus `python -m unittest discover -v`, inspect `git status --short`, and report exact passing counts and any unchanged pre-existing untracked load-report artifacts. Do not claim completion from an earlier run.
