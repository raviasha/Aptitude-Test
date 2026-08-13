# Question-bank package format v2

A v2 bank is a ZIP file with this shape:

```text
manifest.json
questions/
  arithmetic-01.jsonl
  data-interpretation.jsonl
assets/
  quarterly-sales.svg
  admissions-table.png
```

`manifest.json` identifies the question files and the reusable visual stimuli:

```json
{
  "format_version": 2,
  "bank_name": "Quantitative Aptitude - 2026",
  "question_files": [
    "questions/arithmetic-01.jsonl",
    "questions/data-interpretation.jsonl"
  ],
  "stimuli": [
    {
      "id": "sales-q1",
      "type": "image",
      "title": "Quarterly sales by region",
      "alt_text": "Grouped bar chart of quarterly sales for North and South",
      "file": "assets/quarterly-sales.svg"
    }
  ]
}
```

Each JSONL line is one complete question. A regular JSON file containing a list,
or an object with a `questions` list, is also accepted.

```json
{
  "key": "di-sales-001",
  "category": "Data Interpretation",
  "chapter": "Bar Graphs",
  "stimulus_id": "sales-q1",
  "difficulty": "Medium",
  "question_text": "What is the percentage increase from Q1 to Q2?",
  "options": {"A": "10%", "B": "15%", "C": "20%", "D": "25%"},
  "correct_answer": "C",
  "explanation": "Use (new - old) / old x 100.",
  "solution_steps": ["Read Q1 and Q2 from the shared chart.", "Apply the percentage-change formula."],
  "option_explanations": {"A": "", "B": "", "C": "Correct.", "D": ""}
}
```

Questions without a graph omit `stimulus_id`. Multiple questions can point to
one stimulus; the assessment sampler keeps selected questions sharing a
stimulus together in the generated test.

## Structured charts and tables

Instead of an image file, a stimulus can contain structured data. Supported
types are `chart` and `table`. Chart `kind` may be `bar` or `line`.

```json
{
  "id": "enrolment-trend",
  "type": "chart",
  "title": "Student enrolment",
  "alt_text": "Line chart of enrolment from 2023 to 2026",
  "content": {
    "kind": "line",
    "labels": ["2023", "2024", "2025", "2026"],
    "series": [{"name": "Students", "values": [420, 460, 510, 575]}]
  }
}
```

```json
{
  "id": "zone-results",
  "type": "table",
  "title": "Candidates by zone",
  "content": {
    "columns": ["Zone", "Appeared", "Qualified"],
    "rows": [["North", 800, 520], ["South", 760, 480]]
  }
}
```

Image stimuli support `.png`, `.jpg`, `.jpeg`, `.webp`, and safe `.svg` files.
SVG files containing scripts, event handlers, embedded objects, data URLs, or
external URLs are rejected. ZIP paths are validated and archive size limits are
enforced before extraction.

## Migrated quantitative bank

Run the deterministic migration/build script with the supplied source PDF to
include the Data Interpretation tables and graphs:

```powershell
& "C:\Users\ravis\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  scripts\build_quantitative_bank.py --write-source `
  --di-pdf "C:\path\to\quantitative-aptitude.pdf"
```

It assigns all 5,151 questions to Arithmetical Ability or Data Interpretation,
adds one of 39 chapters, and builds
`question-banks/quantitative_aptitude_categorized_v2.zip` from split JSONL files.
When `--di-pdf` is provided, the script matches every DI question to its source
exercise page, renders only those non-solution pages as compact JPEG assets, and
links the questions to 45 shared stimuli.
