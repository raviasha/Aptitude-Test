# Aptitude Lab — Server Installation

The Windows installer installs a single server executable, creates a Desktop shortcut, and opens the application at `http://localhost:8000`. Student computers use `http://<SERVER-LAN-IP>:8000`.

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
3.10+ and Inno Setup 6 installed. The build explicitly packages only the sample
pair from `templates`; files in `question-banks` remain separate. It produces:

```text
release\Aptitude-Lab-Setup.exe
```

Install that file on the designated lab server. The installer adds a private-network Windows Firewall rule for port 8000 and places the Desktop shortcut.

## Demo accounts

- Student: `1KS23AI042` / `student123`
- Faculty: `faculty` / `faculty123`

Change demo passwords and set a strong `SESSION_SECRET` before production use.
