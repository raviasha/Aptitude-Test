# Chapters 1–5 All-Vision Text-Only Question Banks

## Objective

Produce a separately importable, text-only KSAT ZIP for each textbook chapter from Chapter 1 through Chapter 5. Save each ZIP immediately after that chapter passes validation.

## Required process

- Use the textbook PDF as authoritative source evidence.
- Extract every recoverable question, answer mapping, and printed solution with vision; do not place textbook-page images in the student-facing bank.
- Preserve mathematical meaning, answer choices, superscripts/subscripts, grouping, stacked fractions, and structured arithmetic using software-rendered Unicode text.
- Validate record schemas and exact source/job fingerprints before building.
- Render questions and submitted solutions through the real KSAT application at both configured viewports.
- Use independent vision review for newly extracted or materially changed records.
- Repair material mathematical, answer-mapping, readability, clipping, or structured-layout defects.
- Accept minor proportional-font spacing differences when the software-rendered text preserves the printed structure and meaning.
- Keep incomplete textbook entries as explicit reviewed exclusions rather than inventing missing content.
- Package with `format_version` 3 and a distinct internal bank name.
- Validate each ZIP with the application parser, question count, manifest, and a zero-display-image assertion.

## Outputs

- `question-banks/ch01_number_system_all_vision_text_only.zip`
- `question-banks/ch02_hcf_lcm_all_vision_text_only.zip`
- `question-banks/ch03_decimal_fractions_all_vision_text_only.zip`
- `question-banks/ch04_simplification_all_vision_text_only.zip`
- `question-banks/ch05_square_roots_cube_roots_all_vision_text_only.zip`

The exact Chapter 2–5 filename stems may follow their existing config names when those names differ; the `_all_vision_text_only.zip` suffix and distinct internal bank names are mandatory.

## Stopping rule

Do not repeat a full chapter cycle solely for punctuation or pixel-level typography. Repeat only when a difference changes mathematical meaning, answer mapping, source fidelity of a printed derivation, readability, clipping, or the recognizable structure of stacked/long arithmetic.
