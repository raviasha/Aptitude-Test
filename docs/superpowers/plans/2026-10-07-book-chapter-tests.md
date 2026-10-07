# Book-Grouped Balanced Chapter Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let faculty select chapters across books and create a randomly sampled test with a previewed, roughly equal chapter allocation, without reimporting existing banks or losing results.

**Architecture:** Add book metadata and explicit test-to-bank dependencies while retaining the existing single-bank API and saved compositions. A focused selection module calculates server-owned allocations; the Coordinator prepares its existing release format from all selected sources. The faculty UI consumes a grouped catalogue and allocation preview, leaving clients and practice creation unchanged.

**Tech Stack:** Python, FastAPI, Pydantic, SQLite, vanilla JavaScript/CSS, unittest/pytest, Node.js, Playwright with Microsoft Edge.

**Spec:** `docs/superpowers/specs/2026-10-07-book-chapter-tests-design.md` (approved 2026-10-07).

## Global Constraints

- Existing maximum of 500 questions still applies.
- Practice creation is unchanged.
- Require N to be at least the number of selected chapters so every checked chapter is represented.
- No question ID may occur twice in a test.
- Use disposable databases and browser fixtures, never the running lab's ProgramData.
- Existing published assessment packs, student accounts, attempts, CSV result archives, timing, and client enrollment are retained.
- No GitHub publication or installation is performed merely by approving this specification.
- Keep the public-question/assessment-pack schema unchanged; report any necessary client protocol expansion before proceeding.
- Work in the existing `codex/book-chapter-tests` worktree. Preserve unrelated files and previous release artifacts.

## Review Focus

- Two imports with the same chapter, source key, or passage ID must stay separate, including their media and review answers (Tasks 2–3).
- A chapter or book changed in another faculty session between preview and creation must require a refreshed preview, with no partially created test (Tasks 2–3).
- A secondary source bank must receive the same replacement/deletion protection as the legacy anchor bank, and failed cleanup must preserve data (Task 4).
- Unicode/case variations in book titles and an invalid ID in a batch assignment must not produce duplicate books or partial assignments (Task 1).
- A delayed preview response, changed filter, or navigation away from the builder must not revive obsolete selections or enable an invalid submission (Task 5).

## Files and boundaries

- Create `ksat/coordinator/question_selection.py`: chapter identity/order, balanced allocation, catalogue queries and preview fingerprints. No FastAPI or filesystem side effects.
- Create `ksat/coordinator/question_books.py`: book metadata validation, assignment/rename, and schema migration for books and test sources.
- Modify `app.py`: request models, authorized routes, import integration, saved composition handling, source lifecycle, and sampling/release integration. Do not refactor unrelated routes.
- Modify `static/app.js` and `static/faculty.css`: book assignment and the balanced builder; retain manual selection.
- Create `tests/test_question_books.py`, `tests/test_balanced_selection.py`, and `tests/test_multi_chapter_tests.py`; extend `tests/test_distributed_admin.py`, `tests/test_distributed_migration.py`, and `tests/faculty_ui_browser.cjs`.
- Update `tests/test_faculty_ui_contract.py` only for intentional UI changes; do not weaken existing bulk-upload, timing, or client regressions.

## Test commands

Run from this worktree. The project Python with test dependencies is `C:/Users/ravis/OneDrive/Documents/ChatGPT/Aptitude Test/.build-venv/Scripts/python.exe`; use that executable for every `python` command below. No dependency installation is planned.

For browser tests, set `$env:NODE_PATH='C:/Users/ravis/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'` in the test shell, then run `node tests/faculty_ui_browser.cjs`. The fixture serves disposable content on localhost, not a live Coordinator.

## Task 1: Durable book organization and source metadata

**Files:** create `ksat/coordinator/question_books.py`, `tests/test_question_books.py`; modify `app.py` schema/import/admin routes and `tests/test_distributed_migration.py`.

**Interfaces:**

