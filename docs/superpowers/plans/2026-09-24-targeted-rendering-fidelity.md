# Targeted Rendering Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native execution, or superpowers:subagent-driven-development if the user selects delegation. Steps use checkbox syntax for tracking.

**Goal:** Preserve the accepted banks while fixing reported rendering defects through shared rules, selective validation, and chapter-by-chapter publication.

**Architecture:** Extend the existing textbook_chapters_v2 pipeline with a sidecar classification registry and baseline-aware release selection. Reuse its source crops, artifact storage, audit ledger, and renderer; verify the real import path before trusting render evidence. Keep app-only corrections separate from ZIP content changes.

**Tech Stack:** Existing Python pipeline, Flask KSAT application, JavaScript frontend, unittest, Node assertions, browser screenshots, coding-agent vision.

**Spec:** docs/superpowers/specs/2026-09-24-targeted-rendering-fidelity-maintenance-design.md

## Global Constraints

- The 39 current *_all_vision_text_only.zip files are the accepted baseline.
- Classification metadata lives in a versioned pipeline-side registry rather than being inserted into every baseline archive.
- Clocks remain outside the proactive cropping exercise.
- Chapters 36–39 are processed after the earlier reported issues are resolved.
- No generative model redraws or reconstructs textbook visuals.
- The pipeline must never drop a question silently.
- Newly generated packages must be correct without question-specific runtime repair maps.
- Unchanged baseline questions are not subjected to an exhaustive vision sweep.
- Preserve textbook inconsistencies under the existing source-fidelity policy.
- Checkpoint 7e07e79 is unverified; do not assume its tests pass.
- Use the focused-fidelity worktree; preserve the broader source-render-fidelity checkpoint.
- No production database changes or EXE deployment are needed for this plan.

## Review Focus

1. Same question numbers across chapters must never collide (Task 1).
2. A changed shared crop must invalidate every consumer, including a consumer absent from the reported issue (Tasks 2 and 5).
3. Replacement of a bank with assessment/history references must preserve history or reject safely (Task 3).
4. Missing vision evidence or evidence from an earlier renderer must block the affected release (Tasks 2 and 4).
5. Archive timestamp differences alone must not trigger publication; source changes during promotion must block replacement (Task 6).

## File responsibilities

Existing modules remain authoritative: source.py creates crops; store.py hashes dependencies; audit.py records verification; render.py captures KSAT; package.py assembles packages; promote.py publishes; cli.py orchestrates.

Add registry.py for baseline inventory and per-field classification, and maintenance.py for issue scope and affected-stage selection. Add corresponding test_registry.py and test_maintenance.py. Extend existing audit, render, package, source and CLI tests where their contracts change. Avoid a parallel replacement pipeline.

Commands below run from the repository root. Set PYTHONPATH=data-engineering in the command environment for pipeline unittest invocations. Use an available Python/Node runtime; record exact runtime versions during execution.

## Task 1: Inventory and classify the accepted baseline

**Files:** Create data-engineering/textbook_chapters_v2/registry.py and tests/test_registry.py in that package; extend cli.py and README.md.

**Interfaces:** inventory_banks(bank_dir: Path) -> dict; classify_field(text: str, spatial: bool = False) -> dict. Inventory includes ZIP hashes, member-content hashes, chapter identity, stable source question identity, and field content hashes. Classification contains category, rule_version, dependency_tags, and reason.

- [ ] Inspect repository/ancestor AGENTS.md instructions and checkpoint changes. Locate the 39 ZIPs and source PDF; verify PDF SHA256 against 0723862418cd7b088341bcfc78a10745fd434b3f4db695986b1ff4f40a7223bf. Record actual counts rather than assuming the historical 5,166 total.
- [ ] Add tests for deterministic classification, cross-chapter identity, and zero baseline mutation:
```python
def test_math_categories(self):
    self.assertEqual(classify_field("112 × 5⁴")["category"], "unicode_math")
    self.assertEqual(classify_field("2.6\u03054\u0305")["category"], "structured_math")
    self.assertEqual(classify_field("2.64")["category"], "plain_text")
    self.assertEqual(classify_field("graph", spatial=True)["category"], "source_visual")
```
- [ ] Run python -m unittest textbook_chapters_v2.tests.test_registry -v; confirm new behavior fails before implementation.
- [ ] Implement deterministic proposals for stems, each option, solutions and media using the existing package schema and source identities. Preserve original text. Spatial decisions must use chapter shared-context associations; keyword matches alone flag review_needed. Store all decisions in sidecar JSON using canonical_json and atomic writes.
- [ ] Expose a read-only inventory/classify CLI operation. Run tests, classify all banks, confirm all 39 original hashes unchanged, and commit the registry plus tests. Keep generated evidence under the configured work root.

