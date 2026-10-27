# Coordinator 2.2.5 — restore an unused withdrawn pilot update

[Download the installer](../release/coordinator-2.2.5/KSATCoordinatorSetup-2.2.5.exe)
and [checksums](../release/coordinator-2.2.5/SHA256SUMS.txt).

Previously, withdrawing an update after selecting an offline PC left the
release permanently withdrawn. Uploading the same bundle again did not reset
it. Coordinator 2.2.5 adds **Restore for pilot** for a withdrawn release that
has never been published and has no client progress reports.

## Recover the offline pilot

1. Finish tests/uploads, stop the Coordinator, and install 2.2.5 over the
   existing installation. Keep `C:\ProgramData\KSAT Coordinator` intact.
2. Start the Coordinator and open **Faculty > Client updates**.
3. On the withdrawn Client 2.1.6 release, click **Restore for pilot**. The
   server rechecks the stored bundle's signatures and identity. No re-upload
   or rebuilt Client package is needed.
4. Select the desired enrolled PC, then click **Start pilot**.
5. Open KSAT on that PC. If it is already running, finish any active test and
   pending upload, close KSAT completely, then reopen it using its shortcut.
   The client checks for updates at startup. Keep the Coordinator running.
6. Refresh **Client updates** to see the latest status. Wait for **Updated**
   and installed version **2.1.6** before publishing more broadly.

Offline means the server has not heard from that client's KSAT application
recently; Windows being powered on alone is insufficient. **Withdraw** applies
to the entire update release, not just one PC. An offline pilot can be left
selected until its KSAT application starts.

## Recovery limits and data

The action is unavailable when any client has reported progress or the release
was previously published. Those withdrawals remain in place to preserve
ongoing work and prevent accidental rollout reactivation. Restore returns an
unused release to the uploaded state and requires an explicit pilot selection;
it never publishes to all clients. The original bundle and last withdrawal
timestamp remain intact.

Student logins, results, question banks, enrolled PCs and security identities
are preserved. Client and Lab Package Builder remain **2.1.6**, and the
[automatic publisher trust fix](coordinator-2.2.4.md) is retained.

See [verification evidence](../release/coordinator-2.2.5/verification.json).
Automated recovery uses isolated test data. A physical lab installation and
client rollout remain operator checks, not completed deployment claims.
