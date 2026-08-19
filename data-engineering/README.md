# Data engineering

This directory is deliberately separate from the Aptitude Lab application.
It owns source-page analysis, question/image lineage, crop generation, package
validation, and question-bank builds. The application only consumes the
versioned ZIP contract documented in `QUESTION_BANK_FORMAT_V2.md`.

Boundary rules:

- `app.py`, `static/`, and the installer must not import this directory.
- Pipeline-only dependencies belong in `data-engineering/requirements.txt`,
  not the application's `requirements.txt`.
- Source extraction sessions are immutable inputs.
- Generated packages carry their own page/question/image lineage metadata.
- Same-page association is preferred. A cross-page question is publishable
  only when a reviewed continuation explicitly identifies every source page;
  multi-page dependencies are combined into one tagged stimulus.

Chapter-specific instructions live beside each pipeline. For Chapter 36, see
`chapter36/README.md`.

The generalized, fail-closed workflow for the other textbook chapters lives in
`textbook_chapters/`. It reuses the Chapter 36 rules while keeping each
chapter's reviewed corrections and rejection ledger separate from shared build
logic.
