# Logical Reasoning V2 Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure the existing V2 source-fidelity pipeline to create chapter-wise, vision-verified question-bank ZIPs for the Logical Reasoning textbook.

**Architecture:** Add a declarative 47-chapter source map and a small config generator; retain the existing V2 preparation, extraction, rendering, verification, packaging, and promotion implementation. The generated configs use globally unique chapter IDs, while their record numbers remain the book's local printed question numbers.

**Tech Stack:** Python 3.14, pypdfium2, Pillow, Playwright, existing `textbook_chapters_v2` pipeline, unittest.

**Spec:** `docs/superpowers/specs/2026-09-17-logical-reasoning-v2-design.md`

## Global Constraints

- Source PDF: `data-engineering/dokumen.pub_a-modern-approach-to-logical-reasoning-1nbsped-8121919053-9788121919050.pdf`.
- Work root: `tmp/logical-reasoning-v2/`.
- Use format-v3 text-only packages only; do not create page-image ZIPs.
- Preserve exact printed source content; incomplete or ambiguous evidence must be quarantined or recorded as a reviewed rejection.
- Do not modify existing quantitative V2 configs, packages, or pipeline behavior.

---

### Task 1: Create the Logical Reasoning chapter map

**Files:**
- Create: `data-engineering/logical_reasoning_v2/__init__.py`
- Create: `data-engineering/logical_reasoning_v2/book_map.py`
- Test: `data-engineering/logical_reasoning_v2/tests/test_book_map.py`

**Interfaces:**
- Produces: `ChapterEntry` dataclass with `pipeline_id`, `section_id`, `section_title`, `chapter_number`, `slug`, `title`, `pdf_page_start`, and `pdf_page_end` fields.
- Produces: `chapters() -> tuple[ChapterEntry, ...]` containing the book's 47 contents-defined chapters.

- [ ] **Step 1: Write the failing map completeness test**

```python
from logical_reasoning_v2.book_map import chapters

def test_book_map_has_all_contents_chapters_in_source_order():
    entries = chapters()
    assert len(entries) == 47
    assert entries[0].title == "Analogy"
    assert entries[-1].title == "Practice Question Set"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest data-engineering/logical_reasoning_v2/tests/test_book_map.py -v`

Expected: FAIL because `logical_reasoning_v2.book_map` does not exist.

- [ ] **Step 3: Write the minimal map implementation**

```python
@dataclass(frozen=True)
class ChapterEntry:
    pipeline_id: int
    section_id: str
    section_title: str
    chapter_number: int
    slug: str
    title: str
    pdf_page_start: int
    pdf_page_end: int

def chapters() -> tuple[ChapterEntry, ...]:
    return _CHAPTERS
```

Populate `_CHAPTERS` from the supplied contents pages, assigning IDs 101-147 in source order and deriving each end page from the following chapter or section boundary.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest data-engineering/logical_reasoning_v2/tests/test_book_map.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add data-engineering/logical_reasoning_v2
git commit -m "feat: map logical reasoning textbook chapters"
```

### Task 2: Generate V2 configs from the chapter map

**Files:**
- Create: `data-engineering/logical_reasoning_v2/configs.py`
- Create: `data-engineering/logical_reasoning_v2/cli.py`
- Test: `data-engineering/logical_reasoning_v2/tests/test_configs.py`

**Interfaces:**
- Consumes: `chapters() -> tuple[ChapterEntry, ...]`.
- Produces: `config_for(entry: ChapterEntry, source_pdf: Path, root: Path) -> dict[str, object]`.
- Produces: `write_configs(destination: Path, source_pdf: Path, root: Path) -> tuple[Path, ...]`.

- [ ] **Step 1: Write the failing generated-config test**

```python
from pathlib import Path
from logical_reasoning_v2.book_map import chapters
from logical_reasoning_v2.configs import config_for

