# KSAT Coordinator 2.2.0

Released 9 October 2026. This supersedes the October 7 Coordinator 2.1.0 build.

[Download the installer](KSATCoordinatorSetup-2.2.0.exe) ·
[Upgrade and usage instructions](../../docs/coordinator-2.2.0.md) ·
[SHA-256 checksums](SHA256SUMS.txt) · [Build checks](verification.json) ·
[Test and review record](../../docs/coordinator-2.2.0-verification.md)

Includes book-grouped multi-chapter random tests, direct textbook master-ZIP import,
and completed results retained in CSV exports after test/bank deletion.

Install over the existing Coordinator after finishing exams/uploads, closing it,
and backing up ProgramData. Do not uninstall or delete ProgramData. Existing banks
do not need reimporting. The sign-in page displays **Build 2.2.0**.
No client binaries were changed or rebuilt.

The installer and executable are signed with the existing KSAT lab publisher.
Automated HTTPS startup and extracted installer payload checks passed. An actual
upgrade on a physical lab server has not been performed as part of this build.