- `ensure_book_schema(connection: sqlite3.Connection) -> None`: create `books(book_id, title, title_key)` with unique normalized `title_key`, nullable `question_banks.book_id`, and `test_source_banks(test_id, bank_id)` with a composite primary key. Use the repository's foreign-key/transaction conventions.
- `assign_book(connection, bank_ids: list[int], *, book_id: int | None = None, book_title: str | None = None) -> dict`: require exactly one target, validate all banks before writes, return `{book_id, title, bank_ids}`. New titles are trimmed, NFC-normalized, 1–200 characters; `title_key` uses casefold. Unknown targets raise `ValueError` for the route to map to HTTP 400.
- `rename_book(connection, book_id: int, title: str) -> dict`: preserve ID; reject a conflicting title owned by another book. Route maps a missing book to HTTP 404.
- `source_bank_ids(connection, test_id: int) -> list[int]`: sorted union of recorded associations, legacy anchor, bank-qualified saved rules, and response-question banks. This also supports legacy/directly inserted tests.
- Admin routes: `GET /api/admin/books` returns `{books:[{book_id,title}]}`; `POST /api/admin/books/assign` accepts `{bank_ids,book_id}` or `{bank_ids,book_title}`; `PATCH /api/admin/books/{book_id}` accepts `{title}`. All require admin authorization and existing CSRF handling.

- [ ] Write failing tests named `test_assign_existing_banks_without_reimport`, `test_unicode_casefold_book_identity`, `test_assignment_is_atomic_for_missing_bank`, `test_rename_preserves_identity`, and `test_book_routes_require_admin`. Assert unchanged question IDs/counts and preserved accounts; assigning `"  Book A  "` and `"book a"` reuses one ID, and NFC-equivalent titles do too.
- [ ] Add migration tests: run migration twice on an old schema, assert one association per source and unchanged saved compositions/results. Include an old retry/practice attempt containing a question from a non-anchor bank.
- [ ] Run `python -m pytest tests/test_question_books.py tests/test_distributed_migration.py -q`; verify failures identify the missing behavior, not fixture errors.
- [ ] Implement the module and routes. Backfill associations after existing starter/legacy bank initialization. Start existing banks as Unassigned; batch assignment is the deliberate non-guessing migration path. Add book ID/title to bank listings.
- [ ] Preserve `parse_question_package`'s current four-value return contract. Add `parse_question_package_with_metadata(package_file) -> tuple[str, list[dict], list[dict], int, dict]` using one internal parsing path; its metadata contains validated optional `book_title`. The old parser discards metadata. Add optional `book_title: str | None = None` to `save_question_package`; only new/unassigned imports inherit manifest titles, preserving existing assignments. Validate metadata before filesystem/database changes. Cover format-2/3 imports and malformed titles in Task 1 tests.
- [ ] Rerun Task 1 tests; expect all pass. Run `git diff --check`, then commit only Task 1 files as `feat: organize question banks by book`.

## Task 2: Balanced chapter allocation and preview

**Files:** create `ksat/coordinator/question_selection.py`, `tests/test_balanced_selection.py`; modify `app.py` preview/catalogue models and routes.

**Interfaces:**

- Chapter identity is `{bank_id: int, chapter: str}`; a catalogue row adds `{bank_name, book_id, book_title, question_count, difficulties: {level: count}}`. Normalize blank stored chapters with the existing `UNCATEGORIZED_CHAPTER` constant.
- `balanced_counts(capacities: list[int], total: int) -> list[int]`: pure equal-round allocation in supplied order, capped by capacity. Reject booleans, non-integers, nonpositive capacities, empty inputs, total outside 1–500, total below chapter count, or insufficient combined capacity with `ValueError`.
- `chapter_catalogue(connection) -> list[dict]`: all active chapter pools, merging categories within `(bank_id, chapter)` only. Sort by book title (Unassigned last), explicit leading chapter number if present; for single-chapter banks allow an explicit `Chapter N` bank label as ordering metadata; otherwise natural chapter label, then bank ID. Never treat chapter numbers as book identity.
- `preview_selection(connection, chapters: list[dict], total: int, difficulties: list[str]) -> dict`: validate unique known identities, filter capacities, apply `balanced_counts`, return `{allocations:[{bank_id,chapter,bank_name,book_id,book_title,available,quantity}],total_questions,redistributed,preview_token}`. Fingerprint canonical sorted source identities, labels, selected difficulty levels, capacities, allocations, and total; it is a change detector, not authorization.
- `GET /api/admin/chapter-catalogue` returns `{chapters:[...]}`. `POST /api/admin/tests/preview` accepts `{chapters,total_questions,difficulties}` and returns the preview. Both are admin-only; preview is read-only. Use strict Pydantic integer fields and the existing difficulty validator.

