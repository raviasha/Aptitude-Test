import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.windows_release import (
    APP_VERSION,
    ReleaseLayout,
    build_commands,
    coordinator_payload_manifest,
    inspect_release_inputs,
    write_sha256s,
)


class WindowsPackagingTests(unittest.TestCase):
    def test_release_layout_has_two_distinct_versioned_products(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = ReleaseLayout(Path(directory))
            self.assertEqual("2.0.0", APP_VERSION)
            self.assertEqual("KSATCoordinator.exe", layout.coordinator_executable.name)
            self.assertEqual("KSATClient.exe", layout.client_executable.name)
            self.assertEqual(
                "KSATCoordinator-2.0.0.exe",
                layout.coordinator_release_executable.name,
            )
            self.assertEqual(
                "KSATClient-2.0.0.exe", layout.client_release_executable.name
            )
            self.assertEqual(
                "KSATCoordinatorSetup-2.0.0.exe", layout.coordinator_installer.name
            )
            self.assertEqual("KSATClientSetup-2.0.0.exe", layout.client_installer.name)
            self.assertEqual("SHA256SUMS.txt", layout.hash_manifest.name)

    def test_build_commands_use_distinct_entrypoints_workpaths_and_safe_assets(self):
        root = Path(__file__).resolve().parents[1]
        commands = build_commands(root, Path("C:/Python/python.exe"))
        self.assertEqual(2, len(commands))
        coordinator, client = commands
        self.assertIn(str(root / "coordinator_main.py"), coordinator)
        hidden_imports = [
            coordinator[index + 1]
            for index, value in enumerate(coordinator[:-1])
            if value == "--hidden-import"
        ]
        self.assertIn("app", hidden_imports)
        self.assertIn(str(root / "client_app.py"), client)
        self.assertIn("KSATCoordinator", coordinator)
        self.assertIn("KSATClient", client)
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
            second = release / "KSATClientSetup-2.0.0.exe"
            first = release / "KSATCoordinatorSetup-2.0.0.exe"
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
            self.assertIn("2.0.0", text, path.name)
            self.assertNotIn("1.3.3", text, path.name)


if __name__ == "__main__":
    unittest.main()
