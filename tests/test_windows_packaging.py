import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from scripts.create_lab_signing_identity import create_lab_signing_identity

from scripts.windows_release import (
    APP_VERSION,
    _assert_payload_safe,
    _manifest_has_update_key,
    ReleaseLayout,
    SigningConfigurationError,
    build_commands,
    build_executables,
    coordinator_payload_manifest,
    create_ephemeral_test_signing_config,
    create_lab_client_update_bundle,
    lab_signing_config,
    create_test_client_update_bundle,
    inspect_release_inputs,
    publish_signed_executables,
    sign_and_verify_artifact,
    verify_authenticode_signature,
    write_update_public_key_resource,
    write_sha256s,
)


class WindowsPackagingTests(unittest.TestCase):
    def test_lab_identity_protects_directory_only_after_all_secrets_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "identity"

            def assert_complete_before_protection(path):
                for name in (
                    "KSATLabReleaseSigning.pfx",
                    "pfx-password.txt",
                    "update-signing-private.key",
                    "update-signing-public.key",
                ):
                    self.assertTrue((path / name).is_file(), name)

            with patch(
                "scripts.create_lab_signing_identity._protect_directory",
                side_effect=assert_complete_before_protection,
            ):
                create_lab_signing_identity(root, years=5)

    def test_persistent_lab_identity_has_separate_public_and_private_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = create_lab_signing_identity(root, years=5)

            self.assertEqual("CN=KSAT LAB RELEASE SIGNING", result["publisher"])
            self.assertEqual(32, (root / "update-signing-private.key").stat().st_size)
            self.assertEqual(32, (root / "update-signing-public.key").stat().st_size)
            certificate = x509.load_der_x509_certificate(
                (root / "KSATLabReleaseSigning.cer").read_bytes()
            )
            self.assertGreater(
                certificate.not_valid_after_utc,
                datetime.now(timezone.utc).replace(microsecond=0),
            )
            password = (root / "pfx-password.txt").read_text("ascii").strip()
            key, pfx_certificate, _chain = pkcs12.load_key_and_certificates(
                (root / "KSATLabReleaseSigning.pfx").read_bytes(),
                password.encode("ascii"),
            )
            self.assertIsNotNone(key)
            self.assertEqual(
                certificate.public_bytes(serialization.Encoding.DER),
                pfx_certificate.public_bytes(serialization.Encoding.DER),
            )
            public_names = {"KSATLabReleaseSigning.cer", "update-signing-public.key"}
            for name in public_names:
                self.assertNotIn(b"PRIVATE", (root / name).read_bytes().upper())

    def test_lab_signing_accepts_persistent_self_signed_identity_without_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = create_lab_signing_identity(root, years=5)
            config = lab_signing_config(
                pfx_path=root / "KSATLabReleaseSigning.pfx",
                password_environment_name="LAB_PASSWORD",
                expected_publisher=created["publisher"],
                environ={
                    "LAB_PASSWORD": (root / "pfx-password.txt").read_text("ascii").strip()
                },
            )
            self.assertTrue(config.lab_identity)
            self.assertIsNone(config.timestamp_url)
            self.assertEqual(created["thumbprint"], config.expected_thumbprint)

    def test_lab_bootstrap_pins_and_installs_public_release_certificate(self):
        root = Path(__file__).resolve().parents[1]
        bootstrap = (root / "scripts" / "bootstrap_windows_clients.ps1").read_text("utf-8")
        trust = (root / "scripts" / "install_lab_release_trust.ps1").read_text("utf-8")
        self.assertIn("[Parameter(Mandatory)] [string]$TrustCertificate", bootstrap)
        self.assertIn("SignerCertificate.Thumbprint", bootstrap)
        self.assertIn("Import-Certificate", bootstrap)
        self.assertIn("Cert:\\LocalMachine\\Root", bootstrap)
        self.assertIn("Cert:\\LocalMachine\\TrustedPublisher", bootstrap)
        self.assertIn("'@ | Set-Content -LiteralPath $script -Encoding UTF8", bootstrap)
        self.assertIn("$candidate.Thumbprint", trust)
        self.assertIn("Import-Certificate", trust)
    def test_secret_scan_rejects_pem_but_not_marker_text_inside_a_pe_binary(self):
        marker = b"-----BEGIN PRIVATE KEY-----"
        with self.assertRaisesRegex(ValueError, "private/live"):
            _assert_payload_safe("secret.txt", marker)
        _assert_payload_safe("dependency.dll", b"MZ" + marker)

    def test_inspector_recognizes_pyinstaller_typed_data_entry_names(self):
        self.assertTrue(
            _manifest_has_update_key({"C:b:update-release-public.json": "digest"})
        )

    def test_release_scripts_are_directly_invokable_from_repository_root(self):
        root = Path(__file__).resolve().parents[1]
        for script in ("windows_release.py", "build_client_update.py"):
            completed = subprocess.run(
                [sys.executable, str(root / "scripts" / script), "--help"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
        release_help = subprocess.run(
            [sys.executable, str(root / "scripts" / "windows_release.py"), "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("--lab-signing", release_help)
        self.assertIn("--update-signing-private-key", release_help)

    def test_release_writes_only_canonical_public_update_key_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "update-release-public.json"
            write_update_public_key_resource(b"p" * 32, target)
            value = json.loads(target.read_bytes())
            self.assertEqual(1, value["format_version"])
            self.assertNotIn("private", target.read_text("ascii").lower())
            self.assertEqual(target.read_bytes(), json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    def test_artifact_build_fails_closed_before_work_without_signing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SigningConfigurationError, "signing"):
                build_executables(Path(directory), Path("C:/Python/python.exe"))

    def test_batch_build_forwards_pinned_production_signing_configuration(self):
        root = Path(__file__).resolve().parents[1]
        batch = (root / "build-windows.bat").read_text("utf-8")
        self.assertIn("KSAT_SIGNING_PFX", batch)
        self.assertIn("KSAT_SIGNING_PUBLISHER", batch)
        self.assertIn("KSAT_SIGNING_TIMESTAMP_URL", batch)
        self.assertIn("--signing-pfx", batch)
        self.assertIn("--signing-publisher", batch)
        self.assertIn("--timestamp-url", batch)
        self.assertNotIn("--test-signing", batch)

    def test_inner_executables_are_verified_before_their_signed_bytes_are_published(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = ReleaseLayout(Path(directory))
            layout.dist_dir.mkdir(parents=True)
            layout.coordinator_executable.write_bytes(b"MZcoordinator")
            layout.client_executable.write_bytes(b"MZclient")
            layout.updater_executable.write_bytes(b"MZupdater")
            events = []

            def fake_sign(path, _config):
                events.append(path.name)
                path.write_bytes(path.read_bytes() + b"-signed")

            config = create_ephemeral_test_signing_config(
                layout.root / "test-identity",
                environ={"KSAT_RELEASE_TEST_SIGNING": "1"},
            )
            publish_signed_executables(layout, config, signer=fake_sign)
            self.assertEqual(["KSATCoordinator.exe", "KSATClient.exe", "KSATClientUpdater.exe"], events)
            self.assertEqual(
                layout.coordinator_executable.read_bytes(),
                layout.coordinator_release_executable.read_bytes(),
            )
            self.assertTrue(layout.client_release_executable.read_bytes().endswith(b"-signed"))

    def test_ephemeral_test_signing_requires_explicit_nonproduction_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SigningConfigurationError, "KSAT_RELEASE_TEST_SIGNING"):
                create_ephemeral_test_signing_config(Path(directory), environ={})

    @unittest.skipUnless(os.name == "nt", "Authenticode is a Windows release gate")
    def test_ephemeral_identity_signs_and_tamper_fails_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = create_ephemeral_test_signing_config(
                root, environ={"KSAT_RELEASE_TEST_SIGNING": "1"}
            )
            artifact = root / "probe.ps1"
            artifact.write_text("Write-Output 'signed probe'\n", encoding="utf-8")
            sign_and_verify_artifact(artifact, config)
            artifact.write_text("Write-Output 'tampered probe'\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "signature"):
                verify_authenticode_signature(artifact, config)

    def test_release_layout_has_two_distinct_versioned_products(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = ReleaseLayout(Path(directory))
            self.assertEqual("2.1.0", APP_VERSION)
            self.assertEqual("KSATCoordinator.exe", layout.coordinator_executable.name)
            self.assertEqual("KSATClient.exe", layout.client_executable.name)
            self.assertEqual("KSATClientUpdater.exe", layout.updater_executable.name)
            self.assertEqual(
                "KSATCoordinator-2.1.0.exe",
                layout.coordinator_release_executable.name,
            )
            self.assertEqual(
                "KSATClient-2.1.0.exe", layout.client_release_executable.name
            )
            self.assertEqual(
                "KSATCoordinatorSetup-2.1.0.exe", layout.coordinator_installer.name
            )
            self.assertEqual("KSATClientSetup-2.1.0.exe", layout.client_installer.name)
            self.assertEqual(
                "KSATClientUpdate-2.1.0-TEST-ONLY.ksat-client-update",
                layout.test_client_update.name,
            )
            self.assertEqual("SHA256SUMS.txt", layout.hash_manifest.name)
            self.assertEqual(
                "KSATClientUpdate-2.1.0.ksat-client-update",
                layout.client_update.name,
            )
            self.assertEqual(
                "KSATLabReleaseSigning.cer", layout.lab_trust_certificate.name
            )
            self.assertEqual(
                "SHA256SUMS-2.1.0-TEST-ONLY.txt", layout.test_hash_manifest.name
            )

    def test_test_update_bundle_uses_matching_ephemeral_update_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = ReleaseLayout(root)
            layout.release_dir.mkdir(parents=True)
            layout.client_installer.write_bytes(b"signed test installer")
            private = root / "update-private.key"
            private.write_bytes(b"k" * 32)

            output = create_test_client_update_bundle(
                layout,
                private,
                publisher="CN=KSAT TEST SIGNING IDENTITY - NOT FOR PRODUCTION",
                authenticode_verifier=lambda _path, _publisher: None,
            )

            self.assertEqual(layout.test_client_update, output)
            self.assertTrue(output.is_file())
            self.assertNotIn(b"k" * 32, output.read_bytes())

    def test_lab_update_bundle_uses_normal_release_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = ReleaseLayout(root)
            layout.release_dir.mkdir(parents=True)
            layout.client_installer.write_bytes(b"signed lab installer")
            private = root / "update-private.key"
            private.write_bytes(b"l" * 32)
            output = create_lab_client_update_bundle(
                layout,
                private,
                publisher="CN=KSAT LAB RELEASE SIGNING",
                authenticode_verifier=lambda _path, _publisher: None,
            )
            self.assertEqual(layout.client_update, output)
            self.assertTrue(output.is_file())

    def test_build_commands_use_distinct_entrypoints_workpaths_and_safe_assets(self):
        root = Path(__file__).resolve().parents[1]
        commands = build_commands(root, Path("C:/Python/python.exe"))
        self.assertEqual(3, len(commands))
        coordinator, client, updater = commands
        self.assertIn(str(root / "coordinator_main.py"), coordinator)
        hidden_imports = [
            coordinator[index + 1]
            for index, value in enumerate(coordinator[:-1])
            if value == "--hidden-import"
        ]
        self.assertIn("app", hidden_imports)
        self.assertIn(str(root / "client_app.py"), client)
        self.assertIn(str(root / "client_updater.py"), updater)
        self.assertIn("KSATCoordinator", coordinator)
        self.assertIn("KSATClient", client)
        self.assertIn("KSATClientUpdater", updater)
        self.assertNotEqual(
            coordinator[coordinator.index("--workpath") + 1],
            client[client.index("--workpath") + 1],
        )
        self.assertIn("--uac-admin", coordinator)
        self.assertNotEqual(
            coordinator[coordinator.index("--specpath") + 1],
            client[client.index("--specpath") + 1],
        )
        joined = "\n".join("\n".join(command) for command in commands).lower()
        for forbidden in (
            "aptitude.db",
            "client.sqlite3",
            "client-config.json",
            "device-key.bin",
            ".ksatpack",
            "protocol-signing.key",
            "pack-master.key",
            "enrollment.code",
        ):
            self.assertNotIn(forbidden, joined)

    def test_smoke_equivalence_manifest_rejects_payload_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "release.exe"
            second = root / "smoke.exe"
            first.write_bytes(b"MZrelease")
            second.write_bytes(b"MZsmoke")
            with self.assertRaises(ValueError):
                coordinator_payload_manifest(first, second)

    def test_smoke_seeds_production_directories_and_requires_healthy_client(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "windows_release.py").read_text("utf-8")
        self.assertIn('for name in ("identity", "state", "packs")', source)
        self.assertIn('client_state.get("state") != "device_setup"', source)
        self.assertIn('or client_state.get("problem") is not None', source)
        self.assertIn(
            '_add_data(layout.build_dir / "update-release-public.json", ".")',
            source,
        )

    def test_input_inspection_rejects_deployment_values_in_client_static(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static" / "client").mkdir(parents=True)
            (root / "static" / "client" / "app.js").write_text(
                "fetch('/api/state')", encoding="utf-8"
            )
            inspect_release_inputs(root)
            (root / "static" / "client" / "bad.js").write_text(
                "const server = 'https://ksat.example.edu:8443';", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "deployment-specific"):
                inspect_release_inputs(root)

    def test_hash_manifest_is_sorted_and_uses_exact_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            second = release / "KSATClientSetup-2.1.0.exe"
            first = release / "KSATCoordinatorSetup-2.1.0.exe"
            second.write_bytes(b"client-installer")
            first.write_bytes(b"coordinator-installer")
            manifest = write_sha256s([second, first], release / "SHA256SUMS.txt")
            expected = "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                for path in sorted((second, first), key=lambda item: item.name)
            )
            self.assertEqual(expected, manifest.read_text("ascii"))

    def test_inno_scripts_avoid_unsupported_pascal_helpers(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("KSATCoordinator.iss", "KSATClient.iss"):
            script = (root / "installer" / name).read_text("utf-8")
            self.assertNotIn("TryStrToInt", script)

    def test_client_installer_contains_protected_updater_and_rollback_seed(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "installer" / "KSATClient.iss").read_text("utf-8")
        self.assertIn("KSATClientUpdater.exe", script)
        self.assertIn("SeedLastKnownGood", script)
        self.assertIn("Get-AuthenticodeSignature", script)
        self.assertIn("KSAT Client\\updates", script)

    def test_installers_track_only_owned_firewall_and_root_ca_cleanup(self):
        root = Path(__file__).resolve().parents[1]
        coordinator = (root / "installer" / "KSATCoordinator.iss").read_text("utf-8")
        client = (root / "installer" / "KSATClient.iss").read_text("utf-8")
        self.assertIn("installer-owned-root-ca", client)
        self.assertIn("CurUninstallStepChanged", coordinator)
        self.assertIn("CurUninstallStepChanged", client)
        self.assertIn("ResultCode <> 0", coordinator)
        self.assertIn("ResultCode <> 0", client)
        self.assertIn("firewall-owner.json", coordinator)
        self.assertIn("Get-NetFirewallRule", coordinator)
        self.assertIn("Get-NetFirewallApplicationFilter", coordinator)
        self.assertIn("Get-NetFirewallPortFilter", coordinator)
        for attribute in (
            '"direction":"inbound"',
            '"action":"allow"',
            '"protocol":"tcp"',
            '"profile":"private"',
            '"enabled":true',
        ):
            self.assertIn(attribute, coordinator)
        self.assertGreaterEqual(coordinator.count("VerifyOwnedFirewall"), 5)
        self.assertIn("MoveFileEx", coordinator)
        self.assertIn("--validate-config", coordinator)
        self.assertIn("Thumbprint", client)
        self.assertGreaterEqual(client.count("^[0-9A-F]{40}$"), 2)
        self.assertGreaterEqual(client.count("PSObject.Properties.Name"), 2)
        self.assertGreaterEqual(client.count("-is [System.Array]"), 2)
        self.assertGreaterEqual(client.count("$raw-cne $canonical"), 2)
        self.assertNotIn("[Run]\nFilename: \"{sys}\\netsh.exe\"", coordinator)
        self.assertNotIn("if not ExistingConfiguration() then\n    begin\n      Parameters := '--install-config", client)

    def test_coordinator_installer_can_adopt_compatible_legacy_firewall_rule(self):
        root = Path(__file__).resolve().parents[1]
        coordinator = (root / "installer" / "KSATCoordinator.iss").read_text("utf-8")
        self.assertIn("ExistingFirewallRuleState(PortValue", coordinator)
        self.assertIn("FirewallRuleStateCompatible", coordinator)
        self.assertIn("if FirewallState = FirewallRuleStateCompatible then", coordinator)
        self.assertIn("FirewallRuleStateConflict", coordinator)

    def test_mutable_client_url_is_outside_lab_user_writable_state(self):
        root = Path(__file__).resolve().parents[1]
        client_source = (root / "client_app.py").read_text("utf-8")
        installer = (root / "installer" / "KSATClient.iss").read_text("utf-8")
        self.assertGreaterEqual(
            client_source.count('data_dir / "coordinator-url.json"'), 2
        )
        self.assertNotIn('data_dir / "state" / "coordinator-url.json"', client_source)
        self.assertGreaterEqual(
            client_source.count('ClientProcessLock(data_dir / "state")'), 2
        )
        self.assertIn(
            'Name: "{commonappdata}\\KSAT Client"; Permissions: admins-full system-full users-readexec',
            installer,
        )

    def test_client_installer_uses_protected_localsystem_service_authority(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "installer" / "KSATClient.iss").read_text("utf-8")
        self.assertIn("KSATLabClientAuthority", installer)
        self.assertIn("LocalSystem", installer)
        self.assertIn("sidtype", installer)
        self.assertIn("NT SERVICE\\KSATLabClientAuthority", installer)
        self.assertIn("--windows-service", installer)
        self.assertIn("--open-client", installer)
        self.assertNotIn("AccountPage", installer)
        self.assertNotIn("LABACCOUNT", installer)
        self.assertNotIn("AccountName + ':(OI)(CI)M'", installer)

    def test_client_installer_preflights_versioned_state_before_service_start(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "installer" / "KSATClient.iss").read_text("utf-8")
        self.assertIn("--migrate-state", installer)
        self.assertIn("CONFIRMLEGACYSTATEMIGRATION", installer)
        self.assertIn("--confirm-legacy-state", installer)
        migration = installer.index("--migrate-state")
        configure = installer.index("ConfigureClientService();", migration)
        start = installer.index("StartClientService();", configure)
        self.assertLess(migration, configure)
        self.assertLess(configure, start)

    def test_frozen_client_smoke_uses_guarded_service_console_mode(self):
        root = Path(__file__).resolve().parents[1]
        release_source = (root / "scripts" / "windows_release.py").read_text("utf-8")
        self.assertIn('"--service-console"', release_source)

    def test_all_active_release_metadata_uses_version_2(self):
        root = Path(__file__).resolve().parents[1]
        paths = (
            root / "app.py",
            root / "coordinator_main.py",
            root / "ksat" / "coordinator" / "tls.py",
            root / "scripts" / "windows_release.py",
            root / "installer" / "KSATCoordinator.iss",
            root / "installer" / "KSATClient.iss",
            root / "static" / "app.js",
            root / "static" / "index.html",
        )
        for path in paths:
            text = path.read_text("utf-8")
            self.assertIn("2.1.0", text, path.name)
            self.assertNotIn("1.3.3", text, path.name)


if __name__ == "__main__":
    unittest.main()