- [ ] Write tests with these exact assertions: `balanced_counts([100,100,100],50)==[17,17,16]`, `balanced_counts([4,100,100],50)==[4,23,23]`, `balanced_counts([1,2,20],10)==[1,2,7]`, and `balanced_counts([2,3],5)==[2,3]`. Assert errors for total 0/501/2.5/True, `[0,5]`, total below chapter count, and excess total.
- [ ] Add catalogue/preview fixtures with same-named chapters in two books, two categories in one chapter, and duplicate selections. Assert categories pool together, banks stay separate, difficulty changes capacity, input order does not change allocation/token, and metadata/capacity changes invalidate the token. Assert no preview writes and unauthorized access is rejected.
- [ ] Run `python -m pytest tests/test_balanced_selection.py -q`; expect the new cases to fail before implementation.
- [ ] Implement the module and routes. Bound selected chapters to 500, validate chapter names against existing length rules, and return actionable 400 errors naming unavailable sources. Keep ordering identical in catalogue and preview.
- [ ] Rerun Task 2 tests and Task 1 tests; expect all pass. Run `git diff --check`, then commit as `feat: preview balanced chapter question allocations`.

## Task 3: Persist and prepare mixed-source assessments

**Files:** modify `app.py` models, selection normalization/validation/sampling, test creation/duplication/listing, and release material; create `tests/test_multi_chapter_tests.py`.

**Interfaces:**

- New balanced create payload: `{test_name,selection_mode:"balanced",chapters,total_questions,difficulties,preview_token}`. Legacy create payload stays `{test_name,bank_id,selection_rules,composition,difficulties}` with mode omitted or `"manual"`. Reject mixed-mode fields; do not expose bank overrides in practice/manual rule models.
- Persist balanced allocations as `{bank_id,chapter,quantity,scope:"chapter"}` rules; legacy rules retain `{category,chapter,quantity}`. Keep `tests.bank_id` as the first source in stable allocation order and record all sources in `test_source_banks`.
- Extend `normalize_selection_rules`, `decode_selection_rules`, `validate_selection_rules`, and `sample_questions` without changing their existing call signatures. Only chapter-scope rules pool categories; legacy rules remain category-specific. Validate unknown scopes and require a real source bank for every chapter-scope rule.
- Test listing adds `source_banks:[{bank_id,bank_name,book_id,book_title}]` and `source_summary`; leave legacy `bank_name` present. Duration/count use the full composition.

- [ ] Write tests creating 50-question assessments from two books/three banks. Assert release counts 17/17/16, exactly 50 unique IDs, preserved categories and correct answers, full source associations, and unchanged accounts/legacy tests. Patch randomness where needed to prove sampling is invoked without probabilistic/flaky assertions.
- [ ] Add tests for same-named source keys/passages with different media, stale preview conflict (409) without test/pack residue, mixed payload rejection, legacy manual behavior, unchanged practice rules, and duplication preserving sources/quotas while preparing a separate release.
- [ ] Run `python -m pytest tests/test_multi_chapter_tests.py -q`; verify intended failures.
- [ ] Implement creation under `BEGIN IMMEDIATE`: recalculate preview, compare token, insert test/associations, prepare release. Preserve established transaction rollback/artifact cleanup. Implement bank-aware composition normalization and pool queries; include `bank_id` in sampled rows and passage grouping keys.
- [ ] Inspect client consumers of `stimulus.id`; namespace new mixed-source serialized stimulus IDs as `bank:<id>:<stimulus_id>` where needed, within existing protocol field bounds. Do not rewrite prepared releases. Check `source_key` consumers for equivalent collision risks before changing any serialized value.
- [ ] Update duplication and listing; use `source_bank_ids` from Task 1 rather than an anchor-only query. Run `python -m pytest tests/test_multi_chapter_tests.py tests/test_assessment_releases.py tests/test_protocol.py tests/test_distributed_attempt_start.py tests/test_assessment_review_api.py tests/test_sealed_assessment_review.py -q`; expect all pass.
- [ ] Run `git diff --check`, then commit as `feat: create assessments across chapter banks`.

## Task 4: Protect all source dependencies and archived results

**Files:** modify `app.py` import replacement, bank counts/deletion, test deletion and retry creation; modify `ksat/coordinator/question_books.py`; extend `tests/test_distributed_admin.py` and `tests/test_multi_chapter_tests.py`.

**Interfaces:** `dependent_test_ids(connection, bank_id: int) -> list[int]` in `question_books.py` returns unique IDs from recorded associations, anchor banks, saved bank-qualified rules, and response-question banks. Use one resolved set consistently throughout a deletion transaction; do not repeatedly query only the anchor.

