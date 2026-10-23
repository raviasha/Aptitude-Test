# Client 2.1.6 — visual update and server rollout

Client 2.1.6 and matching Lab Package Builder 2.1.6 are signed private-lab
pilot builds alongside Coordinator 2.2.3. Automated artifact checks passed;
the real installed-PC pilot remains required. The existing release publisher
and signing identity are unchanged.

## Downloads

Large artifacts are distributed as GitHub Release assets under `client-v2.1.6`.

| Artifact | Download |
| --- | --- |
| Direct Windows installation/upgrade | [KSATClientSetup-2.1.6.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.6/KSATClientSetup-2.1.6.exe) |
| Lab-specific installer generator | [KSATLabPackageBuilder-2.1.6.exe](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.6/KSATLabPackageBuilder-2.1.6.exe) |
| Signed server-managed update | [KSATClientUpdate-2.1.6.ksat-client-update](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.6/KSATClientUpdate-2.1.6.ksat-client-update) |

## What the complete fix requires

Use **Coordinator 2.2.3 plus Client 2.1.6**. The server removes matching
reasoning textbook-number prefixes when creating new assessment releases and
converts supporting context images into the established image-stimulus format.
Client 2.1.5 remains supported by that format, but the full UI fix requires 2.1.6.

In tests and answer review, Client 2.1.6 hides duplicate question/option text
only once a reviewed complete replacement image has loaded successfully. This
replacement behavior is scoped to reasoning source chapters 401–441. Text
remains available while loading and is restored on image failure; option labels
remain visible. Supporting diagrams supplement the stem and do not hide it.
If a supporting image fails, its transcription/description is shown as fallback.

Existing reasoning ZIPs need no reimport. Newly created tests/releases are
needed for the server's number cleanup; upgrading does not rewrite old releases.
For quantitative corrections, import the six revisioned chapter banks and select
them for new tests. See the [Coordinator and bank guide](coordinator-2.2.3.md)
for both ZIPs, the 30 added diagrams, the 306 chart/table deduplications
and preservation of old history and answers.

## Update existing Windows clients from the server

1. Upgrade the Coordinator to 2.2.3 using its guide. Finish active exams/uploads and stop
   the server for installation, then restart it for client rollout.
2. Sign in to **Faculty > Client updates**. Choose
   `KSATClientUpdate-2.1.6.ksat-client-update` and click **Verify and upload**.
   Upload the signed bundle here; the client installer and builder EXEs are not
   substitutes for that bundle.
3. Select an active enrolled Windows computer under **Pilot lab computer** and
   click **Start pilot**. Open KSAT on that PC with no active assessment or
   pending submission and connect to its existing Coordinator.
4. Wait for the selected pilot to report **healthy** with installed version
   **2.1.6** for this exact release. Check normal student launch/sign-in and a
   newly released sample assessment, including diagrams and answer review.
   Complete the real administrator/standard-user installation pilot before
   wider deployment. These checks have not yet been recorded for this release.
5. Click **Publish to all clients**. This action is disabled until the exact
   pilot health/version gate passes. Open KSAT on the remaining lab PCs and
   let eligible idle clients update before student login on start/connect.
6. Monitor the release's device list for **Updated**, **Downloading**,
   **Waiting**, **Offline** or **Failed**. An offline or deferred PC is not
   evidence of a completed update; bring it online/open KSAT and resolve its
   saved work before retrying.

Configured devices enroll automatically using their computer name. The dashboard
uses enrolled device identities; central rollout needs no hand-maintained
hostname list or per-PC push script. Newly installed PCs still need their lab's
connection profile, supplied by the lab-specific installer or normal setup.

Managed updates defer while an active test or pending upload exists. Finish the
test and allow uploads to be acknowledged before retrying; never clear the
queue to force installation. A fully closed client does not run background
updates or upload retries. Open KSAT and connect it to participate. The server
must be reachable for the bundle download. This managed rollout is for Windows;
Ubuntu is unsupported.

## Direct installation and lab packages

Use Builder **2.1.6** to generate installers containing Client **2.1.6**, with
each lab's existing server URL and its two public connection files. Distribute
the generated lab installer to student PCs. Older builders and previously
generated EXEs do not update themselves; regenerate them with the matching
builder for future installations.

Install over the existing client with administrator approval. Retain
`C:\ProgramData\KSAT Client`, saved submissions, device identity and connection
settings. Do not uninstall, delete ProgramData, replace the Coordinator/device
identity or silently move an existing PC to another server.

Finish active tests and upload results first. The 2.1.5 interactive-upgrade
behavior is retained: a normal, non-silent same-server installation can preserve
pending submissions when an old client cannot connect. It does not acknowledge
or discard them; reopen KSAT afterward to resume eligible retries. Managed and
silent updates still defer pending uploads. For legacy clients without the
installation-safety handshake, follow the existing
[installation instructions](lab-client-installation.md) before stopping a service.

See the actual [2.1.6 checksums](../release/client-2.1.6/SHA256SUMS.txt) and
[verification record](../release/client-2.1.6/verification.json). Automated
checks do not substitute for the physical installed-PC pilot described above.
