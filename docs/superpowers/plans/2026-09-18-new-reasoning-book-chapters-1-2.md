# New Reasoning Book Chapters 1-2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate separate V2 all-vision rendered-page ZIP packages for Chapters 1 and 2 of the newly selected reasoning textbook.

**Architecture:** Extend the existing chapter renderer with a selectable book profile and chapter filter. Keep the existing A Modern Approach packages unchanged; write the new packages to a separate output directory with manifests bound to the new PDF hash.

**Tech Stack:** Python, pypdfium2, Pillow, ZIP, unittest.

**Spec:** User-approved in-chat design: new textbook Chapter 1 PDF pages 5–38 and Chapter 2 PDF pages 39–58, using the existing V2 all-vision text-render package format.

## Global Constraints

- Render source pages as images only; do not add OCR, answers, or solutions.
- Preserve existing old-book output packages.
- Bind each manifest to the SHA-256 hash of the new source PDF.
- Use separate names and output location for the new textbook.

### Task 1: Add and test the new-book profile

**Files:**
- Modify: `scripts/build_logical_reasoning_vision_zips.py`
- Test: `scripts/test_build_logical_reasoning_vision_zips.py`

- [x] Add a profile for the new textbook with Chapter 1 `coding_decoding` pages 5–38 and Chapter 2 `alphabet_test` pages 39–58.
- [x] Add a chapter filter so only requested chapters are emitted.
- [x] Add tests for the profile ranges, output names, and preservation of the existing profile.
- [x] Run the focused tests and confirm they fail before implementation, then pass after implementation.

### Task 2: Build the two chapter packages

**Files:**
- Create: `question-banks/logical-reasoning-new-book-vision/logical_reasoning_new_ch01_coding_decoding_all_vision.zip`
- Create: `question-banks/logical-reasoning-new-book-vision/logical_reasoning_new_ch02_alphabet_test_all_vision.zip`
- Create: `question-banks/logical-reasoning-new-book-vision/logical_reasoning_vision_index.json`

- [x] Run the renderer with the new source PDF, new-book profile, and chapters 1–2 only.
- [x] Confirm each ZIP contains a manifest and the expected rendered page sequence.

### Task 3: Verify artifacts and regression safety

**Files:**
- No production changes.

- [x] Validate both manifests against the source hash, page ranges, and package type.
- [x] Confirm page counts are 34 and 20 respectively.
- [x] Run the focused builder tests and the existing relevant test suites.
- [x] Confirm existing old-book ZIPs are unchanged.