def test_generated_config_uses_isolated_work_and_qualified_package_name():
    config = config_for(chapters()[0], Path("source.pdf"), Path("tmp/logical-reasoning-v2"))
    assert config["chapter"] == 101
    assert config["work_root"] == "tmp/logical-reasoning-v2"
    assert config["candidate_path"].endswith("logical_reasoning_s01_ch01_analogy_candidate.zip")
    assert config["published_path"].endswith("logical_reasoning_s01_ch01_analogy_all_vision_text_only.zip")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest data-engineering/logical_reasoning_v2/tests/test_configs.py -v`

Expected: FAIL because `logical_reasoning_v2.configs` does not exist.

- [ ] **Step 3: Write the minimal config generator**

```python
def config_for(entry: ChapterEntry, source_pdf: Path, root: Path) -> dict[str, object]:
    stem = f"logical_reasoning_{entry.section_id}_ch{entry.chapter_number:02d}_{entry.slug}"
    return {
        "chapter": entry.pipeline_id,
        "chapter_name": f"{entry.section_title}: {entry.title}",
        "bank_name": f"R. S. Aggarwal - {entry.section_title} - Chapter {entry.chapter_number}: {entry.title} (vision-verified)",
        "source_pdf": str(source_pdf),
        "work_root": str(root),
        "candidate_path": str(root / f"chapter-{entry.pipeline_id:03d}" / f"{stem}_candidate.zip"),
        "published_path": str(Path("question-banks") / f"{stem}_all_vision_text_only.zip"),
    }
```

Leave question/key/solution discovery fields empty until source preparation has identified objective exercises, keys, and numbered solution pages. The command must refuse to overwrite an existing config without `--force`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest data-engineering/logical_reasoning_v2/tests/test_configs.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add data-engineering/logical_reasoning_v2
git commit -m "feat: generate logical reasoning V2 configs"
```

### Task 3: Propose and vision-review Chapter 1 source markers

**Files:**
- Create: `data-engineering/logical_reasoning_v2/discovery.py`
- Modify: `data-engineering/logical_reasoning_v2/cli.py`
- Test: `data-engineering/logical_reasoning_v2/tests/test_discovery.py`
- Create: `data-engineering/logical_reasoning_v2/configs/chapter-101.json`

**Interfaces:**
- Consumes: `ChapterEntry` and the source PDF.
- Produces: `propose_markers(source_pdf: Path, entry: ChapterEntry) -> MarkerProposalSet` with candidate question, answer-key, and solution segments.
- Produces: a review queue binding every candidate segment to its rendered source-page hash.
- Produces: a config compatible with `textbook_chapters_v2.ChapterConfig.load`.

- [ ] **Step 1: Write the failing Chapter 1 discovery test**

```python
from pathlib import Path
from logical_reasoning_v2.discovery import discover_chapter_one

def test_marker_proposal_preserves_every_detected_numbered_boundary(tmp_path):
    proposals = propose_markers(Path("fixture.pdf"), chapters()[0])
    assert proposals.question_numbers[0] == 1
    assert proposals.question_segments
    assert proposals.review_jobs
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest data-engineering/logical_reasoning_v2/tests/test_discovery.py -v`

Expected: FAIL because `propose_markers` does not exist.

- [ ] **Step 3: Implement source discovery without transcribing records**

Use `pdfplumber` to propose numbered question and solution boundaries and answer-key entries inside every chapter page. Render every source page with the V2 renderer and create a review job that binds the proposed segment coordinates to that page's hash. A reviewed marker must use explicit V2 `segments`; never use adjacent marker boundaries for this book. Do not infer question text, answers, or solution text at this stage.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest data-engineering/logical_reasoning_v2/tests/test_discovery.py -v`

Expected: PASS with all proposed markers represented in a source-bound review queue.

- [ ] **Step 5: Commit**

```bash
git add data-engineering/logical_reasoning_v2
git commit -m "feat: discover logical reasoning chapter one evidence"
```

### Task 4: Execute the V2 Chapter 1 extraction boundary

**Files:**
- Create: `tmp/logical-reasoning-v2/chapter-101/`
- Create: `tmp/logical-reasoning-v2/chapter-101/extraction-jobs.jsonl`

**Interfaces:**
- Consumes: `data-engineering/logical_reasoning_v2/configs/chapter-101.json`.
- Produces: source-crop evidence and record-specific vision extraction jobs.

- [ ] **Step 1: Run the Chapter 1 preparation command**

Run:

```powershell
$env:PYTHONPATH = 'data-engineering;tmp/app-contract-deps'
python -m textbook_chapters_v2 prepare --config data-engineering/logical_reasoning_v2/configs/chapter-101.json
```

Expected: source images, marker evidence, and a current audit ledger under `tmp/logical-reasoning-v2/chapter-101/`.

- [ ] **Step 2: Generate the extraction queue**

Run:

```powershell
python -m textbook_chapters_v2 extract --config data-engineering/logical_reasoning_v2/configs/chapter-101.json
```

Expected: exit code 20 with a source-bound `extraction-jobs.jsonl` queue; no ZIP is created.

- [ ] **Step 3: Validate the queue has unique, fingerprinted records**

