# KSAT Lab Client Package Builder design

Date: 27 September 2026

Status: Approved by the user on 27 September 2026; implementation has not started.

## Purpose and agreed workflow

Faculty needs a separate Windows application, **KSAT Lab Package Builder.exe**, to create one self-contained client installer for each lab. On the administrator computer, faculty enters the Coordinator URL, selects its two public connection files, names the lab and chooses an output folder. The resulting installer is copied to every student PC in that lab. Students do not enter URLs, browse for certificates or run PowerShell commands. An authorised administrator still approves Windows elevation.

The same application and updater serve all labs. Only the initial connection profile differs. Subsequent centrally published client releases preserve that profile, so routine updates do not require rebuilding each lab installer.

This design assumes a designated, trusted Windows packaging computer with a one-time signing/tool setup. It does not promise that the builder can create trusted installers on an arbitrary PC without that setup. No server credentials or private keys are requested as lab configuration.

## Scope and alternatives

Selected approach: a local graphical builder compiles and signs a lab-specific edition of the existing Inno Setup client installer, using approved prebuilt client binaries. Reuse the existing installation, service, trust-validation and update paths rather than introducing a second installer inside a wrapper.

Alternatives not selected:

- A ZIP containing the generic installer, configuration and script is simpler but fails the user's single-EXE requirement.
- Generating installers inside Coordinator would introduce a server build/signing service and place additional privileges on assessment infrastructure. A separate faculty-side application was explicitly preferred.

In scope: builder UI, protected signing setup integration, reusable profile validation, a lab-enabled client installer, packaging checks and regression tests. Preserve the generic installer for current workflows.

Out of scope: remote deployment, automatic PC renaming, DNS/firewall administration, server self-updates, changing student authentication, a web-based package builder, migration to another Coordinator, and automatic CA rotation. No changes to exam scoring or review permissions.

## User interface

### One-time setup on the packaging computer

A **Builder setup** screen reports readiness of:

1. The approved client payload and installer template included with this builder release.
2. The supported Inno Setup compiler and Windows signing tool.
3. The selected Windows code-signing certificate and access to its private key.

Use Inno Setup 6.7.3 to match the current release inspection toolchain. The administrator installs the required build tools once using their official installers; the GUI detects them and provides a browse option and clear missing-tool instructions. Python and repository source are not prerequisites for the end user: the builder is packaged as a Windows application.

Provision the authorised code-signing certificate into the packaging computer's Windows certificate store once. Use its pinned thumbprint for signing. PFX provisioning is a separate administrator operation, not a per-lab field. Do not silently create a new signing identity or ask the user to send signing keys through chat. The existing institution/private-lab signing policy remains applicable.

### Per-lab screen

| Control | Behaviour |
| --- | --- |
| Lab name | Required readable label; used to derive a safe output filename. |
| Coordinator HTTPS URL | Required; can be filled from selected metadata and confirmed by the user. |
| Coordinator CA file | Browse for `coordinator-ca.pem`. |
| Coordinator metadata file | Browse for `coordinator-public.json`. |
| Output folder | Folder picker; no silent overwrites. |
| Test connection | Optional live TLS and compatible Coordinator health check. |
| Validate and Create Installer | Mandatory offline validation, compilation, signing and output inspection. |

Display the selected client version and server hostname prominently. Show separate progress stages for validation, compilation, signing and verification. Disable duplicate build requests while one is running. File dialogs and errors must be usable without a console window.

The builder remembers only the build-tool/certificate selection needed for its setup. V1 does not maintain a lab database or store copies of connection files automatically between sessions. The lab profile is constructed from the selected inputs for each build.

### Successful result

Produce `KSATClientSetup-<lab-slug>-<client-version>.exe`, display its SHA-256 and offer **Open output folder**. The version comes from the approved payload manifest rather than a hard-coded GUI value. A small adjacent build receipt records the version, profile fingerprint, signer thumbprint and installer hash for the administrator. Only the EXE needs to be distributed to students.

The builder does not automatically publish packages to GitHub, deploy clients, modify Coordinator or install the generated package on the packaging computer.

## Components and integration boundaries

