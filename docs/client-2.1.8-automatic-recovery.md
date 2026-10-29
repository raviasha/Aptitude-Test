# Client 2.1.8: automatic obsolete-attempt recovery

Client 2.1.8 includes a Windows installer, matching Lab Package Builder and a
signed managed-update bundle for clients already running 2.1.7. See the
[release downloads](https://github.com/raviasha/Aptitude-Test/releases/tag/client-v2.1.8)
and [verification record](../release/client-2.1.8/verification.json).

The lab's retention policy now treats submissions rejected with the exact code
`attempt_identity_mismatch` as obsolete. Client 2.1.8 automatically creates and
verifies a protected archive before removing those attempts from its live queue.
This handles both old blocked records at startup and new rejections during use.
The student returns to the available assessments without an administrator visit.

Archives remain under `C:\ProgramData\KSAT Client\recovery`. They contain the
saved answers and signed submission; they are not uploaded as graded results.
Other errors still show a diagnostic, and active tests, other queued submissions,
accepted results, device enrollment and the lab connection remain intact.
An archive failure leaves the live attempt untouched. Automatic recovery waits
until any managed installation has finished verifying preserved data.

## Deploy from the Coordinator

1. Download `KSATClientUpdate-2.1.8.ksat-client-update` from the
   [Client 2.1.8 release](https://github.com/raviasha/Aptitude-Test/releases/tag/client-v2.1.8).
2. In Faculty > Client updates, upload the bundle and select a pilot PC already
   running Client **2.1.7**. Start the pilot and open KSAT on that PC.
3. Wait for the Coordinator to report a verified successful update. The client
   header should say **Client version 2.1.8**. Verify test start, submission and
   answer review, then publish the rollout to the other PCs.

Client 2.1.7 supports this managed upgrade even when completed submissions are
queued. Active tests defer installation. Offline PCs update when KSAT reconnects.
Clients 2.1.5/2.1.6 need a one-time interactive installation of 2.1.8; they do not
need an intermediate 2.1.7 installation.

For manual upgrades, run `KSATClientSetup-2.1.8.exe` over the existing client
after finishing active tests and closing KSAT. It retains that PC's server
connection. Use `KSATLabPackageBuilder-2.1.8.exe` with the lab's existing public
connection files to prepare installers for new PCs. Keep ProgramData intact.

## Coordinator and version display

The test-screen API now retains each question's source identifier. Its omission
prevented the existing full-question image replacement rule from running during
tests, despite passing isolated UI tests. Integration tests use the actual
Missing Number chapter questions `ch404-q0072` and `ch404-q0014` photographed by
the lab owner, passing API output into the shipping browser script. The loaded
image replaces duplicate directions and table/figure transcripts; a failed image
restores readable text. Supporting diagrams retain their necessary stem. These
reasoning banks do not need reimport for this fix.

Coordinator **2.2.8 source** adds a clearly labelled running version in the page
footer; its installer has not been built or published as part of this release.
Client **2.1.8** shows its running version below the assessment title, including
on login and error screens. Both labels come from the running application.

**Keep using Coordinator 2.2.7: it can deploy the 2.1.8 bundle. No server upgrade
or bank reimport is required for this release.** Automatic recovery and the
test-screen image fix are in the client, so updating only the server does not
deliver them to older clients.

These are private-lab pilot builds. Automated checks do not establish that a
physical lab rollout has succeeded; confirm one PC before broad deployment.
