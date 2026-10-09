# Coordinator 2.2.0 — 9 October 2026

[Download the installer](../release/coordinator-2.2.0/KSATCoordinatorSetup-2.2.0.exe).
This is a Coordinator-only update. Existing clients and lab connection files remain compatible.

## Upgrade the server

Finish active exams and uploads, close the Coordinator, and make a backup of
`C:\ProgramData\KSAT Coordinator`. Run **KSATCoordinatorSetup-2.2.0.exe** over the
existing installation. Do not uninstall or delete ProgramData. Existing accounts,
question banks, results, security identity and network settings are retained.
Existing banks do not need reimporting. After starting the app, the sign-in page
must show **Build 2.2.0**. Refresh the browser if necessary.

The backup is a recovery precaution; the installer does not restore backups for you.
Try one test and CSV export on the upgraded server before resuming lab exams.

## Import a whole textbook

In **Question banks**, choose the master ZIP and click **Import question bank**.
No extraction is needed. You can also select individual chapter ZIPs or several ZIPs
together. Each chapter becomes its own bank. Keep the page open until the report
appears; it lists successes and errors for individual chapters. Successful chapters
remain imported even if another chapter fails. Retry only failed chapter ZIPs
(extract those from the master if needed), not the entire successful batch.

The existing replacement checkbox still applies: replacing a bank used by a test is
not permitted. Leave it unchecked unless you intend to replace an unused bank.
Imports support one enclosing ZIP level, at most 200 chapters, a 50 MB upload limit,
and 150 MB/10,000 files total expanded chapter content. Corrupt, unsafe, encrypted
or unsupported archives produce errors rather than silently succeeding.

[The quantitative master ZIP](../question-banks/latest/textbook-downloads/quantitative-aptitude-chapters-01-39.zip)
contains 39 importable chapters. The two reasoning **SOURCE-PAGES-ONLY** archives
contain textbook images, not question banks; this update does not convert them into
questions and reports that extraction is still required.

## Choose chapters for a test

Use the book assignment controls in **Question banks** to group existing chapter
banks under a textbook; no reimport is required. Banks without book metadata appear
under **Unassigned**. Package-provided book titles are retained during import.

In **Tests → Create an assessment**, choose **Balanced across chapters**, tick the
desired chapters, and enter the total question count. Selection may span books.
The preview shows each chapter's share. Questions are randomly sampled, split as
evenly as possible, with shortages redistributed to chapters with available questions.
The old manual single-bank mode remains available.

## Results after deletion

Deleting a test or question bank preserves **completed result summaries** in future
CSV exports (`aptitude-results.csv`). The archive is stored in the Coordinator database
and survives restarts. This does not preserve deleted question-by-question reviews,
unfinished attempts or practice attempts, nor recover results deleted before the
retention feature was installed. Existing CSV files already exported are not modified.

## Version and compatibility

Product, installer and app UI: **2.2.0**. `/api/coordinator-build` reports that product
version. The legacy `/api/build` response and public trust metadata intentionally
remain **2.1.0** because installed clients and lab builders require that exact contract.
The installer retains the existing application identity, installation directory and
owned firewall rule names so an in-place upgrade can recognize them.

[Automated verification and known limitations](coordinator-2.2.0-verification.md)
are recorded alongside the download.
No installed lab ProgramData or Windows trust/firewall configuration was changed
during the build tests; an actual physical lab upgrade remains a pilot check.
