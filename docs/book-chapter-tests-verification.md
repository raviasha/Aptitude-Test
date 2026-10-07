# Book/chapter selection verification

Implementation branch: `codex/book-chapter-tests`.

## Verified behavior

- Existing imports can be assigned to books without reimporting questions.
- Balanced selection spans chapter banks and books; the preview and creation share one allocator.
- Capacity limits redistribute the shortfall. Difficulty filtering, stale previews, empty sources, duplicate selections and mixed payloads are checked.
- Releases keep question IDs, answers, media and passage identities distinct across banks.
- Duplication retains all source quotas. Deleting either source finds its dependent tests and preserves submitted CSV summaries.
- Migration is repeatable, backfills old response sources, and retains existing question IDs and records.
- The browser fixture covers cross-book selection, stale-response suppression, difficulty failures, conflict recovery, source-aware labels, collapse/expand, book assignment/rename, manual creation, mobile layout and existing bulk imports.

## Independent review and decisions

Faraday reviewed the full change read-only. No critical finding was reported. All actionable findings were addressed:

1. Dependency counting now resolves source dependencies in three queries, instead of querying every test once per bank. A 50-bank/100-test regression enforces a bounded query count.
2. A failed post-commit quarantine purge returns `cleanup_pending` and displays a warning. Database deletion is already committed at that point; the durable recovery record is retained. Stage/archive/commit failures still roll back and restore quarantined files.
3. Chapter checkbox names include their source bank for assistive technology.

Decisions made during implementation:

- Response-derived dependencies count even when a legacy test has no anchor bank. Deleting that bank therefore removes the dependent test instead of leaving it with missing responses; its submitted CSV summary remains archived.
- Windows-native verification commands and a manual execution ledger were used alongside the skill helpers. This affects bookkeeping, not application behavior.
- The existing source-key-based presentation repairs were not redesigned; bank-specific media and frozen-answer tests cover the new multi-source behavior.
- Building/publishing an installer and a physical-lab pilot are separate steps, not claimed by this source change.

## Test record

- Original administration/migration baseline: 34 passed.
- Combined-test and assessment/client compatibility run: 108 passed, 249 subtests passed.
- Post-review book/multi-source/admin checks: 48 passed, 112 subtests passed.
- Root application/import/media/package regressions: 84 passed, 5 subtests passed.
- Extended faculty browser fixture and bulk-import JavaScript checks: passed.
- Final Coordinator/client suite: 834 passed, 48 skipped, 595 subtests passed; 40 existing deprecation warnings. Completed on 2026-10-08 in 564.26 seconds.

All independent-review findings were addressed. No minor findings were deferred. The branch remains local, with no rebuilt executable, installer, publication, or live-lab installation.

The bare repository-wide pytest command could not collect ten textbook-extraction test modules. Missing dependencies are `pdfplumber`, `pypdf` and `jsonschema`; two extraction directories also expose the same `test_build` module name. No extraction code was changed, and this report does not claim that those suites passed.

The blocked modules are:

- `data-engineering/python_vision_calibration/tests/test_baseline.py`
- `data-engineering/python_vision_calibration/tests/test_cli.py`
- `data-engineering/textbook_chapters/tests/test_build.py`
- `data-engineering/textbook_chapters/tests/test_chapter02.py`
- `data-engineering/textbook_chapters/tests/test_chapter03.py`
- `data-engineering/textbook_chapters/tests/test_chapter04.py`
- `data-engineering/textbook_chapters/tests/test_vision_pipeline.py`
- `scripts/test_compile_logical_recovery_results.py`
- `scripts/test_generate_v2_marker_overrides.py`
- `scripts/test_run_logical_boundary_recovery.py`