- [ ] Add tests `test_secondary_source_replacement_is_blocked`, `test_secondary_source_usage_count`, and `test_delete_either_source_preserves_csv_and_other_banks`. Assert combined tests counted once, results exported once after deletion, other bank questions retained, and only affected packs removed.
- [ ] Add `test_multi_source_delete_rolls_back_on_archive_failure` and `test_multi_source_delete_restores_quarantined_assets_on_failure`; assert test, attempts, answers, sources, and assets remain after rollback. Cover a submitted zero-response attempt and a retry attempt whose source differs from its anchor.
- [ ] Run `python -m pytest tests/test_distributed_admin.py tests/test_multi_chapter_tests.py -q`; confirm new lifecycle tests fail before changing dependency queries.
- [ ] Implement dependency-aware replacement/counts/deletion, retaining current archive-before-delete and quarantine ordering. Remove source associations on test deletion; populate them for all new legacy/manual/practice/retry paths too. Keep fallback support for tests inserted directly by legacy fixtures or old tools. Use bound query parameters, not user-provided SQL fragments.
- [ ] Rerun Task 4 tests plus `tests/test_distributed_submission.py`, `tests/test_distributed_end_to_end.py`, and `tests/test_distributed_migration.py`; expect all pass. Check diff and commit as `fix: preserve history for multi-bank test dependencies`.

## Task 5: Book assignment and checkbox-based test builder

**Files:** modify `static/app.js`, `static/faculty.css`, `tests/faculty_ui_browser.cjs`, and relevant expectations in `tests/test_faculty_ui_contract.py`.

**Interfaces:** Consume Task 1 book routes, Task 2 catalogue/preview routes, and Task 3 create/list payloads exactly. Keep `importBankFiles` and its busy/retry behavior unchanged. Add named UI helpers in `static/app.js`: `renderBookChapterPicker(chapters, selectedKeys)`, `chapterSelectionKey(bankId, chapter)`, and `requestChapterPreview(state)`; the latter owns a monotonic request generation and updates only its current, connected builder.

- [ ] Extend disposable browser fixtures for books, assignment/rename, chapter catalogue, delayed previews, and create success/error. Add assertions for default balanced mode, expandable groups, visible labels, chapter/source distinction, keyboard checkbox use, and preserved selection on collapse.
- [ ] Add tests assigning multiple existing banks to an existing/new book and renaming it without reimport; malformed and HTML-like titles render safely. Verify existing batch import/progress/retry flows still pass.
- [ ] Add cross-book selection tests: request 50, display 17/17/16, change a chapter's availability to 4 and display 4/23/23. Assert total/difficulty changes invalidate pending preview, late responses are ignored, empty/insufficient selections prevent submission, and 409 preserves fields and requires refreshed preview. Navigate away during preview and assert no later DOM update/error.
- [ ] Run the browser fixture and UI contract suite; expect the new feature assertions to fail before implementation.
- [ ] Implement book assignment controls and collapsible chapter checkboxes, grouped under book headings with Unassigned last. Add labelled total/difficulty inputs and allocation/status output with accessible announcements. Disable creation while preview is invalid, pending, or submission is running. Keep the existing manual single-bank builder under an explicit alternate mode and show `source_summary` in the test library. Update deletion copy to clearly include combined tests.
- [ ] Run `node tests/faculty_ui_browser.cjs`, `node tests/test_bulk_bank_import.cjs`, and `python -m pytest tests/test_faculty_ui_contract.py -q`; expect all pass. Inspect generated desktop and narrow-screen screenshots, fix clipping/focus/label problems, and rerun. Check diff and commit as `feat: select test chapters under book headings`.

## Final verification and handoff

- [ ] Run `python -m pytest tests -q` using the specified venv, plus the faculty browser and bulk-import Node suites. Record actual passes/skips/failures; investigate regressions rather than assuming skipped platform checks passed.
- [ ] Review the full branch against the approved specification, with an independent reviewer if available, especially source deletion, migration, immutable releases, and stale-preview behavior. Resolve findings with focused regression tests.
- [ ] Recheck `git diff --check` and working-tree status. Summarize what changed, verified results, and any untested physical-lab checks. No client rebuild is expected.
- [ ] Do not install, publish to GitHub, or overwrite prior release binaries under this plan. Building/publishing a new signed Coordinator installer is a subsequent delivery action when requested.

## Execution choice

Recommended: native execution in this session, task-by-task, followed by an independent whole-branch review. These five tasks share `app.py` and source identity contracts, so sequential implementation avoids overlapping changes. Subagent-driven implementation with review after each task remains an option. Await the user's plan review and execution choice before product changes.
