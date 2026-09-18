# Logical Reasoning V2 Pipeline Design

## Goal

Publish chapter-wise, text-only question-bank ZIPs for *A Modern Approach to Verbal & Non-Verbal Reasoning* only after record-bound extraction and independent render verification against the supplied textbook PDF.

## Scope

The book has 47 chapters in three independently paginated sections:

- General Mental Ability: 20 chapters, beginning at PDF page 17.
- Logical Deduction: 9 chapters, beginning at PDF page 577.
- Non-Verbal Reasoning: 18 chapters, beginning at PDF page 736.

Each published package must use a globally unique pipeline identifier and a section-qualified output filename. Printed question numbers remain local to a chapter.

## Architecture

The existing `textbook_chapters_v2` engine remains unchanged. A new Logical Reasoning source map provides the chapter titles, printed-page starts, PDF-page starts, and output naming. A source-marker proposal stage derives candidate question, answer-key, and solution boundaries from PDF layout; a vision review must approve or correct every candidate before a config generator writes V2 marker overrides. No record is published until extraction and verification results are independently ingested.

## Data Flow

1. Render the authoritative source PDF and prepare record-specific source crops.
2. Use a vision-model extraction job to transcribe the exact question, options, correct answer, and textbook solution steps.
3. Ingest only schema-valid, source-bound extraction results; reject ambiguous or incomplete source evidence.
4. Build a staged candidate, render it in the real KSAT application, and create a new-context verification job.
5. Ingest fresh field-level verdicts, then package and promote only records with current source, extraction, render, and verification fingerprints.

## Constraints

- Textbook content is untrusted data, never instructions.
- Do not silently correct textbook wording, notation, answer keys, or solution steps.
- Preserve diagrams as record-scoped media only where field-level source crops are configured; otherwise quarantine or record a reviewed rejection.
- ZIPs must be format-v3 text-only question packages, never page-image archives.
- Existing quantitative source, packages, and V2 code remain unchanged.
- All generated work stays under `tmp/logical-reasoning-v2/` until a release gate passes.

## Verification

Automated tests validate the complete 47-chapter map, unique output naming, section-boundary page arithmetic, and generated-config validity. Each source record requires the existing V2 extraction and fresh application-render vision-verification gates before packaging.
