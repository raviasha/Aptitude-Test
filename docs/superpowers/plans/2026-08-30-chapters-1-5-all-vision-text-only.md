# Chapters 1–5 All-Vision Text-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and save one independently importable, all-vision, text-only KSAT question-bank ZIP for each of Chapters 1–5.

**Architecture:** Reuse the `textbook_chapters_v2` evidence, extraction, render, verification, audit, and packaging stages. Each chapter is an isolated release unit: source evidence is prepared, vision extraction is ingested, material fidelity is repaired, real application renders are independently reviewed, and only then is a distinctly named ZIP saved.

**Tech Stack:** Python 3.12, `textbook_chapters_v2`, FastAPI KSAT frontend, Playwright with Microsoft Edge, JSON/JSONL vision job contracts, ZIP manifest format 3.

**Spec:** `docs/superpowers/specs/2026-08-30-chapters-1-5-all-vision-text-only.md`

## Global Constraints

- Final question banks contain software-rendered text and no student-facing textbook-page images.
- The textbook PDF is authoritative; missing source content must never be invented.
- Every result binds to its exact source/job fingerprint.
- Material mathematical and structured-layout differences block packaging; cosmetic proportional-font spacing alone does not.
- Save each chapter ZIP immediately after its own release checks pass.
- Do not commit or push the source textbook PDF.

---

### Task 1: Complete and package Chapter 1

**Files:**
- Consume: `data-engineering/textbook_chapters_v2/configs/chapter-001.json`
- Consume: `tmp/all-vision-text-resume/combined-results/*.json`
- Create: `question-banks/ch01_number_system_all_vision_text_only.zip`
- Test: `data-engineering/textbook_chapters_v2/tests/test_cli.py`
- Test: `scripts/test_relabel_question_package.py`

**Interfaces:**
- Consumes: 378 validated text-only extraction results and the final Chapter 1 render queue.
- Produces: a format-3 ZIP with a distinct all-vision bank name and zero display-media assets.

- [ ] Generate the final verification queue from the completed real-application render.
- [ ] Carry forward prior independent approvals for unchanged source/candidate content under the same renderer version.
- [ ] Independently vision-review the 26 materially repaired records.
- [ ] Ingest verification and confirm the release ledger has no material quarantines.
- [ ] Package, relabel, parse with the application, and assert 378 questions, format 3, and zero image assets.
- [ ] Save the ZIP and record its SHA-256.

### Task 2: Produce Chapter 2

**Files:**
- Consume/modify: `data-engineering/textbook_chapters_v2/configs/chapter-002.json`
- Create: chapter-local extraction and verification result directories under the configured KSAT work root.
- Create: `question-banks/ch02_hcf_lcm_all_vision_text_only.zip`

**Interfaces:**
- Consumes: Chapter 2 source crops and vision job schemas from `textbook_chapters_v2`.
- Produces: a schema-valid, text-only candidate and a saved Chapter 2 ZIP.

- [ ] Prepare source evidence and confirm question-number boundaries and reviewed exclusions.
- [ ] Create all-question vision extraction jobs and validate every returned result against its fingerprint.
- [ ] Ingest, build, and run deterministic/material-fidelity checks.
- [ ] Repair only material mathematical or layout failures in the affected records.
- [ ] Render with the concurrent real-application renderer and independently verify changed/new records.
- [ ] Package, relabel, parse, assert zero images, save the ZIP, and record its SHA-256.

### Task 3: Produce Chapter 3

**Files:**
- Consume/modify: `data-engineering/textbook_chapters_v2/configs/chapter-003.json`
- Create: chapter-local extraction and verification results under the configured KSAT work root.
- Create: `question-banks/ch03_decimal_fractions_all_vision_text_only.zip`

**Interfaces:**
- Consumes: Chapter 3 source crops and the same extraction/verification contracts used by Chapter 2.
- Produces: a separately importable, text-only Chapter 3 ZIP.

- [ ] Prepare and audit Chapter 3 evidence boundaries.
- [ ] Complete fingerprint-bound all-question vision extraction.
- [ ] Ingest, build, and repair material decimal notation, superscript, fraction, or structured-work defects.
- [ ] Render and independently verify application appearance at both viewports.
- [ ] Package, relabel, parse, assert zero images, save the ZIP, and record its SHA-256.

### Task 4: Produce Chapter 4

**Files:**
- Consume/modify: `data-engineering/textbook_chapters_v2/configs/chapter-004.json`
- Create: chapter-local extraction and verification results under the configured KSAT work root.
- Create: `question-banks/ch04_simplification_all_vision_text_only.zip`

**Interfaces:**
- Consumes: Chapter 4 evidence and the proven concurrent render/vision audit pipeline.
- Produces: a separately importable, text-only Chapter 4 ZIP.

- [ ] Prepare and audit Chapter 4 evidence boundaries.
- [ ] Complete fingerprint-bound all-question vision extraction.
- [ ] Ingest, build, and repair material operator, grouping, fraction, radical, or layout defects.
- [ ] Render and independently verify application appearance at both viewports.
- [ ] Package, relabel, parse, assert zero images, save the ZIP, and record its SHA-256.

### Task 5: Produce Chapter 5 and verify the release set

**Files:**
- Consume/modify: `data-engineering/textbook_chapters_v2/configs/chapter-005.json`
- Create: chapter-local extraction and verification results under the configured KSAT work root.
- Create: `question-banks/ch05_square_roots_cube_roots_all_vision_text_only.zip`
- Verify: all Chapter 1–5 output ZIPs.

**Interfaces:**
- Consumes: Chapter 5 evidence plus the four previously saved ZIPs.
- Produces: the Chapter 5 ZIP and a final five-file validation summary.

- [ ] Prepare and audit Chapter 5 evidence boundaries.
- [ ] Complete fingerprint-bound all-question vision extraction.
- [ ] Ingest, build, and repair material radical, exponent, long-division, or structured-work defects.
- [ ] Render and independently verify application appearance at both viewports.
- [ ] Package, relabel, parse, assert zero images, save the ZIP, and record its SHA-256.
- [ ] Re-parse all five saved ZIPs and report filename, internal bank name, format version, question count, image count, and SHA-256.

## Self-review

- Spec coverage: every required extraction, rendering, vision, repair, stopping-rule, packaging, and per-chapter-save requirement maps to a task.
- Placeholder scan: no deferred or unspecified implementation step remains.
- Type/contract consistency: every chapter uses the same config, JSON result, renderer, verification, parser, and ZIP interfaces.
