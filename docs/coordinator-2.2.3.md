# Coordinator 2.2.3 — upgrade guide

Signed private-lab build; frozen startup, TLS and installer-payload checks passed.
A physical lab installation pilot is still required. Downloads:
[installer](../release/coordinator-2.2.3/KSATCoordinatorSetup-2.2.3.exe) and
[standalone executable](../release/coordinator-2.2.3/KSATCoordinator-2.2.3.exe).
See the [verification record](../release/coordinator-2.2.3/verification.json).

## What changes

Coordinator 2.2.3 removes a matching printed question-number prefix from the
display text of *A New Approach to Reasoning* questions identified by source
keys `ch401-q0001` through the corresponding chapter 441 keys. It preserves
directions, internal statement numbering, decimals and unmatched numbers;
stored bank records and answer keys are not rewritten. New assessment releases
receive the corrected text. Existing releases remain unchanged, so create new
tests/releases after upgrading to apply the server cleanup.

Supporting diagrams and charts marked as context are packaged as image stimuli
using the existing client wire format. This keeps the stem beside its supporting
image and allows older clients, including 2.1.5, to consume new releases. The
complete presentation fix also requires **Client 2.1.6**, which hides duplicate
text after complete question/option images load and provides image-failure
fallback. See the [client rollout guide](client-2.1.6-visual-update.md).

The 2.2.2 master-ZIP import support and Coordinator shortcut behavior remain:
master uploads up to 100,000,000 bytes, individual chapters up to 50,000,000
bytes, and the existing archive validation protections. Clicking the shortcut
while the server is running reopens its page; closing the browser alone does
not stop the server.

## Upgrade the existing server

1. Download the installer and check its [checksum](../release/coordinator-2.2.3/SHA256SUMS.txt).
   The build retains the existing release publisher and signing identity.
2. Finish active exams and pending uploads. In the Coordinator, use **Stop
   server**, confirm and wait for its process to exit.
3. With administrator approval, run `KSATCoordinatorSetup-2.2.3.exe` over the
   existing installation. Do not uninstall or delete
   `C:\ProgramData\KSAT Coordinator`.
4. Retain accounts, question banks, results, network settings, enrolled devices
   and the existing Coordinator security identity. Do not regenerate connection
   files or change client trust as part of this upgrade.
5. Start the Coordinator and check that its sign-in page reports **Build 2.2.3**.
   Confirm existing banks/results and client connectivity before new exams.
6. Use **Faculty > Client updates** to pilot and publish the signed Client 2.1.6
   bundle as described in the client guide. Keep the server running for rollout.

These are operator checks to perform after the build, not a record of checks
already completed.

## Question banks and new tests

Existing reasoning ZIPs are unchanged: **no reasoning reimport is needed**.
Use the existing reasoning banks in newly created tests/releases after the
upgrade. Old tests/releases do not acquire the server's number cleanup merely
by installing the new executable.

The quantitative visual revision adds 30 geometry diagrams (29 in chapter 24,
one in chapter 25) and removes duplicate chart/table transcription from 306
question stems in chapters 36–39. Required question text, directions, options,
answers and solutions are retained. These are presentation corrections, not a
new answer-key audit or a new full V2 vision seal.

| Installation | Package |
| --- | --- |
| Existing quantitative banks | [quantitative-visual-updates-6-chapters.zip](../question-banks/quantitative-visual-update-20261022/quantitative-visual-updates-6-chapters.zip) |
| New setup needing all chapters | [quantitative-aptitude-master-39-chapters.zip](../question-banks/quantitative-visual-update-20261022/quantitative-aptitude-master-39-chapters.zip) |

The master contains six corrected chapters and 33 unchanged baseline chapters.
In **Question banks**, import the appropriate ZIP after the server upgrade.
Corrected banks carry `[Visual revision 2026-10-22]` in their names, allowing
them to coexist with old banks and their history. For new tests, select only
the revised copy of each corrected chapter; do not select both copies.
Do not force replacement, delete the old banks or remove student records/results.
Importing a revision does not change an existing assessment release.

The package contents and local source checks are documented in the
[package notes](../question-banks/quantitative-visual-update-20261022/README.txt).
They do not certify a signed Coordinator or Client binary.
