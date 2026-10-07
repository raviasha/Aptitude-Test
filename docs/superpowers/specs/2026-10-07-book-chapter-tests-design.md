# Book-grouped, balanced multi-chapter tests

## Outcome and approval

The faculty member can choose chapter checkboxes under book headings, enter a total question count, inspect a roughly equal chapter allocation, and create a test containing randomly selected questions from those chapters. Selection can span books.

The user approved this approach in chat on 2026-10-07, including redistribution when a chapter has too few eligible questions, a preview, and assigning existing imports to books without reimporting. This document specifies the architecture and edge cases for review before an implementation plan is written.

## Existing behavior and scope

- `app.py` stores one `tests.bank_id`; saved composition rules identify category and chapter, but not their source bank. Each published textbook chapter is generally imported as its own bank.
- The current faculty builder in `static/app.js` selects one bank and accepts manual question counts. Its difficulty filter remains available.
- Question media is already loaded using each question's bank ID. However, sampling currently groups passages by stimulus ID alone; identical IDs from different banks must not collide.
- Import replacement checks, bank usage counts, and bank deletion currently depend on the test's single bank ID. They must become aware of all sources before multi-bank tests are enabled.
- Existing published assessment packs, student accounts, attempts, CSV result archives, timing, and client enrollment are retained. No textbook extraction work is included.

## Faculty workflow

1. Open Create an assessment and enter its name.
2. In the new default balanced-selection mode, expand book headings and check chapters. Each row shows the chapter label, source bank when needed to distinguish imports, and eligible question count.
3. Set the total question count and difficulty. Existing maximum of 500 questions still applies.
4. Review the planned count beside each chosen chapter and any shortfall redistribution notice.
5. Create the assessment. Questions are randomly sampled once when its release is prepared, using the established shared-release behavior; they are not independently redrawn for each student.

The existing manual single-bank composition workflow remains available as an alternate mode. Practice creation is unchanged.

Book headings are collapsible. Selection is preserved when headings are collapsed. All controls have visible labels and support keyboard operation. Selection and preview errors retain the form contents. Creation is disabled while a preview is pending or invalid. An older preview response must never replace a newer one.

## Book and chapter organization

Add a small books table with a stable ID and display title, and an optional book reference on question banks. A bank belongs to one book or to an explicit Unassigned group. Questions continue to belong to their original banks; organizing a bank never copies or reimports its questions.

The question-bank library gains a batch Assign book action: select imported banks, choose an existing book or enter a new title, and save. Book titles are trimmed, bounded, and matched case-insensitively to avoid accidental duplicate headings. Renaming a book keeps its ID and bank associations. Removing a bank does not remove its book or any other bank.

Future format-2/3 manifests may include an optional `book_title`; existing manifests remain valid. Explicit assignments take precedence and are preserved when replacing an otherwise replaceable bank. Existing banks start unassigned unless there is an exact, tested mapping to a known published package and bank identity. Do not infer a book merely from an author's name. Batch assignment ensures older and unfamiliar imports can be organized without a new ZIP.

A selectable chapter is identified by `(bank_id, chapter)`. All categories within that bank and chapter contribute to its question pool. Category labels are retained on the sampled questions. Equal chapter names in separate banks or books are distinct selectable sources, with source labels shown to the faculty member. An imported bank with several chapters exposes several checkboxes. Separate editions/imports are not automatically merged or deduplicated by text.

Order chapters naturally by available chapter number, then by chapter label and bank ID as a stable tie-breaker. Books are ordered by title; Unassigned appears last. Empty chapters and unsupported page-only archives cannot supply questions.

## Allocation and random selection

The server owns the allocation algorithm. The same function serves preview and creation, avoiding separate browser and server interpretations.

Given selected chapter capacities after the difficulty filter and requested total N:

- Reject an empty selection, repeated chapter identities, unknown or unavailable chapters, invalid difficulty, non-integer totals, or totals outside 1–500.
- Every selected chapter must have at least one eligible question. If a changed filter makes a selection empty, identify it and ask for a different filter or deselection; do not silently drop it.
- Require N to be at least the number of selected chapters so every checked chapter is represented.
- If combined capacity is below N, show the available total and reject creation; do not silently shorten the test.
- Distribute questions in equal rounds among chapters that still have capacity. Allocate a final incomplete round in the same stable chapter order used by the preview. Counts therefore differ by at most one among chapters not limited by capacity.
- Example: N=50 and three sufficiently large chapters produces 17, 17, 16. If the first chapter has capacity 4, the allocation becomes 4, 23, 23.
- Randomly sample without replacement within each allocated chapter pool. No question ID may occur twice in a test.

