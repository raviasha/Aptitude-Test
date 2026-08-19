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
- any required visual has a question-first association; and
- the chapter's audited totals remain unchanged.

Textbook records with missing, incomplete, or mismatched printed solutions are
listed in `metadata/rejected-questions.jsonl`; the pipeline never invents a
replacement solution.

## Build Chapter 1

```powershell
$python = "python"
$pdf = "C:\path\to\quantitative-aptitude.pdf"
& $python data-engineering\textbook_chapters\build.py --source-pdf $pdf
```

This produces `question-banks/ch01_number_system_complete.zip`. The source PDF
path is an input only; packages store its SHA-256 checksum rather than a local
machine path.

Chapter 2 uses the same builder with its own review ledger:

```powershell
& $python data-engineering\textbook_chapters\build.py --source-pdf $pdf `
  --review data-engineering\textbook_chapters\reviews\chapter-002.json `
  --output question-banks\ch02_hcf_lcm_complete.zip
```

## Validate

```powershell
$env:APTITUDE_SOURCE_PDF = "C:\path\to\quantitative-aptitude.pdf"
& $python -m unittest discover -s data-engineering\textbook_chapters\tests -v
```

Dependencies remain isolated in `data-engineering/requirements.txt`.