- A small Windows GUI entry point owns file selection and progress presentation. Prefer the project's Python/Tkinter packaging pattern with a windowed frozen build; packaging/signing work runs outside the UI thread.
- A reusable lab-profile module parses and validates the URL, public metadata and CA. Extract reusable logic from `client_app.install_client_configuration` without introducing writes or changing the generic installer's validation behaviour.
- A packaging module constructs an isolated build directory, copies only allowed inputs, invokes the compiler/signing tools with argument lists, validates the output and publishes it atomically.
- A signing adapter uses the Windows certificate store rather than passing PFX passwords on command lines. Existing release-signing rules and publisher pins remain authoritative; no broad release-signing refactor is included.
- A lab mode in `installer/KSATClient.iss` bundles the public profile, skips editable URL/file-selection pages and installs the pinned publisher certificate. Generic mode retains its current interactive fields.
- A read-only client installation preflight reports version, existing Coordinator identity, active attempt and pending submission state using existing lifecycle/state mechanisms. It runs before installer mutations.

Package the approved client EXE, updater EXE, public update verification key, publisher certificate and installer template with the builder. Include a manifest that binds their versions and hashes. Verify executable signatures and pins before use. Do not allow the GUI to accept arbitrary replacement executables or arbitrary scripts.

No live database, device identity, cached test, result or private key is copied from the packaging computer into a generated installer. In particular, the generic frozen client remains lab-neutral; the lab profile belongs only to the installer resources.

## Lab profile validation

The installer embeds a versioned profile containing the lab label, canonical Coordinator URL, public metadata, CA certificate and hashes binding those resources. These bytes are included before Authenticode signing so the final installer signature covers the configuration as well as executable payloads. Never append an unsigned lab profile to an already signed installer.

Validation must:

- Accept only the metadata schema and URL rules supported by the current client. Require HTTPS, a host and a valid port; reject credentials, query strings, fragments and unsupported path components.
- Match the canonical URL to the metadata's `coordinator_url` using the existing normalization contract. Do not remove `.local`, resolve a hostname into a stored IP, or repair a mismatch silently.
- Verify the CA hash, signing-public-key fingerprint/binding and certificate validity with the existing trust rules. The selected PEM must contain the expected certificate and no private-key material.
- Enforce bounded input sizes, strict JSON parsing and supported schema versions. Reject private-key PEM blocks, unexpected executable/archive inputs and malformed or inconsistent public files.
- Derive the filename from a restricted slug while preserving the human-readable label only as data. Reject path traversal and never interpolate user text into Inno source or shell commands. The template reads a generated data resource rather than executable source assembled from user input.

Building away from the lab must be possible. Offline validation is mandatory; live connectivity testing is optional and separately reported. If a live check fails, show that the connection has not been verified and require explicit acknowledgement before building. A successful test on the packaging computer is not a claim that every lab PC can resolve the hostname.

## Signing and trust

Each newly generated installer must be signed after all lab resources are embedded. Signing uses the authorised certificate from the packaging computer's Windows store, selected by an exact expected thumbprint and publisher. Refuse missing, expired, unsuitable or mismatched identities; do not emit an unsigned success result. Apply the existing timestamp policy for the selected institution/private-lab signing profile.

The private signing key remains protected on the designated packaging computer and is not embedded in the builder distribution or generated installers. This tool does not need the private key used to sign central-update manifests, nor any Coordinator CA/server private key. Only the public software-publisher certificate is embedded in client packages.

On a client, installing publisher trust is an explicit part of the administrator-approved installation. Pin the embedded certificate identity; do not import arbitrary certificates selected by a student. Track certificates added by this installer and never remove pre-existing or shared trust entries indiscriminately. A client uninstall must not remove publisher trust still needed by another KSAT product.

A certificate imported during installation cannot retroactively make Windows trust the initial installer before launch. The builder and generated installers may still show Windows/SmartScreen warnings for a private-lab publisher. Do not disable protection, promise warning-free installation, or treat a signature as proof of malware clearance. Lab IT verifies the initial package and its distribution channel.

## Installation lifecycle and existing data

Keep the current client AppId, service identity, protected data locations and update verification key. A new lab installer is not a separate Windows product for each lab.

1. After administrator approval, display lab name, target Coordinator and client version with one **Install** action. No per-client configuration input is required.
2. Validate bundled resources and perform read-only preflight before changing trust, files, services or configuration. Reject a running assessment or unacknowledged submission with an actionable message; never silently stop an examination to install.
3. Coordinate preflight and installation with the existing lifecycle locking mechanism so another attempt cannot start between the safety check and service shutdown. Recheck protected state after quiescing, before replacing anything. If blocked, restore the previous service availability without altering data.
4. For a fresh PC, create configuration using the bundled public profile, install the required trust, service and updater, and let the client generate its own device identity.
5. For an existing client, compare the bundled profile to its effective saved URL and pinned Coordinator identity, not only the original installer JSON. Allow same-server upgrades while preserving identity, records, pending state and server configuration. A different CA, protocol key or effective URL must stop with a clear explanation and no automatic migration option in V1.
6. Reject normal interactive downgrades. Preserve the central updater's explicit recovery/rollback workflow; do not accidentally block an authorised last-known-good rollback.
7. Start the client service, verify local health and offer to open KSAT. Distinguish installation failure from successful local installation with the Coordinator temporarily unreachable. Provide a connection retry without destructive reinstallation.