The preview contains source identities, display labels, capacities, allocated counts, total, and a fingerprint of the allocation inputs/counts. Creation recalculates within its database transaction. If availability or the resulting allocation has changed since preview, return a refresh-required conflict rather than silently creating a different composition.

Question order remains randomized with existing shared-passage grouping. Group identities include the bank ID as well as the stimulus ID. Serialized stimulus identities in new mixed-bank packs must also be disambiguated if the client groups by that field, without changing the existing client schema.

## Persisted test sources and compatibility

Keep `tests.bank_id` as the legacy anchor for existing readers, and add a test-to-bank association table with a unique `(test_id, bank_id)` pair. This table is authoritative for source dependency checks on new tests. Backfill existing tests from their anchor bank and any additional banks represented by their saved response questions. Migration must be idempotent.

Extend saved composition to carry bank-qualified chapter allocations for the new balanced mode. Legacy category/chapter rules and category-count dictionaries retain their previous meaning and use the anchor bank as their fallback source. Normalization must not merge equal chapter/category names across banks. New balanced rules pool all categories within the selected bank/chapter, rather than introducing arbitrary category quotas.

Creation inserts the test, its source associations, its allocation, and its prepared release in one transaction. Existing artifact cleanup applies on failures. Duplication copies all source associations and saved quotas, then samples a new release using the existing duplication semantics. It does not edit the source test or reshuffle an already prepared release.

Test listings show a readable multi-source summary instead of presenting the anchor bank as the only source. Counts and durations derive from all saved allocations. Existing tests remain usable before and after migration.

No client update is intended: the Coordinator still sends the same public-question and assessment-pack schema. Validate this with existing pack/client contract tests; any discovered requirement to change the client protocol must be reported before expanding scope.

## Source lifecycle and results protection

All usage counts and replacement checks consult the complete set of sources, including legacy fallback associations. Replacing any bank used by a test or attempt remains blocked under the existing history-preservation policy, including a secondary source in a mixed test.

Bank deletion retains the existing explicitly confirmed cascading behavior, but its warning and affected-test count include combined tests. Resolve the complete affected test set once, then archive submitted CSV summaries and delete associated attempts/releases/tests transactionally. Keep other banks and their questions. Delete only the requested bank's question assets and the packs belonging to removed tests, using the existing recoverable quarantine mechanism.

If one bank is used by multiple selected sources in a test, count that test once. Preserve the current `archived_results` behavior: submitted result summaries remain exportable after deleting tests or source banks. Never report a successful deletion when archival or cleanup failed.

## API and implementation boundaries

- Admin-only book listing/assignment/rename endpoints and a grouped chapter catalogue expose the metadata needed by the faculty UI.
- An admin-only allocation-preview endpoint accepts chapter identities, difficulty, and total. It has no persistent side effects.
- Extend test creation with a distinct balanced-selection payload while retaining the existing single-bank payload. Reject ambiguous requests supplying both selection modes.
- Keep allocation and source-identity logic in a focused helper module rather than duplicating it across endpoints or further expanding unrelated application code.
- Update the current UI and stylesheet only where needed for grouping, assignment, selection, preview, and source summaries.
- Escape book/chapter titles in browser rendering, bound metadata inputs, parameterize queries, and retain existing admin authorization.

## Verification and acceptance

Use disposable databases and browser fixtures, never the running lab's ProgramData.

1. Allocator tests cover exact and uneven division, capped chapters, multiple redistributions, total capacity, one chapter, invalid totals, duplicate selections, zero capacity, and difficulty filtering. Assert exact totals and no over-allocation.
2. Integration tests create a test from at least two books and three chapter banks. Verify per-chapter counts, random sampling without duplicate IDs, correct answers, category retention, media, and same-named chapters/passages across banks.
3. Migration tests load an older database, migrate twice, and verify existing tests, accounts, question IDs, source associations, and results remain intact.
4. Lifecycle tests cover duplication, launch, submission, post-close review, replacement of a secondary source, and deleting either primary or secondary source. CSV summaries must survive and unrelated banks must remain available.
5. Browser tests cover book assignment, grouped checkbox selection, cross-book selection, total/difficulty changes, allocation preview, stale-response handling, clear failures, keyboard labels, and the existing manual mode.
6. Run existing Coordinator, assessment-pack/client compatibility, import, and faculty UI regression suites. If a new Coordinator installer is subsequently built, run the existing signed-release smoke checks; do not modify or publish client binaries for this feature.

## Non-goals and review status

No changes to exam timing, independent per-student question draws, automatic content deduplication, textbook ZIP contents, client update delivery, or the historical results CSV schema. No GitHub publication or installation is performed merely by approving this specification.

Status: proposed written specification, ready for user review. Product code has not been changed for this feature.
