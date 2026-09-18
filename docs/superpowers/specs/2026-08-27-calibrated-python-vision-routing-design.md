# Calibrated Python/Vision Routing for Textbook Chapters

## Purpose

Create a reusable Python-first, source-verified extraction policy for textbook Chapters 5 and later. The policy uses Chapters 1–4 as a calibration corpus. Python creates the first candidate for every record; original-detail vision compares every candidate with its textbook source and only re-extracts records that do not match. It does not replace the existing V2 pipeline; it adds a parallel policy layer and leaves the current published ZIPs and paused V2 Chapter 1 work unchanged.

## Non-goals

- Publishing or changing Chapters 1–4 while calibrating.
- Using the new V2 Chapter 1 extraction output to train the policy. It remains an independent acceptance/holdout corpus.
- Treating a high heuristic score or mathematical plausibility as proof of textbook fidelity.
- Automatically changing a rule after a failure.

## Calibration corpus

Only Chapters 1–4 participate:

| Chapter | Existing package | Existing provenance |
|---|---|---|
| 1 Number System | `question-banks/ch01_number_system_complete.zip` | legacy source bank, review ledger, audit/rejection data |
| 2 HCF & LCM | `question-banks/ch02_hcf_lcm_complete.zip` | legacy source bank, review ledger, audit/rejection data |
| 3 Decimal Fractions | `question-banks/ch03_decimal_fractions_complete.zip` | legacy source bank, review ledger, audit/rejection data |
| 4 Simplification | `question-banks/ch04_simplification_complete.zip` | legacy source bank, review ledger, audit/rejection data |

The calibration runner creates a fresh raw Python baseline from the source PDF using source page/answer/solution association but with legacy correction text and review overrides disabled.  Existing ZIPs and corrected ledgers are comparison evidence only; they are not the raw baseline and never hide a Python failure.

## Source-verification contract

Python is a first draft, never the final source authority. For every record, a coding-agent vision review receives the original textbook question/options/answer-key/solution crops alongside the Python candidate. It returns exactly one of:

1. `accept_python`: every source field matches exactly and has no unresolved source exception.
2. `vision_reextract`: a text, layout, notation, answer, or solution mismatch needs a source-backed vision result.
3. `source_quarantine`: the textbook is missing, incomplete, or internally inconsistent.

Deterministic detector warnings and mathematical/logical inconsistency checks are routing signals, but source comparison is mandatory even when they pass. A source exception is preserved verbatim and never silently "corrected" from mathematical reasoning.

## Evidence and feature profile

Each raw baseline record stores immutable source/candidate hashes and a normalized feature vector covering:

- Unicode and text integrity: replacement characters, detached digit chains, operator runs, broken word order, option labels/count.
- Mathematical layout: superscript/subscript geometry, fraction bars/stacks, radicals, grouped expressions, aligned equations.
- Page geometry: multi-column boundaries, shared directions, tables, diagrams/images, cross-page/cross-column continuations, crop count.
- Provenance: answer-key association, solution association/completeness, source exceptions, candidate/app structural checks.

Feature extraction must use source PDF text/bounding-box geometry as well as the raw text. It supplies review context, catches obvious corruption early, and produces regression categories; it does not waive required source comparison.

## Calibration vision audit

For every raw Chapter 1–4 baseline record, the audit compares textbook crops for question/options/answer key/solution to the raw Python candidate. It records field verdicts and one of:

- `accept_python`
- `vision_reextract` with literal failing source/candidate evidence and routing reason
- `source_quarantine` for missing, incomplete, or internally inconsistent textbook material

This audit is evidence only; it does not silently repair raw results. `vision_reextract` creates a separate source-backed V2/vision result. All records then receive package-parser and automated app screenshot/layout checks; re-extracted/complex records retain independent source-to-app vision verification.

## Rule lifecycle

Rules are versioned and append-only diagnostics, not approval authority:

1. A general failure needs a literal calibration or later-production fixture.
2. The fixture fails before a routing-rule change.
3. The narrow diagnostic/routing rule is added and the routing-policy version is incremented.
4. All calibration records and future records matching that profile are rerun.
5. A human-reported issue follows the same path; it never directly changes published data or automatically changes a rule.

All source-comparison evidence is invalidated when its detector, raw extractor, source renderer, application renderer, or policy version changes.

## Future chapter execution

For Chapters 5+:

1. Prepare source evidence and raw Python baseline.
2. Compute feature profile and deterministic integrity/logical checks.
3. Vision compares every Python candidate to the original source crops and accepts it, routes it to vision re-extraction, or quarantines the source.
4. Run package parser and automated application screenshot/layout checks for all records; retain independent source-to-app vision verification for re-extracted/complex records.
5. Any failed deterministic/app check, source exception, or missing review blocks publication and routes/returns the record to vision or review.
6. Publish only through the existing authoritative audit/release gate.

The system does not claim that deterministic checks semantically validate a formula. Mathematical/logical checks can flag likely corruption, but original-source visual comparison is what accepts a Python candidate.

## Acceptance criteria

- Calibration covers every raw baseline record from Chapters 1–4.
- Every record has immutable source/baseline/feature/audit evidence.
- Every Python candidate has source-comparison evidence; no missing review is publishable.
- Rule changes have literal regression fixtures, policy-version changes, and matching-record reruns.
- Tests prove malformed text, broken geometry, source mismatches, unknown profiles, and later human-reported fixtures route to `vision_reextract` or `source_quarantine`.
- Existing Chapters 1–4 packages, paused V2 Chapter 1 data, and legacy pipelines are not modified by calibration.