## Task 2: Select affected records and bind audit evidence

**Files:** Create maintenance.py and tests/test_maintenance.py; modify audit.py, cli.py and tests/test_audit.py.

**Interfaces:** affected_records(registry: dict, changed_tags: set[str]) -> set[str]; stages_for_failure(stage: str) -> tuple[str, ...]. Persist issue events with stage, old/new categories, rule version, dependency tags, evidence hashes, affected keys and outcome. Reuse dependency_fingerprint and AuditLedger rather than creating a competing approval store.

- [ ] Add selection tests:
```python
def test_shared_dependency_selection(self):
    registry = {
        "ch36-q0001": {"dependency_tags": ["crop:table-a"]},
        "ch36-q0002": {"dependency_tags": ["crop:table-a"]},
        "ch36-q0003": {"dependency_tags": ["crop:table-b"]},
    }
    self.assertEqual(affected_records(registry, {"crop:table-a"}),
                     {"ch36-q0001", "ch36-q0002"})
```
- [ ] Add tests that renderer changes preserve extraction artifacts, category changes invalidate downstream approval, and stale/missing screenshot or vision hashes cannot approve a changed record.
- [ ] Run the new tests to establish failures. Implement stage selection for extraction, math representation, crop/association, packaging, import/update and frontend failures. Changes to registry membership must select both old and new consumers.
- [ ] Extend the ledger with explicit baseline_accepted provenance: only records matching the inventoried baseline hashes qualify. They must never be labeled newly vision-verified. Modified records require current evidence or documented omission.
- [ ] Run python -m unittest textbook_chapters_v2.tests.test_maintenance textbook_chapters_v2.tests.test_audit textbook_chapters_v2.tests.test_feedback_loop -v; commit.

## Task 3: Resolve the two reported defects in the shared app paths

**Files:** Modify app.py, static/app.js, test_registration.py, tests/test_recurring_math.js; extend existing browser tests where suitable.

**Interfaces:** Preserve existing importer and frontend entry points. Reconcile the checkpoint's explicit bank replacement and mathText/mathEsc behavior with their actual call sites; do not create source-question repair maps.

- [ ] Run the checkpoint's existing import and recurring-math tests and record the baseline result.
- [ ] Add tests for replacement of an unused bank, duplicate source identities, failed replacement rollback and refusal/preservation when historical assessments reference the bank. Assert bank identity and counts do not duplicate.
- [ ] Retain these literal math regression assertions and add mixed marked/unmarked digit groups and escaping tests:
```javascript
assert.equal(mathText('112 × 5⁴'), '112 × 5⁴');
assert.equal(mathText('2.64'), '2.64');
assert.equal(mathText('2.6\u03054\u0305'), '2.(64)');
assert.notEqual(mathEsc('2.64'), mathEsc('2.6\u03054\u0305'));
```
- [ ] Run the added cases before changes; implement only the shared import/replacement and recurring notation corrections they demonstrate. Preserve ordinary decimals, superscripts and HTML escaping.
- [ ] Run python -m unittest test_registration -v and node tests/test_recurring_math.js. Record that current ZIP correctness does not by itself prove a live database is stale; classify stale import as confirmed only with evidence.
- [ ] Commit the tested app fixes. Do not regenerate Chapters 1 or 3 if their package content is unchanged.

## Task 4: Validate through actual package import and KSAT rendering

**Files:** Modify render.py, cli.py, tests/test_render.py and tests/test_cli.py; add literal source-backed fixtures for ch01-q0124 and ch03-q0113 under the existing test fixture conventions.

**Interfaces:** Retain render_candidate for existing callers. Add render_imported_question(package_path: Path, source_key: str, output_dir: Path, viewports: tuple[tuple[int, int], ...]) -> RenderArtifacts. It imports into an isolated application data directory, resolves the persisted question by source identity, and captures the real unanswered/submitted page.

