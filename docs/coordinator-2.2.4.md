# Coordinator 2.2.4 — publisher trust setup

[Download the installer](../release/coordinator-2.2.4/KSATCoordinatorSetup-2.2.4.exe)
and check its [SHA-256 checksum](../release/coordinator-2.2.4/SHA256SUMS.txt).

This update fixes missing Windows publisher trust that prevented some servers
from accepting the signed Client 2.1.6 update bundle. During
administrator-approved setup, the installer verifies the bundled public KSAT
publisher certificate and adds missing entries to the machine's Trusted Root
and Trusted Publishers stores. No separate PowerShell command is needed.
Existing trust is reused and retained on uninstall because other KSAT apps
share it. This private-lab certificate is not a publicly trusted publisher.

1. Finish active exams and uploads, then stop the Coordinator.
2. Run **KSATCoordinatorSetup-2.2.4.exe** over the existing installation on
   each server and approve Windows administrator access. Do not delete
   `C:\ProgramData\KSAT Coordinator` or uninstall the existing application.
3. Start the Coordinator and confirm **Build 2.2.4**. Existing logins,
   question banks, results, network settings and lab identities are preserved.
4. In **Faculty > Client updates**, upload the existing
   [Client 2.1.6 bundle](https://github.com/raviasha/Aptitude-Test/releases/download/client-v2.1.6/KSATClientUpdate-2.1.6.ksat-client-update).
   Follow the [pilot and rollout steps](client-2.1.6-visual-update.md).

Client and Lab Package Builder remain **2.1.6**. No new client installation,
connection files or question-bank reimport is required for this server fix.
The standalone EXE does not perform the installer trust step; use the installer.

Setup stops if certificate validation, clock validity or certificate-store
access fails. Check the server date/time and ask its administrator to check
certificate-store permissions or security policy before retrying. Update
signature verification remains enabled. Only the public signing certificate
is bundled; lab TLS certificates and private keys are not replaced.

Publisher: `CN=KSAT LAB RELEASE SIGNING`  
Thumbprint: `13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908`

See the [build verification](../release/coordinator-2.2.4/verification.json).
Automated tests use isolated certificate-store substitutes; no test changes
this build PC's trusted certificates. Installation on an untrusted lab server
and the subsequent Client 2.1.6 upload remain operator checks, not completed
deployment claims. The [2.2.3 guide](coordinator-2.2.3.md) describes the visual
fixes retained in this version.
