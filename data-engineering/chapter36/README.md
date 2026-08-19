# Chapter 36 page-aware pipeline

This pipeline rebuilds the Chapter 36 Tabulation bank from the immutable page
images and raw extraction session under
`question-banks/extraction-ch36-session/`.

The workflow is question-first and page-local:

1. A page analysis identifies the questions actually printed on each page.
2. It identifies each table, graph, caption, unit label, and legend needed by
   those questions.
3. It first associates same-page visuals, then resolves only explicitly
   reviewed cross-page continuations.
4. It crops the visual region without question bodies, options, answer keys,
   or solutions.
5. It tags cross-page questions with every visual source page and creates a
   composite stimulus when the required visual spans more than one page.
6. It replaces every raw answer and explanation with the reviewed textbook
   answer key and printed solution from `textbook-solutions.json`.
7. It records the answer-key page and every solution page on each published
   question, and rejects the build if any question lacks textbook provenance.
8. It validates the ZIP contract without importing pipeline code into the app.

The checked-in `page-analysis.json` is the reviewed output of that analysis for
pages 896-908. `VISION_PROMPT.md` defines the provider-neutral contract a vision
model must follow when producing or revising this file. Answer and solution
provenance extends through pages 907-908; raw vision-generated answers,
explanations, and calculations are explicitly untrusted and are never published.

Build from the repository root:

```powershell
& "C:\Users\ravis\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  data-engineering\chapter36\build.py `
  --output question-banks\ch36_tabulation_complete.zip
```

For crop review, add `--qa-dir tmp\chapter36-crop-qa`. The QA directory is not
part of the application or the final package.

Run the pipeline tests:

```powershell
& "C:\Users\ravis\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  -m unittest discover -s data-engineering\chapter36\tests -v
```
