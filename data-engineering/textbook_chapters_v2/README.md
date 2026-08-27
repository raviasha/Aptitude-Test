# Vision-verified textbook pipeline V2

This is the reusable chapter-processing workflow. It keeps source evidence,
extraction jobs, application screenshots, independent verification results,
and the authoritative audit ledger under one chapter work directory. Textbook
images are always treated as untrusted data, never as instructions.

The original pipeline in `data-engineering/textbook_chapters/` remains the
rollback baseline. V2 ordinary runs write a staged candidate and never change a
published question-bank ZIP. Only `promote` can replace a published ZIP, and it
does so only after revalidating the live audit ledger and the package with the
application parser.

## Chapter configuration

Along with page ranges, reviewed crop markers, and question numbers, each JSON
config supplies these paths:

```json
{
  "source_pdf": "data-engineering/textbook_chapters_v2/sources/quantitative-aptitude.pdf",
  "work_root": "tmp/textbook-v2",
  "candidate_path": "tmp/textbook-v2/chapter-001/ch01_number_system_v2_candidate.zip",
  "published_path": "question-banks/ch01_number_system_complete.zip",
  "validation_viewports": [[1024, 768], [1600, 900]]
}
```

Paths are resolved from the directory where the command is run. Run commands
from the repository root. The crop-marker format is documented by the checked
chapter configs and is validated before any source crop is accepted.

Fields that must remain images require an explicit, record-scoped
`field_media` mapping. The mapping selects only source crops already authorized
for that record; it never falls back to the whole question crop for a question,
option, or solution field. A mapping can select a relative sub-box within a
crop:

```json
{
  "field_media": {
    "84": {
      "question": [
        {"role": "question", "source_index": 0},
        {"role": "question", "source_index": 1}
      ],
      "options": {
        "D": [{"role": "question", "source_index": 2, "box": [20, 30, 420, 180]}]
      },
      "solution": [
        {"role": "solution", "source_index": 0},
        {"role": "solution", "source_index": 1}
      ]
    }
  }
}
```

Multiple question or option segments are combined in source order into one
display image. Solution segments remain separate and their count is independent
of any transcribed solution-step count. If vision recommends an image field but
no safe field-level mapping exists, the record is quarantined instead of
substituting a broader crop.

## Repeatable workflow

In PowerShell:

```powershell
$env:PYTHONPATH = "data-engineering"
python -m textbook_chapters_v2 prepare --config data-engineering/textbook_chapters_v2/configs/chapter-001.json
python -m textbook_chapters_v2 extract --config data-engineering/textbook_chapters_v2/configs/chapter-001.json
```

`extract` writes the strict sequential coding-agent queue and exits with code
20 while results are pending. Process each queue item in a fresh vision context
and put each schema-valid response in the results directory using its job ID,
for example `extract-ch01-q0001.json`. Then continue:

```powershell
python -m textbook_chapters_v2 ingest-extraction --config data-engineering/textbook_chapters_v2/configs/chapter-001.json --results tmp/textbook-v2/chapter-001/extraction-results
python -m textbook_chapters_v2 build --config data-engineering/textbook_chapters_v2/configs/chapter-001.json
python -m textbook_chapters_v2 render --config data-engineering/textbook_chapters_v2/configs/chapter-001.json
python -m textbook_chapters_v2 verify --config data-engineering/textbook_chapters_v2/configs/chapter-001.json
```

`verify` emits a separate queue. Verification must use a fresh context and
compares the textbook source crops with screenshots from the real KSAT student
screen in unanswered and submitted states. The queue includes both full-card
screenshots and Task 8 field screenshots; every persisted screenshot is
reloaded and hash-validated before it can enter the verification fingerprint.
Put results in the verification results directory, then run:

```powershell
python -m textbook_chapters_v2 ingest-verification --config data-engineering/textbook_chapters_v2/configs/chapter-001.json --results tmp/textbook-v2/chapter-001/verification-results
python -m textbook_chapters_v2 package --config data-engineering/textbook_chapters_v2/configs/chapter-001.json
python -m textbook_chapters_v2 promote --config data-engineering/textbook_chapters_v2/configs/chapter-001.json
```

`run` performs the available deterministic stages and stops at the first vision
boundary. It prints a machine-readable summary such as:

```json
{"status":"vision_pending","stage":"extract","pending_jobs":293,"queue":"tmp/textbook-v2/chapter-001/extraction-jobs.jsonl"}
```

Resume `run` after the named results have been ingested. `run` packages an
all-green chapter but deliberately does not promote it.

## Recording a reviewed rejection

A record can become a reviewed rejection only after deterministic field checks
or vision verification have quarantined it with status `blocked`. An operator
must then provide both their identity and a concrete reason:

```powershell
python -m textbook_chapters_v2 reject `
  --config data-engineering/textbook_chapters_v2/configs/chapter-001.json `
  --question 85 `
  --reviewer "Ravi Asha" `
  --reason "The source option diagram is clipped and cannot be represented faithfully."
```

The command refuses pending, rendered, or approved records, so it cannot be
used to bypass extraction, field, or vision gates. Re-running the same command
is idempotent; changing existing review evidence is blocked. After recording
the rejection, resume `ingest-extraction` or `run`. The rejected record remains
in the authoritative audit metadata while only approved candidates enter the
question JSONL.

## Exit codes and safety gates

- `0`: the requested deterministic stage completed.
- `20`: external vision work is pending; the printed JSON names the queue.
- `21`: a semantic or audit gate blocked the stage.
- `22`: configuration, filesystem, or malformed-input error.

Every configured source record must be either `approved_for_publish` with
current source, candidate, render, and field-level verification evidence, or a
reviewed rejection with a reviewer and reason. Pending, quarantined, missing,
or stale records block both packaging and promotion. Source PDF bytes, chapter
configuration, crop evidence, extraction jobs/results, candidate content,
render assets, and verification jobs/results are fingerprinted. Replacing any
dependency invalidates only the downstream cache and returns a
`vision_pending` boundary instead of silently reusing stale results.

Reviewed rejections remain documented in the package audit metadata, but only
the authoritative ledger's `approved_records` are written to the published
question JSONL.

Every render also persists an application/renderer manifest. It hashes the real
`app.py`, its first-party `question_media.py` and `chapter_repairs.py` startup
imports, every static frontend asset, the V2 renderer and package/vision contract
code, and both result schemas. Its deterministic runtime data binds the Python
ABI, Playwright version, browser-selection policy, the actually selected browser
engine and version, viewports, package format, and render-contract version
without recording machine-specific browser paths. That identity comes from the
same browser instance that produced each screenshot, not a separate capability
probe, and a render fails closed if records report different runtimes.
`renders.json` is
content-addressed to that manifest, the exact candidate hashes, and normalized
per-record hashes for every full-card and field screenshot. Release validation
also compares those hashes with each approved audit record. A changed asset,
renderer contract, browser runtime, candidate, or screenshot therefore forces a
fresh render and fresh vision result; `package` and `promote` refuse approvals
made against earlier evidence.

`--force` only clears disposable cache entries under the configured chapter
work directory. It cannot alter the audit ledger, turn a failed vision verdict
into a pass, overwrite an existing staged candidate, or bypass package or
promotion gates.

Promotion verifies the candidate against the current persisted ledger, checks
the package-stage hash seal plus embedded audit and lineage metadata, reparses the ZIP with
`app.parse_question_package`, saves the old published ZIP as a timestamped
rollback artifact, and uses a temporary sibling plus atomic replacement. The
adjacent `.promotion.json` receipt records source, destination, old and new
hashes, audit hash, rollback path, and UTC timestamp.