- [ ] Inspect the current rendering harness for injected payloads that bypass import. Add a test where a package/import mismatch cannot pass merely because the renderer received a correct in-memory candidate.
- [ ] Add cases for absent assets, clipping, unavailable browser, unavailable vision, and stale evidence; all remain visibly pending/failed.
- [ ] Implement import-backed rendering using the app's existing local test setup and authentication helpers. Do not write to the user's active bank database. Hash package, imported content, renderer and screenshots into the evidence.
- [ ] Run python -m unittest textbook_chapters_v2.tests.test_render textbook_chapters_v2.tests.test_cli -v.
- [ ] Render the PDF source crops and actual KSAT pages for both regression questions. Inspect them with coding-agent vision, recording the compared paths/hashes and specific verdicts. Confirm Q124 renders 112 × 5⁴ with B=70000; Q113 B is ordinary 2.64 and D visibly recurring, with correct answer D.
- [ ] On failure, record the stage/category review, fix the shared rule, invalidate affected evidence and repeat. Commit only after the reported cases pass; preserve pending work if a required capability is unavailable.

## Task 5: Add verified shared source crops for Chapters 36–39

**Files:** Modify source.py, cli.py, candidates.py, configs/chapter-036.json through chapter-039.json, tests/test_source.py and tests/test_cli.py.

**Interfaces:** Reuse SourceCrop, prepare_source_evidence and existing display_media schema. Crop hashes act as dependency tags shared by every consuming source key.

- [ ] Inspect the checkpoint's source_segments changes. Ensure they select required spatial context rather than replacing every text question with a whole-question image.
- [ ] Add tests for one graph shared by multiple questions, adjacent unrelated context, multi-page groups, crop boundary loss and missing associations. Missing required visual content must prevent approval.
- [ ] Run the targeted tests, then implement shared asset reuse and association using existing reviewed shared_contexts. Preserve baseline text and printed answers; attach source display_media and meaningful alternative text.
- [ ] Process chapter 36, then 37, 38 and 39. For each shared context: crop the original PDF; inspect all required labels, scales, legends, units and values; render representative associated questions in KSAT; check every association mechanically. Individually compare any changed field not covered by the verified shared rendering context.
- [ ] Document coverage: group comparison approves the shared visual only, never unrelated changed text. Do not substitute a sample pass for a failed member.
- [ ] If a question cannot be resolved, record an explicit omission and source number. Use floor(0.05 × baseline chapter count) as the automatic omission cap; above it keep the chapter pending and report the decision needed.
- [ ] Run python -m unittest textbook_chapters_v2.tests.test_source textbook_chapters_v2.tests.test_cli -v; commit crop-rule changes and provenance. Package each chapter through Task 6 immediately when ready.

## Task 6: Publish changed chapters and produce the audit manifest

**Files:** Modify package.py, promote.py, audit.py, tests/test_package.py, tests/test_audit.py and README.md.

**Interfaces:** Reuse build_candidate_package and promote_candidate. Add package_content_digest(path: Path) -> str using sorted archive member names and bytes, excluding ZIP timestamps/compression metadata. Release comparison must include semantic package metadata and assets, but keep run timestamps and changing audit events outside content-change detection.

- [ ] Add tests for unchanged baseline passthrough, byte-identical content with different ZIP timestamps, changed assets, explicit omission accounting, stale vision, and a published ZIP changing between inventory and promotion.
- [ ] Run failing tests; implement baseline-plus-approved-changes packaging. Only exact accepted baseline records bypass new verification; all changed records must have current evidence. Include omission metadata without silently removing records.
- [ ] Before replacement, verify expected old hash and candidate evidence; refuse concurrent divergence. Use atomic promotion and preserve previous package/hash for rollback.
- [ ] Run python -m unittest textbook_chapters_v2.tests.test_package textbook_chapters_v2.tests.test_audit -v.
- [ ] For each ready chapter, compare content digests. Publish only changed output and record previous/new hashes, changed question keys, omissions, root causes and validation coverage. If the fix is app-only, record no ZIP regeneration required.
- [ ] Run the complete pipeline unittest suite once, plus importer and JS regression suites. Address failures attributable to this work; report unrelated failures accurately.
- [ ] Save a user-facing audit manifest and changed ZIP copies under this task's outputs directory; commit code and tracked provenance. Report which chapters changed, which remained unchanged, omissions, validation results and any pending work. Do not claim deployed application behavior without deployment.

## Self-review and execution notes

This plan supersedes the short checkpoint plan 2026-09-24-focused-fidelity-fixes.md. It reuses existing pipeline components and scopes new checks to changed records. Each task ends with a testable result; chapter generation and promotion are interleaved so one difficult chapter does not hold the others.

Resolve apparent spec ambiguity as follows: app-only visible changes trigger validation but no ZIP replacement when content is unchanged; representative visual checks cover shared assets, while every changed association gets an automated check and every independently changed text field gets its own comparison. The baseline is accepted, not retroactively certified by vision.

Recommended execution: native, in this session, with one final independent review after implementation. This minimizes repeated context setup while preserving a final check of release gates.

