# Aptitude Lab / KSAT 2.0

KSAT 2.0 ships separate Windows installers for the HTTPS faculty coordinator and
the loopback-only lab client. See the
[distributed-assessment operations runbook](docs/distributed-assessment-operations.md)
for installation, enrollment, launch, outage recovery, backup, load validation,
and diagnostics. The legacy central-browser workflow remains available for
personal practice and historical data.

## Question-bank formats

The recommended format is a version 2 ZIP package. It keeps a small manifest,
one or more JSON/JSONL question files, and shared graph or table assets together.
Each question records both `category` and `chapter`; questions that depend on the
same graph use the same `stimulus_id`. Faculty and students can then request an
exact quantity from any category/chapter and the server samples those questions
at random while rendering each shared stimulus with its questions.

See [`QUESTION_BANK_FORMAT_V2.md`](QUESTION_BANK_FORMAT_V2.md) for the package
layout and examples. Faculty can upload a v2 ZIP directly from **Question banks**.

Legacy HTML/JSON pairs remain supported. Their upload limit is 25 MB per file,
but they cannot share visual assets as efficiently as v2 packages.

## Assessment files

After installation, copy each assessment pair into:

```text
C:\ProgramData\Aptitude Lab\Question Banks
```

Each pair has the same base filename:

```text
placement-set-02.html
placement-set-02.json
```

- The HTML file contains student-facing text, tables, and inline SVG graphs/diagrams.
- The JSON file holds options, correct answers, category, chapter, difficulty, and explanation.

Open Faculty → **Question banks**, refresh the folder, and click **Import**. The app stores both the visual question markup and scoring data in SQLite; students never receive correct answers.

The installer copies one visual graph sample pair into the folder on first startup.

Additional question banks are stored in the repository's
[`question-banks`](question-banks/) folder and are deliberately excluded from
the installer. Download the desired bank, extract it if necessary, and manually
copy its matching HTML and JSON files into the folder above.

## Data-engineering boundary

Source-page analysis, vision-model contracts, crop generation, lineage checks,
and question-bank builds live under [`data-engineering`](data-engineering/).
They are development-time pipelines and are not imported by `app.py`, included
in the application requirements, or bundled into the Windows installer. The
application consumes only the generated version 2 ZIP package contract.

## Faculty tests and student practice

Faculty create an assessment by entering the desired count beside each chapter.
Students get the same category/chapter selector under **Practice**, with immediate
feedback, chapter-level results, practice history, and a retry-incorrect action.
Faculty-launched assessments remain exclusive while they are live.

Faculty and students can filter new assessment/practice sets by difficulty. A
Faculty-launched assessment is timed at one minute per question, can be taken
only once per student, and shows its final score immediately after submission.
Answers and worked solutions remain hidden at that point. After Faculty uses
**Close**, the submitting student can reopen the completed assessment on any
enrolled client, sign in, and review every question in their original randomized
order together with their choice, the correct choice, and frozen solution steps.
Start-window expiry alone does not release a review. Older releases that lack a
frozen review compartment report that detailed review is unavailable.
The Faculty dashboard lists submitted results and exam-integrity violations in
addition to the CSV export.

Launched assessments use a guarded full-screen browser mode. The app blocks and
records copy, cut, paste, context-menu, full-screen exit, and tab/window focus
loss events, then displays them with the student result and Faculty dashboard.
These controls are browser-enforced; a web application cannot physically stop
operating-system shortcuts or another application from minimizing a window.

Only one active browser login is allowed for a student USN. Signing out releases
the login. Faculty can delete a student account (including its records and login
lock), after which the student can register that USN again.

## Build the Windows installer

Run [`build-windows.bat`](build-windows.bat) on a Windows computer with Python
3.10+, Node.js, Inno Setup 6.7.3, and `innoextract`. The fail-fast build produces:

```text
release\KSATCoordinatorSetup-2.0.0.exe
release\KSATClientSetup-2.0.0.exe
```

Detailed build, smoke, recursive payload-scan, and hash verification steps are in
[`WINDOWS_EXE_BUILD.md`](WINDOWS_EXE_BUILD.md). Physical installation and trust
store/firewall changes require separate institutional authorization. Production
builds fail closed unless an institution-controlled Authenticode identity,
publisher pin, and HTTPS timestamp service are supplied; the ephemeral test
identity is verification-only and never a distributable credential.

## Demo accounts

- Student: `1KS23AI042` / `student123`
- Faculty: `faculty` / `faculty123`

Change demo passwords and set a strong `SESSION_SECRET` before production use.

## Distributed lab assessments

Faculty assessments are prepared as immutable encrypted releases for the installed
KSAT Client. Answer selection, navigation, autosave, and the per-student timer run
on the lab computer; the coordinator receives only the sealed final response and
scores it centrally. Personal practice continues to use the coordinator browser
workflow unchanged.

On Windows, a protected LocalSystem service owns the device identity, signed
requests, assessment state, cached packs, and submission outbox. Student
shortcuts open its constrained loopback UI; ordinary lab accounts do not receive
direct write access to the authoritative files.

The Faculty **Tests** page shows release readiness, a short content-hash prefix,
the ten-minute start window, eligible/started/submitted/voided counts, submission
queue pressure, and enrolled lab computers. Faculty can revoke or reactivate a
computer, rotate the one-time enrollment code, duplicate a used assessment into
a new immutable release, inspect an attempt's deterministic question order, and
perform reasoned/audited void, retake, and timer-extension operations. Enrollment
codes are displayed only in the successful rotation response.

Timer extensions do not rewrite the signed assessment pack. New attempts receive
the current audited release-extension policy in their signed ticket. Active
clients poll a small, answer-free, device-bound control endpoint at a jittered
interval and apply only a newer coordinator-signed deadline revision; offline
clients continue safely and apply it when connectivity returns.

Before first distributed deployment, stop/close every active faculty assessment
and run the compatibility upgrade against the coordinator data paths. Always
preview it first:

```powershell
python scripts/upgrade_distributed_assessments.py `
  "C:\ProgramData\Aptitude Lab\aptitude.db" `
  "C:\ProgramData\Aptitude Lab" --dry-run
```

Remove `--dry-run` to perform the live additive migration. A timestamped,
integrity-checked SQLite backup is created in `backups` before any live schema or
release change. Re-running the command is safe: it preserves existing students,
questions, practice/history, attempts, responses, scores, and violations, and
prepares only eligible unlaunched faculty tests that have no historical
submission or release.

Run the 30-client, 100-client, and outage release gates and follow the complete
[operator runbook](docs/distributed-assessment-operations.md) before deploying
to lab computers.
