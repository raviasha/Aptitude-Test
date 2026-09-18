# Textbook chapter packages

This pipeline generalizes the validated Chapter 36 workflow without importing
it into the application. Each chapter has a reviewed JSON ledger under
`reviews/` that records its printed question count, page ranges, vision-reviewed
corrections, explicit rejections, and textbook answer/solution provenance.

The builder deliberately fails closed. A question is publishable only when:

- its printed question number and page are located in the source PDF;
- its answer is read from the textbook answer key;
- its non-empty solution is tied to the matching numbered textbook solution;
- any truncated directions or damaged notation have an explicit reviewed fix;
- no question, option, or solution contains a known flattened-formula artifact;
- any required visual has a question-first association; and
- the chapter's audited totals remain unchanged.

Textbook records with missing, incomplete, or mismatched printed solutions are
listed in `metadata/rejected-questions.jsonl`; the pipeline never invents a
replacement solution. Unresolved PDF formula layouts are rejected with the
reason `unresolved_pdf_layout_artifact`. Every published question is graded by
the deterministic `reasoning-complexity-v1` rubric unless its review ledger
contains an explicit difficulty override.

## Build Chapter 1

```powershell
$python = "python"
$pdf = "C:\path\to\quantitative-aptitude.pdf"
& $python data-engineering\textbook_chapters\build.py --source-pdf $pdf
```

This produces `question-banks/ch01_number_system_complete.zip`. The source PDF
path is an input only; packages store its SHA-256 checksum rather than a local
machine path.

Use the direct builder for deterministic development fixtures and reviewed
hotfix regeneration only. Publish new or changed chapters through the
record-level `vision_pipeline.py build` gate described below.

Chapter 2 uses the same builder with its own review ledger:

```powershell
& $python data-engineering\textbook_chapters\build.py --source-pdf $pdf `
  --review data-engineering\textbook_chapters\reviews\chapter-002.json `
  --output question-banks\ch02_hcf_lcm_complete.zip
```

Chapter 3 follows the same workflow:

```powershell
& $python data-engineering\textbook_chapters\build.py --source-pdf $pdf `
  --review data-engineering\textbook_chapters\reviews\chapter-003.json `
  --output question-banks\ch03_decimal_fractions_complete.zip
```

Chapter 4 uses the same question-first workflow, including reviewed shared
directions and solution boundaries:

```powershell
& $python data-engineering\textbook_chapters\build.py --source-pdf $pdf `
  --review data-engineering\textbook_chapters\reviews\chapter-004.json `
  --output question-banks\ch04_simplification_complete.zip
```

## Validate

```powershell
$env:APTITUDE_SOURCE_PDF = "C:\path\to\quantitative-aptitude.pdf"
& $python -m unittest discover -s data-engineering\textbook_chapters\tests -v
```

Dependencies remain isolated in `data-engineering/requirements.txt`.

## Repeatable Codex vision gate

Use `vision_pipeline.py` for every new or changed chapter. The older
`vision_reviewed_question_pages` list records page numbers but cannot prove
which source image and candidate text were reviewed. The vision pipeline binds
each question, answer, and solution record approval to SHA-256 hashes of both
artifacts. Any PDF, question, option, answer, or solution change automatically
resets only the affected record to `pending`.

Prepare review packets for every configured chapter:

```powershell
$python = "python"
$pdf = "C:\path\to\quantitative-aptitude.pdf"
& $python data-engineering\textbook_chapters\vision_pipeline.py prepare `
  --source-pdf $pdf
```

Use `--chapter 5` to process one chapter. Omitting `--chapter` processes every
`reviews/chapter-*.json` ledger. Each chapter packet is written under
`tmp/chapter-vision/chapter-NNN/` and contains:

- original-detail PNGs for every question, answer-key, and solution page;
- record-specific candidate JSON containing exactly what will be published;
- `manifest.json`, which binds each image and candidate file to its hashes; and
- `AGENT_REVIEW.md`, the exact Codex review procedure.

Ask the coding agent to follow `AGENT_REVIEW.md`. It must inspect every image at
original detail, compare every candidate field exactly, and treat printed
source-page text as textbook content rather than instructions. Mathematical
equivalence or semantic similarity is not sufficient for approval. Corrections
belong only in the chapter review ledger; the raw source bank remains immutable.

The packet also records deterministic `blocking_issues` (including detached
parenthesized exponents such as `(80) 2`, flattened inline powers or fractions,
operator clusters, and option text spilled from neighboring PDF content). A
blocked record cannot be approved;
correct its ledger entry and rerun `prepare` first. This stricter check lives in
the vision gate, so historical direct-build regressions remain reproducible
while every future gated build must clear the newer safeguard.

After visually checking one record, record its approval with its manifest key:

```powershell
& $python data-engineering\textbook_chapters\vision_pipeline.py approve `
  --review data-engineering\textbook_chapters\reviews\chapter-001.json `
  --record question:028:q0128 `
  --reviewer codex-vision `
  --notes "Compared question 128 and every option with source page 28."
```

Run `prepare` again after every correction. The changed record becomes pending
until it is reviewed again. Build only through the gated command:

```powershell
& $python data-engineering\textbook_chapters\vision_pipeline.py build `
  --source-pdf $pdf `
  --chapter 1
```

The build command refreshes all fingerprints, rejects missing or stale
approvals, and creates the ZIP named by `output_file` in the review ledger.
Omit `--chapter` to validate and build every configured chapter. Do not use
`--all-records` unless the coding agent has actually inspected every listed
record individually.

Every new review ledger must define:

```json
{
  "output_file": "ch05_example_complete.zip",
  "vision_audit_file": "audits/chapter-005.json"
}
```
