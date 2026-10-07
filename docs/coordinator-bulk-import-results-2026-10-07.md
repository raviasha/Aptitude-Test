# Coordinator update: bulk chapter import and retained CSV results

This update affects the Faculty Coordinator only. Lab clients need no update.
The Coordinator protocol/product version remains 2.1.0 for compatibility; the
dated release directory identifies this build separately from older 2.1.0 files.

## Download and import a textbook

1. Download [the quantitative master ZIP](../question-banks/latest/textbook-downloads/quantitative-aptitude-chapters-01-39.zip).
2. Extract that master ZIP once. Open its `chapters` folder.
3. In Coordinator → Question banks, select all the chapter ZIPs, or drag them in
   together. Keep each chapter ZIP zipped.
4. Click Import once. The Coordinator imports chapters sequentially and shows
   progress and a separate success/error result for each file.
5. If any imports fail, correct their errors and select only those failed files
   for retry. Successful imports remain in the library as separate chapter banks.

Keep the page open until processing finishes. The existing “Update an existing
unused bank with the same name” choice applies to every selected package. Existing
validation, duplicate handling and restrictions on replacing used banks still apply.
The master archive itself is not a question bank; extract it before importing.

The [reasoning master download](../question-banks/latest/textbook-downloads/new-reasoning-chapters-01-02-SOURCE-PAGES-ONLY.zip)
contains only the two source-page archives. These are not importable question banks.

## Results after deletion

Deleting a test now saves its completed faculty result rows in the Coordinator
database before removing the test and its detailed attempts/responses. CSV exports
include those archived rows alongside current results, without duplicating attempts.
Student identity, test name, scores, category percentages and recorded violation
details remain in the CSV. The archive survives application restarts and is included
in the database backup. The existing export filename is `aptitude-results.csv`.

Deleting a question bank also preserves completed faculty results affected by its
dependent test/response deletion. Archive writes and deletion use one transaction:
if deletion fails, both roll back. Practice attempts and unfinished attempts are not
added to the faculty results export.

This preserves CSV result summaries, not deleted question-by-question reviews.
It cannot reconstruct results deleted before this update was installed.

## Install on the server

Use [the dated Coordinator installer](../release/coordinator-2026-10-07/KSATCoordinatorSetup-2.1.0.exe).
Finish active exams, close the running Coordinator, back up its ProgramData folder,
and install over the existing installation. Do not uninstall or remove ProgramData.
The update adds its results archive table automatically. Existing accounts,
results, hostname and lab identity are retained. The installer uses the existing
KSAT lab signing identity; the existing lab certificate trust remains applicable.

Checksums and build verification are in the same dated release directory.

Validation: 30 admin tests (including seven result-retention regressions), 19
archive/hostname/handoff tests, and the bulk-upload browser checks passed. The
signed executable passed disposable HTTPS startup and application-payload
equivalence checks; the signed installer was extracted and its embedded EXE
matched the build. A physical lab upgrade pilot has not been performed.