Installer failures must not leave configuration pointing to a new server or discard saved records. Track new trust/configuration mutations, restore prior service availability where possible, and retain a minimal diagnostic if recovery is incomplete. Uninstallation behaviour continues to preserve student state according to the existing policy.

Extract inputs into a protected, uniquely named staging area and validate hashes immediately before privileged use. Untrusted users must not be able to swap certificates, executables or profile bytes between validation and installation. Clean only the exact staging directory owned by the operation; never follow a supplied path into unrelated data.

## Compatibility with central updates

Lab installers and the generic installer contain the same approved client/updater binaries. Use a new client release version for changed client installation/preflight behaviour; do not relabel a changed payload as the existing 2.1.0 release.

Future signed `.ksat-client-update` bundles remain lab-neutral. Their generic installer preserves each PC's existing Coordinator profile. The separate package builder is needed only for initial lab packages or deliberately refreshed initial-install media, not for every central update.

Preserve the existing last-known-good installer seed and verify that a lab-specific installer can be used for rollback on its original same-server client. No new per-lab update channel, signing key or manifest protocol is introduced. Coordinator requires no new build/signing endpoint.

## Failure messages and logging

Use distinct messages for missing compiler/signing tools, missing authorised key access, clock/certificate validity, malformed connection files, URL mismatch, failed network verification, compile/signature failure, existing-server conflict, active assessment and pending upload.

Logs contain operation stage, client version, diagnostic code and output/profile hash. They do not contain passwords, private keys, PFX contents, student records or whole raw configuration dumps. A user-initiated diagnostic export must make its contents visible. Cancellation before final publication removes only temporary build output and leaves existing packages unchanged.

## Verification and acceptance criteria

Automated coverage must include:

- Matching and mismatching URL/CA/metadata, malformed and oversized input, private-key rejection, invalid validity windows and unsupported schemas.
- Lab names and file paths containing spaces, Unicode, quotes and shell metacharacters; collision-safe output naming and no command/source injection.
- Missing or wrong signing identities, unavailable tools, interrupted compilation/signing, cancelled builds and no unsigned output marked successful.
- Recursive inspection of the finished installer: correct profile, only allowed public inputs, correct signed client/updater payloads, matching publisher pins and no private keys, device identities or student data.
- Fresh installation, same-server upgrade, different-server rejection before mutation, active-attempt/pending-upload protection, service recovery after failure and idempotent trust installation.
- Generic installer behaviour unchanged, future generic central update preserving lab configuration, and verified rollback to a lab-specific last-known-good installer.

Physical Windows acceptance uses clean disposable test machines and a test Coordinator:

1. Build two lab installers from distinct valid public profiles using the GUI on the prepared packaging PC.
2. Install one package on two clean student PCs with one administrator-approved setup flow each and no separate scripts or selected files. Verify distinct device identities.
3. Complete sign-in, a short launched test, submission, post-close review and sign-out. Restart both PCs and Coordinator and repeat connection checks.
4. Test an existing same-server client with retained records and a wrong-lab package that changes nothing. Verify that an active attempt and pending submission prevent an unsafe upgrade.
5. Publish a newer generic signed client update through the existing pilot mechanism and verify that both clients retain their own identity and the correct lab settings. Exercise rollback in the test environment.
6. Record results and installer hashes before distributing packages to a live lab. Never infer antivirus approval from successful build, signature or checksum checks alone.

## Delivery and review boundary

Deliver the signed builder application, its approved payload resources, a short administrator setup guide and a one-page student-PC installation guide. The administrator computer needs a one-time supported compiler/signing setup; student PCs need only the generated EXE and authorised elevation.

The exact real-lab name, URL and public files are runtime inputs, so they are not needed to implement or test the builder. Real lab packages are generated after those inputs are supplied to the tool. Any GitHub publication is a separate requested release action.

This specification is the current approval artifact. After user review, prepare a separate implementation plan with ordered work and verification steps, then obtain the user's execution choice. No application implementation or binary build is authorised merely by creating this specification.
