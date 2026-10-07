# Coordinator — 7 October 2026

Use **[KSATCoordinatorSetup-2.1.0.exe](KSATCoordinatorSetup-2.1.0.exe)** to update the
server installation. This dated build adds multi-ZIP question-bank upload and
retains completed faculty results in CSV after test or bank deletion.

The product version stays 2.1.0; this folder distinguishes it from older builds.
The lab clients do not need updating.

Finish active exams, stop the Coordinator, back up its ProgramData directory,
then install over the existing installation. Do not uninstall or delete ProgramData.

[Full instructions](../../docs/coordinator-bulk-import-results-2026-10-07.md) ·
[Checksums](SHA256SUMS.txt) · [Verification](verification.json)

Signed with the existing KSAT lab release identity. Automated startup and package
checks passed; a physical server upgrade pilot has not been performed.
