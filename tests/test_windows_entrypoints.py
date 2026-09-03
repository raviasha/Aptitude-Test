import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as faculty_app

from client_app import (
    ClientConfig,
    ClientConfigStore,
    ClientProcessLock,
    ClientRuntimeConfigStore,
    install_client_configuration,
    main as client_main,
    production_bind,
    update_client_coordinator_url,
)
from ksat.coordinator.tls import load_or_create_coordinator_security
from coordinator_main import (
    CoordinatorRuntimeSettings,
    main as coordinator_main,
    run_coordinator,
    save_runtime_settings,
)
from ksat.coordinator.process_lock import CoordinatorLockHeld, CoordinatorProcessLock


class WindowsEntrypointTests(unittest.TestCase):
    def test_version_is_consistent_and_build_endpoint_exposes_2_0_0(self):
        self.assertEqual("2.0.0", faculty_app.APP_VERSION)
        response = faculty_app.build_information()
        self.assertEqual({"version": "2.0.0"}, response)

    def test_coordinator_defaults_to_private_network_https_and_programdata(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = CoordinatorRuntimeSettings.from_environment(
                {
                    "ProgramData": directory,
                    "COMPUTERNAME": "LAB-SERVER",
                }
            )
        self.assertEqual(Path(directory) / "KSAT Coordinator", settings.data_dir)
        self.assertEqual("lab-server.local", settings.hostname)
        self.assertEqual("0.0.0.0", settings.bind_host)
        self.assertEqual(8443, settings.port)
        self.assertTrue(settings.interactive)

    def test_coordinator_run_locks_before_security_and_lazy_app_import(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = CoordinatorRuntimeSettings(
                data_dir=Path(directory),
                hostname="ksat.example.edu",
                bind_host="0.0.0.0",
                port=9443,
                interactive=False,
            )
            events = []
            fake_security = SimpleNamespace(
                browser_session_secret="browser-secret",
                client_session_secret="client-secret",
                server_certificate_path=Path(directory) / "server.crt.pem",
                server_private_key_path=Path(directory) / "server.key.pem",
            )

            class FakeLock:
                held = False
                data_dir = settings.data_dir

                def acquire(self):
                    self.held = True
                    events.append(("lock",))
                    return self

                def release(self):
                    events.append(("unlock",))
                    self.held = False

            def security_loader(*args, **kwargs):
                events.append(("security", args, kwargs))
                return fake_security

            def app_loader():
                events.append(("app", dict(os.environ)))
                return SimpleNamespace(state=SimpleNamespace())

            runs = []
            held_during_run = []
            run_coordinator(
                settings,
                security_loader=security_loader,
                app_loader=app_loader,
                lock_factory=lambda _path: FakeLock(),
                uvicorn_runner=lambda application, **kwargs: (
                    held_during_run.append(application.state.coordinator_process_lock.held),
                    runs.append((application, kwargs)),
                ),
            )

        self.assertEqual(["lock", "security", "app", "unlock"], [event[0] for event in events])
        imported_environment = events[2][1]
        self.assertEqual(str(settings.data_dir), imported_environment["KSAT_DATA_DIR"])
        self.assertEqual("browser-secret", imported_environment["SESSION_SECRET"])
        self.assertEqual("1", imported_environment["KSAT_HTTPS_ONLY"])
        self.assertEqual("client-secret", imported_environment["KSAT_SESSION_SECRET"])
        self.assertEqual([True], held_during_run)
        self.assertFalse(runs[0][0].state.coordinator_process_lock_release_on_shutdown)
        self.assertEqual(
            {
                "host": "0.0.0.0",
                "port": 9443,
                "ssl_certfile": str(fake_security.server_certificate_path),
                "ssl_keyfile": str(fake_security.server_private_key_path),
                "log_level": "warning",
                "use_colors": False,
            },
            runs[0][1],
        )

    def test_mutable_runtime_url_store_cannot_change_immutable_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = root / "coordinator-ca.pem"
            ca.write_text("public-ca", encoding="ascii")
            trusted = ClientConfig(
                coordinator_base_url="https://old.example.edu:8443",
                trusted_ca_path=str(ca),
                coordinator_signing_public_key_b64="eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
            )
            store = ClientRuntimeConfigStore(
                ClientConfigStore(root / "client-config.json"),
                root / "coordinator-url.json",
            )
            ClientConfigStore(root / "client-config.json").save(trusted)
            updated = store.update_base_url("https://new.example.edu:9443")
            self.assertEqual("https://new.example.edu:9443", updated.coordinator_base_url)
            immutable = ClientConfigStore(root / "client-config.json").load()
            self.assertEqual("https://old.example.edu:8443", immutable.coordinator_base_url)
            (root / "attacker-ca.pem").write_text("attacker", encoding="ascii")
            hostile = type(immutable)(
                coordinator_base_url="https://new.example.edu:9443",
                trusted_ca_path=str(root / "attacker-ca.pem"),
                coordinator_signing_public_key_b64=immutable.coordinator_signing_public_key_b64,
            )
            with self.assertRaises(ValueError):
                store.save(hostile)

    def test_mutable_runtime_url_store_restores_previous_url_on_readback_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = root / "coordinator-ca.pem"
            ca.write_text("public-ca", encoding="ascii")
            trusted = ClientConfig(
                coordinator_base_url="https://old.example.edu:8443",
                trusted_ca_path=str(ca),
                coordinator_signing_public_key_b64="eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
            )
            immutable_store = ClientConfigStore(root / "client-config.json")
            immutable_store.save(trusted)
            store = ClientRuntimeConfigStore(
                immutable_store,
                root / "coordinator-url.json",
            )
            store.update_base_url("https://old.example.edu:8443")
            previous = store.url_path.read_bytes()
            original_load = store.load
            calls = 0

            def fail_first_readback():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ValueError("simulated readback failure")
                return original_load()

            store.load = fail_first_readback
            with self.assertRaisesRegex(ValueError, "simulated readback failure"):
                store.save(ClientConfig(
                    coordinator_base_url="https://new.example.edu:9443",
                    trusted_ca_path=trusted.trusted_ca_path,
                    coordinator_signing_public_key_b64=(
                        trusted.coordinator_signing_public_key_b64
                    ),
                ))
            self.assertEqual(previous, store.url_path.read_bytes())

    def test_mutable_runtime_url_store_rolls_back_post_replace_fsync_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = root / "coordinator-ca.pem"
            ca.write_text("public-ca", encoding="ascii")
            trusted = ClientConfig(
                coordinator_base_url="https://old.example.edu:8443",
                trusted_ca_path=str(ca),
                coordinator_signing_public_key_b64="eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
            )
            immutable_store = ClientConfigStore(root / "client-config.json")
            immutable_store.save(trusted)
            store = ClientRuntimeConfigStore(immutable_store, root / "coordinator-url.json")
            store.update_base_url(trusted.coordinator_base_url)
            previous = store.url_path.read_bytes()
            original_open = Path.open
            failed = False

            def fail_first_target_fsync(path, mode="r", *args, **kwargs):
                nonlocal failed
                if path == store.url_path and mode == "rb+" and not failed:
                    failed = True
                    raise OSError("simulated post-replace fsync failure")
                return original_open(path, mode, *args, **kwargs)

            replacement = ClientConfig(
                coordinator_base_url="https://new.example.edu:9443",
                trusted_ca_path=trusted.trusted_ca_path,
                coordinator_signing_public_key_b64=trusted.coordinator_signing_public_key_b64,
            )
            with patch.object(Path, "open", fail_first_target_fsync):
                with self.assertRaisesRegex(OSError, "simulated post-replace"):
                    store.save(replacement)
            self.assertEqual(previous, store.url_path.read_bytes())

    def test_client_url_update_refuses_while_client_process_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            program_data = Path(directory)
            data_dir = program_data / "KSAT Client"
            (data_dir / "state").mkdir(parents=True)
            owner = ClientProcessLock(data_dir / "state").acquire()
            try:
                with self.assertRaisesRegex(
                    CoordinatorLockHeld, "lab client data directory"
                ):
                    update_client_coordinator_url(
                        program_data, base_url="https://new.example.edu:9443"
                    )
            finally:
                owner.release()

    def test_coordinator_install_config_is_persisted_and_loaded_without_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            program_data = Path(directory)
            saved = save_runtime_settings(
                program_data,
                hostname="KSAT-SERVER.EXAMPLE.EDU",
                port=9443,
            )
            self.assertEqual("ksat-server.example.edu", saved.hostname)
            loaded = CoordinatorRuntimeSettings.from_environment(
                {"ProgramData": str(program_data), "COMPUTERNAME": "ignored"}
            )
            self.assertEqual(saved, loaded)
            runs = []
            result = coordinator_main(
                [
                    "--install-config",
                    "--hostname",
                    "ksat-server.example.edu",
                    "--port",
                    "9443",
                ],
                environ={"ProgramData": str(program_data)},
                coordinator_runner=lambda *_args, **_kwargs: runs.append(True),
            )
            self.assertEqual(0, result)
            self.assertEqual([], runs)

    def test_coordinator_certificate_renewal_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            program_data = Path(directory)
            save_runtime_settings(
                program_data, hostname="ksat-server.example.edu", port=9443
            )
            renewals = []
            runs = []
            held = []

            class RecordingLock:
                def __init__(self, data_dir):
                    self.data_dir = data_dir
                    self.is_held = False

                def __enter__(self):
                    self.is_held = True
                    held.append(self)
                    return self

                def __exit__(self, *_exc):
                    self.is_held = False
            with self.assertRaises(SystemExit):
                coordinator_main(
                    ["--renew-certificate"],
                    environ={"ProgramData": str(program_data)},
                    coordinator_runner=lambda *_args, **_kwargs: runs.append(True),
                    certificate_renewer=lambda *_args, **_kwargs: renewals.append(True),
                )
            result = coordinator_main(
                ["--renew-certificate", "--confirm-renewal"],
                environ={"ProgramData": str(program_data)},
                coordinator_runner=lambda *_args, **_kwargs: runs.append(True),
                certificate_renewer=lambda *args, **kwargs: renewals.append(
                    (args, kwargs, held[-1].is_held)
                ),
                lock_factory=RecordingLock,
            )
            self.assertEqual(0, result)
            self.assertEqual([], runs)
            self.assertEqual(1, len(renewals))
            self.assertEqual(program_data / "KSAT Coordinator", renewals[0][0][0])
            self.assertTrue(renewals[0][1]["confirmed"])
            self.assertTrue(renewals[0][2])
            self.assertFalse(held[-1].is_held)

    def test_coordinator_validate_config_uses_strict_canonical_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            program_data = Path(directory)
            save_runtime_settings(
                program_data, hostname="ksat-server.example.edu", port=9443
            )
            self.assertEqual(
                0,
                coordinator_main(
                    ["--validate-config"],
                    environ={"ProgramData": str(program_data)},
                ),
            )
            path = program_data / "KSAT Coordinator" / "coordinator-runtime.json"
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "configuration is invalid"):
                coordinator_main(
                    ["--validate-config"],
                    environ={"ProgramData": str(program_data)},
                )

    def test_importing_coordinator_entrypoint_does_not_import_faculty_app(self):
        code = (
            "import sys; import coordinator_main; "
            "raise SystemExit(1 if 'app' in sys.modules else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_app_lifespan_borrows_preheld_entrypoint_lock_without_reacquiring(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory).resolve()
            owner = CoordinatorProcessLock(data_dir).acquire()
            original_config = faculty_app.app.state.coordinator_config
            writer = SimpleNamespace(start=lambda: None, stop=lambda: None)
            faculty_app.app.state.coordinator_process_lock = owner
            faculty_app.app.state.coordinator_process_lock_release_on_shutdown = False
            faculty_app.app.state.coordinator_config = SimpleNamespace(
                submission_writer=writer
            )
            try:
                with patch.object(faculty_app, "DATA_DIR", data_dir), patch.object(
                    faculty_app, "ensure_schema"
                ), patch.object(
                    faculty_app, "recover_artifact_quarantine"
                ), patch.object(faculty_app, "seed_data"), patch.object(
                    faculty_app, "copy_starter_question_files"
                ), patch.object(
                    faculty_app, "warm_pack_registry"
                ), patch.object(
                    faculty_app, "close_pack_registry"
                ):
                    faculty_app.startup()
                    faculty_app.shutdown_submission_writer()
                self.assertTrue(owner.held)
            finally:
                owner.release()
                faculty_app.app.state.coordinator_process_lock = None
                faculty_app.app.state.coordinator_process_lock_release_on_shutdown = True
                faculty_app.app.state.coordinator_config = original_config

    def test_client_production_bind_is_loopback_only_and_smoke_override_is_guarded(self):
        self.assertEqual(("127.0.0.1", 8010), production_bind({}))
        self.assertEqual(
            ("127.0.0.1", 49123),
            production_bind({"KSAT_SMOKE_TEST": "1", "KSAT_CLIENT_PORT": "49123"}),
        )
        with self.assertRaises(ValueError):
            production_bind({"KSAT_CLIENT_PORT": "49123"})
        with self.assertRaises(ValueError):
            production_bind({"KSAT_SMOKE_TEST": "1", "KSAT_CLIENT_HOST": "0.0.0.0"}),

    def test_client_service_mode_dispatches_stateful_server_to_windows_scm(self):
        dispatches = []
        uvicorn_runs = []
        result = client_main(
            ["--windows-service"],
            environ={},
            uvicorn_runner=lambda *_args, **_kwargs: uvicorn_runs.append(True),
            windows_service_runner=lambda name, target: dispatches.append((name, target)),
        )
        self.assertEqual(0, result)
        self.assertEqual("KSATLabClientAuthority", dispatches[0][0])
        self.assertTrue(callable(dispatches[0][1]))
        self.assertEqual([], uvicorn_runs)

    def test_client_launcher_only_opens_fixed_loopback_service_url(self):
        opened = []
        uvicorn_runs = []
        result = client_main(
            ["--open-client"],
            environ={},
            browser_opener=lambda url: opened.append(url),
            uvicorn_runner=lambda *_args, **_kwargs: uvicorn_runs.append(True),
        )
        self.assertEqual(0, result)
        self.assertEqual(["http://127.0.0.1:8010/"], opened)
        self.assertEqual([], uvicorn_runs)

    def test_client_console_server_is_restricted_to_explicit_smoke_mode(self):
        with self.assertRaisesRegex(PermissionError, "smoke"):
            client_main(["--service-console"], environ={}, uvicorn_runner=lambda *_a, **_k: None)
        runs = []
        result = client_main(
            ["--service-console"],
            environ={"KSAT_SMOKE_TEST": "1", "KSAT_CLIENT_PORT": "49123"},
            uvicorn_runner=lambda application, **kwargs: runs.append((application, kwargs)),
        )
        self.assertEqual(0, result)
        self.assertEqual("127.0.0.1", runs[0][1]["host"])
        self.assertEqual(49123, runs[0][1]["port"])

    def test_client_install_configuration_validates_and_persists_public_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = load_or_create_coordinator_security(
                root / "coordinator",
                hostname="ksat.example.edu",
                port=8443,
                lan_ip_addresses=["10.0.0.8"],
            )
            installed = install_client_configuration(
                root / "program-data",
                base_url="https://KSAT.EXAMPLE.EDU:8443/",
                ca_source=coordinator.public_export_dir / "coordinator-ca.pem",
                metadata_source=coordinator.public_export_dir / "coordinator-public.json",
            )
            self.assertEqual("https://ksat.example.edu:8443", installed.coordinator_base_url)
            config_path = root / "program-data" / "KSAT Client" / "client-config.json"
            self.assertEqual(installed, ClientConfigStore(config_path).load())
            self.assertEqual(
                coordinator.ca_certificate_pem,
                Path(installed.trusted_ca_path).read_bytes(),
            )

    def test_client_install_configuration_rejects_mismatched_bundle_without_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = load_or_create_coordinator_security(
                root / "coordinator",
                hostname="ksat.example.edu",
                lan_ip_addresses=["10.0.0.8"],
            )
            metadata = coordinator.public_export_dir / "coordinator-public.json"
            value = metadata.read_text("utf-8").replace(
                '"ca_sha256":"', '"ca_sha256":"' + "0" * 64
            )
            hostile_metadata = root / "hostile.json"
            hostile_metadata.write_text(value, encoding="utf-8")
            with self.assertRaises(ValueError):
                install_client_configuration(
                    root / "program-data",
                    base_url="https://ksat.example.edu:8443",
                    ca_source=coordinator.public_export_dir / "coordinator-ca.pem",
                    metadata_source=hostile_metadata,
                )
            self.assertFalse((root / "program-data" / "KSAT Client").exists())

    def test_client_install_cli_seeds_configuration_without_starting_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = load_or_create_coordinator_security(
                root / "coordinator",
                hostname="ksat.example.edu",
                lan_ip_addresses=["10.0.0.8"],
            )
            runs = []
            result = client_main(
                [
                    "--install-config",
                    "--base-url",
                    "https://ksat.example.edu:8443",
                    "--ca",
                    str(coordinator.public_export_dir / "coordinator-ca.pem"),
                    "--metadata",
                    str(coordinator.public_export_dir / "coordinator-public.json"),
                ],
                environ={"ProgramData": str(root / "program-data")},
                uvicorn_runner=lambda *_args, **_kwargs: runs.append(True),
            )
            self.assertEqual(0, result)
            self.assertEqual([], runs)
            self.assertTrue(
                (root / "program-data" / "KSAT Client" / "client-config.json").is_file()
            )

    def test_client_validate_config_rejects_malformed_effective_url_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = load_or_create_coordinator_security(
                root / "coordinator",
                hostname="ksat.example.edu",
                lan_ip_addresses=["10.0.0.8"],
            )
            program_data = root / "program-data"
            install_client_configuration(
                program_data,
                base_url="https://ksat.example.edu:8443",
                ca_source=coordinator.public_export_dir / "coordinator-ca.pem",
                metadata_source=coordinator.public_export_dir / "coordinator-public.json",
            )
            client_data = program_data / "KSAT Client"
            (client_data / "coordinator-url.json").write_bytes(
                b'{"coordinator_base_url":"https://ksat.example.edu:8443"} '
            )
            with self.assertRaisesRegex(ValueError, "configuration is invalid"):
                client_main(
                    ["--validate-config"],
                    environ={"ProgramData": str(program_data)},
                )

    def test_client_url_update_cli_requires_administrator_before_work(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PermissionError, "Administrator"):
                client_main(
                    ["--update-config", "--base-url", "https://new.example.edu:8443"],
                    environ={"ProgramData": directory},
                    administrator_check=lambda: False,
                )
            self.assertFalse((Path(directory) / "KSAT Client").exists())


if __name__ == "__main__":
    unittest.main()
