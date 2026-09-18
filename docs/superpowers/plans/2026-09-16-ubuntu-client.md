# Ubuntu Client Implementation Plan

> Execute in the existing isolated release worktree, using the executing-plans workflow.

**Goal:** Deliver x86-64 Ubuntu 18.04 and 22.04 client installers that use the existing Windows coordinator.

**Architecture:** Reuse client_app, the signed protocol, local SQLite runtime, and browser UI. Inject a Linux identity protector through the production service loader. Run as a dedicated system account under systemd; distribute a frozen executable inside a Debian package. Keep Windows defaults unchanged.

**Tech Stack:** Python, FastAPI, cryptography, PyInstaller, dpkg, systemd, WSL2 build environments.

**Spec:** User-approved Ubuntu 18.04/22.04 clients with login, launched tests, local persistence, sealed submission, and released answer review.

## Constraints

- Preserve server protocol and Windows behavior.
- Bind only 127.0.0.1:8010; protect private state from ordinary student accounts.
- Do not ship identities, server credentials, answer keys, or test databases.
- Keep state and configuration on upgrades and package removal.
- Linux device secrets use authenticated encryption with a separate root-owned key, readable by the service account; this is OS-permission protection, not hardware binding.
- Build and execute each artifact on the intended Ubuntu version. Clearly distinguish automated WSL tests from physical-desktop exam acceptance.

## Tasks

- [x] Establish Ubuntu 22.04 and 18.04 build environments and compatible Python runtimes.
- [x] Add Linux protector and entrypoint with setup, validation, service, and version commands. Test identity persistence/tamper rejection, permissions, setup reuse, and actual HTTP startup.
- [x] Allow an optional protector in the existing production services factory; rerun Windows entrypoint and identity tests.
- [x] Add Debian packaging: dedicated ksat-client user, systemd unit, desktop launcher, explicit trust setup, stopped-service upgrade, persistent data.
- [x] Freeze and package separately on both targets, record dependency versions and hashes.
- [x] Install/reinstall both packages in WSL; test service startup, loopback binding, state preservation, and client protocol integration. Deliver installers with setup and verification instructions.

Pilot handoff: see `docs/ubuntu-client-build-verification.md` for passing focused checks, the broader-suite limitations, and outstanding physical-desktop acceptance. No claim that all 245 inherited tests passed.

## Verification commands

Windows: `.build-venv/Scripts/python.exe -m unittest tests.test_windows_entrypoints tests.test_client_identity -q`.

Linux: run the Linux-specific tests and existing client API/runtime/outbox/review tests with the build interpreter. Build using `python scripts/build_linux_client.py --target <18.04|22.04>`, then inspect `dpkg-deb --info`, install using `dpkg -i`, verify `systemctl status ksat-client`, and repeat installation after recording identity/configuration hashes.
