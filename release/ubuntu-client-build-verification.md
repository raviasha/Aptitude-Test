# Ubuntu client pilot build — 16 September 2026

## What was built

Separate amd64 Debian packages were built inside Ubuntu 18.04.5 (glibc 2.27) and Ubuntu 22.04.5 (glibc 2.35), under WSL2. These are pilot builds, not a claim of physical lab acceptance. No coordinator upgrade is required. The Windows installers and live Windows data were not modified.

Both packages bundle Python 3.11.16 and the pinned dependencies in `installer/linux/requirements-build.txt`. Ubuntu 18.04 also bundles SQLite 3.46.1 because its system SQLite 3.22 cannot execute the client's existing UPSERT statements. Ubuntu 22.04 uses its compatible SQLite 3.37.2. No Python installation is required on student PCs.

Source is the existing release worktree `codex/fix-faculty-exam-timer`, base commit `c09b80527e98d14e463c6f9a948568eb77bcc48f`, plus the uncommitted Linux additions. Build manifests record the actual application source hashes; the base commit alone is not the full source of this build. Linux protection is injected into the existing client loader; the Windows DPAPI default is unchanged.

## Verification results

- **227 client tests passed on each Ubuntu release**: Linux identity/configuration, client HTTP API, runtime, store, outbox, coordinator adapter, pending review and sealed assessment review.
- **38 Windows entrypoint and identity regression tests passed.**
- **Actual installed package smoke check passed on both Ubuntu releases**: systemd startup, HTTPS enrollment, student login, content download, test start, answer save, service restart/recovery, submission acknowledgement, released answer review, same-package reinstall, unchanged identity/configuration/wrapping key, retained result and review.
- The installed service listened on `127.0.0.1:8010`, not a LAN interface. An ordinary account could not read the Linux wrapping key.
- Ubuntu 18.04 SQLite source SHA3-256 matched the value published by SQLite for 3.46.1. The frozen package includes `libsqlite3.so.0`; the systemd service does not need the build environment's library path.
- Package checksums are supplied next to each installer. No fixture identity, database, or private coordinator key is included.
- A separate read-only code review found no critical or important blockers in the Linux entrypoint, protector, packaging, or shared-loader injection. It did not independently rerun tests.

### Known automated-test limitations

The larger 245-test run is **not entirely green**. The inherited load benchmark's `test_load_gate_measures_the_requested_client_start_spread` failed on both systems: it requests a 0.2-second spread but measures signed ticket timestamps with whole-second precision, producing 0.0 in these runs. This benchmark harness issue has not been changed as part of Linux packaging.

One HTTPS review/restart test also failed in the first concurrent Ubuntu 22.04 run because its fixture did not recover the expected active attempt. It passed when rerun alone; the installed-package restart/review test passed. This intermittent fixture result is recorded rather than presented as a clean full-suite pass.

WSL distributions share networking: installed-service smoke checks must run sequentially with the other distribution's service stopped. Earlier parallel smoke attempts conflicted on port 8010. Fresh sequential checks passed. Initial Python 3.10 builds and the initial Ubuntu 18.04 build using system SQLite were superseded and are not delivered.

## Physical acceptance still required

Use one ordinary student account on one physical Ubuntu 18.04 PC and one Ubuntu 22.04 PC. Verify the Windows coordinator connection, text/images, timing, fullscreen departure and desktop switching, brief network loss/recovery, submission, faculty results, and answer review. A browser-based client is not OS-level kiosk lockdown. Keep the pilot label until these checks pass.

## Rebuild notes

Use the exact target Ubuntu release on x86-64 with `build-essential`, `libssl-dev`, `zlib1g-dev`, `libffi-dev`, `libsqlite3-dev`, `libbz2-dev`, `liblzma-dev`, `libreadline-dev`, `binutils`, and `python3-venv` or an independently built Python. Build Python 3.11.16 from the official Python source with a shared runtime:

```sh
./configure --prefix=/opt/ksat-python --enable-shared LDFLAGS=-Wl,-rpath,/opt/ksat-python/lib
make -j4
make install
/opt/ksat-python/bin/python3.11 -m venv /opt/ksat-build-env
/opt/ksat-build-env/bin/pip install -r installer/linux/requirements-build.txt
```

On Ubuntu 18.04, build SQLite 3.46.1 (`sqlite-autoconf-3460100.tar.gz`, official SQLite 2024 archive) with `./configure --prefix=/opt/ksat-sqlite`, `make -j4`, `make install`. Its `sqlite3.c` SHA3-256 must be `186a1baa476b6d546de155160ca6d30ff7b7e6ee375f0bb6445e1a3d180a7dad`. Set `LD_LIBRARY_PATH=/opt/ksat-sqlite/lib` for the source tests and PyInstaller build so that the new library is bundled, not the incompatible system version. Do not modify the system SQLite library.

From the release source directory, use the appropriate build interpreter:

```sh
python scripts/build_linux_client.py --target 18.04 \
  --source-revision c09b80527e98d14e463c6f9a948568eb77bcc48f \
  --output /opt/ksat-artifacts
```

Substitute `22.04` on that release. The script rejects Python older than 3.11 and SQLite older than 3.35. Install with APT. On a **fresh disposable WSL distribution only**, run `python -m scripts.check_linux_package --package /opt/ksat-artifacts/KSATClient-Ubuntu-18.04-amd64.deb`. This creates test client state and refuses to overwrite existing configuration. It must never be run on a real lab installation.

See `ubuntu-client-installation.md` for user installation and upgrade steps.