Run: `python -m unittest data-engineering/textbook_chapters_v2/tests/test_vision.py -v`

Expected: PASS.

- [ ] **Step 4: Commit config and source-discovery code only**

```bash
git add data-engineering/logical_reasoning_v2
git commit -m "feat: prepare logical reasoning chapter one vision queue"
```

### Task 5: Complete the Chapter 1 V2 release gates

**Files:**
- Create: `tmp/logical-reasoning-v2/chapter-101/extraction-results/*.json`
- Create: `tmp/logical-reasoning-v2/chapter-101/verification-results/*.json`
- Create: `question-banks/logical_reasoning_s01_ch01_analogy_all_vision_text_only.zip`

**Interfaces:**
- Consumes: the Chapter 1 extraction queue and independently generated schema-valid vision results.
- Produces: a format-v3 chapter ZIP only when the existing V2 release gate is green.

- [ ] **Step 1: Process the record-specific extraction jobs in isolated vision contexts**

For each `extract-*.json` job, use its bound source crops and exact schema. Save only schema-valid results to `extraction-results/`; do not repair source content in the response.

- [ ] **Step 2: Ingest extraction results and build the candidate**

Run:

```powershell
python -m textbook_chapters_v2 ingest-extraction --config data-engineering/logical_reasoning_v2/configs/chapter-101.json --results tmp/logical-reasoning-v2/chapter-101/extraction-results
python -m textbook_chapters_v2 build --config data-engineering/logical_reasoning_v2/configs/chapter-101.json
```

Expected: every record is either ready for render or blocked with source-specific evidence.

- [ ] **Step 3: Render and create the independent verification queue**

Run:

```powershell
python -m textbook_chapters_v2 render --config data-engineering/logical_reasoning_v2/configs/chapter-101.json
python -m textbook_chapters_v2 verify --config data-engineering/logical_reasoning_v2/configs/chapter-101.json
```

Expected: full-card and field screenshots plus a fresh verification queue; no reuse of extraction-context judgments.

- [ ] **Step 4: Ingest verification results and package only if green**

Run:

```powershell
python -m textbook_chapters_v2 ingest-verification --config data-engineering/logical_reasoning_v2/configs/chapter-101.json --results tmp/logical-reasoning-v2/chapter-101/verification-results
python -m textbook_chapters_v2 package --config data-engineering/logical_reasoning_v2/configs/chapter-101.json
python -m textbook_chapters_v2 promote --config data-engineering/logical_reasoning_v2/configs/chapter-101.json
```

Expected: the V2 gate either atomically promotes a validated ZIP or refuses with the exact stale/pending/blocked record reason.

- [ ] **Step 5: Verify the promoted ZIP with the application parser**

Run: `python -m unittest test_completed_chapter_packages.py -v`

Expected: PASS, including the new Logical Reasoning Chapter 1 package.

### Task 6: Roll out Chapters 2-47 in source order

**Files:**
- Create: `data-engineering/logical_reasoning_v2/configs/chapter-102.json` through `chapter-147.json`
- Create: `tmp/logical-reasoning-v2/chapter-102/` through `chapter-147/`
- Create: `question-banks/logical_reasoning_*.zip`

**Interfaces:**
- Consumes: the map and config generator established in Tasks 1-3.
- Produces: one independently validated package per released chapter.

- [ ] **Step 1: Generate all source configs from the validated map**

Run: `python -m logical_reasoning_v2 write-configs --destination data-engineering/logical_reasoning_v2/configs --source-pdf data-engineering/dokumen.pub_a-modern-approach-to-logical-reasoning-1nbsped-8121919053-9788121919050.pdf --work-root tmp/logical-reasoning-v2`

Expected: 47 section-qualified configs with no collisions.

- [ ] **Step 2: For each chapter, complete Task 4 before any vision work**

Run `prepare` and `extract` per generated config, ensuring source crops contain only the exact question, key, and solution evidence for each record.

- [ ] **Step 3: For each chapter, complete Task 5 before moving to the next package**

Use separate vision contexts for extraction and verification. Save a passing ZIP immediately; record unresolved textbook material as a reviewed rejection, never a fabricated solution.

- [ ] **Step 4: Run final regression tests**

Run: `python -m unittest discover -s data-engineering/textbook_chapters_v2/tests -v`

Expected: PASS with all existing quantitative pipeline behavior retained.

- [ ] **Step 5: Commit source-map and config changes in reviewable batches**

```bash
git add data-engineering/logical_reasoning_v2
git commit -m "feat: add logical reasoning V2 chapter batch"
```
