# Chapters 11-15 All-Vision Text-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce source-faithful, image-free question-bank ZIPs for textbook Chapters 11 through 15.

**Architecture:** Reuse the deterministic V2 pipeline for page rendering, marker segmentation, package building, and validation orchestration. Use the vision model for every question/choice/answer/solution transcription and for every source-versus-application verification; repair only failed records and revalidate their current fingerprints.

**Tech Stack:** Python, pypdf/PyMuPDF, Playwright application rendering, Codex vision extraction and verification queues, ZIP format version 3.

**Spec:** `docs/superpowers/specs/2026-08-30-chapters-1-5-all-vision-text-only.md`

## Global Constraints

- Final questions, choices, and solutions must be software-renderable text; do not include textbook screenshots, assets, or `display_media`.
- Every packaged record must have source-bound question, answer, and solution evidence.
- Validate 100% of records against both the textbook source region and the full application card.
- Keep separate Data Sufficiency exercises outside the objective-bank scope.
- Do not fabricate records when the source is absent or ambiguous; document exclusions explicitly.

---

### Task 1: Configure Chapters 11-15

**Files:**
- Create: `data-engineering/textbook_chapters_v2/configs/chapter-011.json`
- Create: `data-engineering/textbook_chapters_v2/configs/chapter-012.json`
- Create: `data-engineering/textbook_chapters_v2/configs/chapter-013.json`
- Create: `data-engineering/textbook_chapters_v2/configs/chapter-014.json`
- Create: `data-engineering/textbook_chapters_v2/configs/chapter-015.json`

**Interfaces:**
- Consumes: the source PDF and established `ChapterConfig` schema.
- Produces: exact question/answer/solution marker regions and stable output paths.

- [ ] Create minimal configs with titles, counts, physical PDF page spans, 180 DPI source rendering, two render workers, and format-v3 output names.
- [ ] Generate marker overrides from printed question and solution numbers.
- [ ] Inspect boundary pages and correct split/continuation regions.
- [ ] Run `python -m textbook_chapters_v2 prepare --config <config>` and require the expected evidence-record count.

### Task 2: Run All-Vision Extraction

**Files:**
- Create: `tmp/textbook-v2/chapter-011/extraction-results/*.json`
- Create: `tmp/textbook-v2/chapter-012/extraction-results/*.json`
- Create: `tmp/textbook-v2/chapter-013/extraction-results/*.json`
- Create: `tmp/textbook-v2/chapter-014/extraction-results/*.json`
- Create: `tmp/textbook-v2/chapter-015/extraction-results/*.json`

**Interfaces:**
- Consumes: prepared source regions and extraction job envelopes.
- Produces: schema-valid current-fingerprint text-only extraction results.

- [ ] Generate one extraction job for every in-scope question.
- [ ] Run the Codex extraction queue with two workers.
- [ ] Require every result to match its job fingerprint and extraction schema.
- [ ] Ingest extraction results and build candidate banks.

### Task 3: Render and Verify Every Record

**Files:**
- Create: `tmp/textbook-v2/chapter-011/renders/`
- Create: `tmp/textbook-v2/chapter-012/renders/`
- Create: `tmp/textbook-v2/chapter-013/renders/`
- Create: `tmp/textbook-v2/chapter-014/renders/`
- Create: `tmp/textbook-v2/chapter-015/renders/`
- Create: matching `verification-results/*.json` directories.

**Interfaces:**
- Consumes: built candidate banks and source evidence crops.
- Produces: field verdicts for fidelity, answer mapping, readability, clipping, and source identity.

- [ ] Render every application card at 1024x768 and 1600x900.
- [ ] Generate and run a vision verification job for every record.
- [ ] Ingest verdicts and quarantine every failed current fingerprint.
- [ ] Inspect full-card renders before treating auxiliary field-crop clipping as genuine.

### Task 4: Apply Targeted Repairs

**Files:**
- Modify only failed chapter extraction records and chapter-local adjudication/config data.

**Interfaces:**
- Consumes: failed verdict descriptions and textbook source regions.
- Produces: corrected current-fingerprint records with all-pass revalidation.

- [ ] Source-adjudicate each failure.
- [ ] Correct only genuine transcription, notation, mapping, or layout defects.
- [ ] Rerender only changed records.
- [ ] Reverify all changed records and require zero unresolved failures.

### Task 5: Package and Independently Validate

**Files:**
- Create: `question-banks/ch11_percentage_all_vision_text_only.zip`
- Create: `question-banks/ch12_profit_loss_all_vision_text_only.zip`
- Create: `question-banks/ch13_ratio_proportion_all_vision_text_only.zip`
- Create: `question-banks/ch14_partnership_all_vision_text_only.zip`
- Create: `question-banks/ch15_chain_rule_all_vision_text_only.zip`

**Interfaces:**
- Consumes: approved audit ledgers and current candidate banks.
- Produces: five uploadable format-v3 ZIP packages.

- [ ] Package each chapter as soon as it completes.
- [ ] Parse every ZIP with `app.parse_question_package`.
- [ ] Assert in-scope counts: 372, 292, 255, 64, and 82. Chapters 11 and 13 exclude their separately headed Data Sufficiency exercises (18 and 10 records respectively).
- [ ] Assert zero stimuli, ZIP assets, `display_media`, `question_image`, and `solution_image`.
- [ ] Record SHA-256 hashes and report documented source exclusions or textbook conflicts.
