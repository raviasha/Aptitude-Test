# Coordinator 2.2.7 — fix upgrade from 2.2.5

Coordinator 2.2.6 accidentally omitted `2.2.5` from the accepted versions of
`coordinator-runtime.json`. Its installer therefore rejected valid existing
settings with **Coordinator runtime configuration is invalid**. This was an
application compatibility defect, not evidence of damaged student records.

[Download Coordinator 2.2.7](../release/coordinator-2.2.7/KSATCoordinatorSetup-2.2.7.exe).

1. Close the error dialog and exit the failed 2.2.6 setup. Stop any running
   Coordinator before upgrading.
2. Run the 2.2.7 installer over the existing installation.
3. Keep `C:\ProgramData\KSAT Coordinator` intact. Do not delete or manually
   rewrite its runtime configuration, database, certificates or keys.
4. Start the Coordinator and confirm the existing logins, question banks and
   results are available. Its displayed version should be 2.2.7.

The fix accepts the known 2.2.5 and 2.2.6 settings formats. Migration retains the
hostname, bind address, port and interactive setting, and saves the original
configuration to `coordinator-runtime.pre-2.2.7.json.bak` before updating its
version. Existing backups remain untouched. Malformed or unknown configuration
formats still require investigation rather than an automatic reset.

Client and Lab Package Builder remain 2.1.7; this hotfix only changes the
Coordinator package. The client recovery instructions remain in the
[2.1.7 guide](client-2.1.7-recovery.md).

The regression test reproduces the exact 2.2.5 rejection and now tests every
published Coordinator runtime version recorded in the repository. Release
verification also runs the packaged `--validate-config` command twice against
2.2.5 and 2.2.6 fixtures, checking the original configuration backup and unchanged
database, results CSV and security files. See the
[release verification](../release/coordinator-2.2.7/verification.json).
