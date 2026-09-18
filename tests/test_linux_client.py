import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "Linux root build tests")
class LinuxClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = self.root / "wrapping.key"
        self.key.write_bytes(os.urandom(32))
        self.key.chmod(0o640)

    def tearDown(self):
        self.temp.cleanup()

    def test_encrypted_identity_survives_restart_and_rejects_tampering(self):
        from ksat.client.linux_identity import LinuxKeyProtector
        from ksat.client.identity import DeviceIdentityStore
        store = DeviceIdentityStore(self.root / "identity", LinuxKeyProtector(self.key))
        identity = store.load_or_create()
        reopened = DeviceIdentityStore(self.root / "identity", LinuxKeyProtector(self.key))
        self.assertEqual(identity, reopened.load_or_create())
        raw = store.key_path.read_bytes()
        self.assertNotIn(identity.private_key_b64.encode(), raw)
        store.key_path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaises(ValueError):
            reopened.load_or_create()

    def test_world_readable_key_and_symlink_are_rejected(self):
        from ksat.client.linux_identity import LinuxKeyProtector
        self.key.chmod(0o644)
        with self.assertRaises(PermissionError):
            LinuxKeyProtector(self.key)
        self.key.chmod(0o640)
        link = self.root / "link"
        link.symlink_to(self.key)
        with self.assertRaises(OSError):
            LinuxKeyProtector(link)

    def test_wrong_key_cannot_open_identity(self):
        from ksat.client.linux_identity import LinuxKeyProtector
        encrypted = LinuxKeyProtector(self.key).protect(b"secret")
        self.key.write_bytes(os.urandom(32))
        with self.assertRaises(Exception):
            LinuxKeyProtector(self.key).unprotect(encrypted)

    def test_setup_reuses_identity_and_config_and_missing_key_fails(self):
        import linux_client_main as main
        import pwd
        from ksat.coordinator.tls import load_or_create_coordinator_security
        from ksat.client.identity import DeviceIdentityStore
        from ksat.client.linux_identity import LinuxKeyProtector
        tls = load_or_create_coordinator_security(self.root / "server", hostname="ksat.example.edu", port=8443, lan_ip_addresses=["10.0.0.8"])
        program_data = self.root / "data"
        client_dir = program_data / "KSAT Client"
        key_path = self.root / "etc/key"
        account = pwd.getpwnam("root")
        with patch.object(main, "PROGRAM_DATA", program_data), patch.object(main, "DATA_DIR", client_dir), patch.object(main, "KEY_PATH", key_path), patch.object(main.pwd, "getpwnam", return_value=account), patch.object(main.subprocess, "run") as run:
            kwargs = dict(base_url="https://ksat.example.edu:8443", ca=tls.public_export_dir / "coordinator-ca.pem", metadata=tls.public_export_dir / "coordinator-public.json")
            main.configure(**kwargs)
            store = DeviceIdentityStore(client_dir / "identity", LinuxKeyProtector(key_path))
            identity = store.load_or_create()
            config = (client_dir / "client-config.json").read_bytes()
            key = key_path.read_bytes()
            main.configure(**kwargs)
            self.assertEqual(key, key_path.read_bytes())
            self.assertEqual(config, (client_dir / "client-config.json").read_bytes())
            self.assertEqual(identity, store.load_or_create())
            self.assertEqual(["systemctl", "stop", main.SERVICE], run.call_args_list[0].args[0])
            key_path.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                main.configure(**kwargs)

    def test_invalid_bundle_does_not_stop_service_or_create_private_state(self):
        import linux_client_main as main
        data = self.root / "data"
        with patch.object(main, "PROGRAM_DATA", data), patch.object(main, "DATA_DIR", data / "KSAT Client"), patch.object(main.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                main.configure("http://not-https", self.root / "missing.pem", self.root / "missing.json")
            self.assertFalse(data.exists())
            run.assert_not_called()  # The external service must not be stopped on validation failure.

    def test_restart_existing_client_requires_configuration_and_preserves_files(self):
        import linux_client_main as main
        self.assertTrue(hasattr(main, "start_configured_service"), "GUI recovery needs a narrow privileged service-start operation")
        from ksat.coordinator.tls import load_or_create_coordinator_security
        from client_app import install_client_configuration
        tls = load_or_create_coordinator_security(self.root / "server", hostname="ksat.example.edu", port=8443, lan_ip_addresses=["10.0.0.8"])
        data = self.root / "data"
        client_dir = data / "KSAT Client"
        with patch.object(main, "DATA_DIR", client_dir), patch.object(main, "KEY_PATH", self.key), patch.object(main.subprocess, "run") as run:
            with self.assertRaises((OSError, ValueError)):
                main.start_configured_service()
            run.assert_not_called()
            install_client_configuration(data, base_url="https://ksat.example.edu:8443", ca_source=tls.public_export_dir / "coordinator-ca.pem", metadata_source=tls.public_export_dir / "coordinator-public.json")
            before = {p: p.read_bytes() for p in client_dir.rglob("*") if p.is_file()}
            main.start_configured_service()
            self.assertEqual(before, {p: p.read_bytes() for p in client_dir.rglob("*") if p.is_file()})
            run.assert_called_once_with(["systemctl", "enable", "--now", main.SERVICE], check=True)

    def test_retry_completes_permissions_after_interrupted_initial_setup(self):
        import linux_client_main as main
        import pwd
        import stat
        from client_app import install_client_configuration
        from ksat.coordinator.tls import load_or_create_coordinator_security
        tls = load_or_create_coordinator_security(self.root / "server", hostname="ksat.example.edu", port=8443, lan_ip_addresses=["10.0.0.8"])
        data = self.root / "data"
        client_dir = data / "KSAT Client"
        account = pwd.getpwnam("ksat-client")
        kwargs = dict(base_url="https://ksat.example.edu:8443", ca=tls.public_export_dir / "coordinator-ca.pem", metadata=tls.public_export_dir / "coordinator-public.json")
        with patch.object(main, "PROGRAM_DATA", data), patch.object(main, "DATA_DIR", client_dir), patch.object(main, "KEY_PATH", self.key), patch.object(main.subprocess, "run"):
            main.configure(**kwargs)
            config = client_dir / "client-config.json"
            ca = client_dir / "trust/coordinator-ca.pem"
            lock = client_dir / "state/.client.lock"
            contents = {p: p.read_bytes() for p in (config, ca, self.key)}
            for retry in (lambda: main.configure(**kwargs), main.start_configured_service):
                # Simulate interruption after config publication, before chmod/chown.
                config.chmod(0o600)
                ca.chmod(0o600)
                os.chown(lock, 0, 0)
                retry()
                self.assertEqual(0o640, stat.S_IMODE(config.stat().st_mode))
                self.assertEqual(0o640, stat.S_IMODE(ca.stat().st_mode))
                self.assertEqual(account.pw_gid, config.stat().st_gid)
                self.assertEqual(account.pw_uid, lock.stat().st_uid)
                self.assertEqual(contents, {p: p.read_bytes() for p in contents})


if __name__ == "__main__":
    unittest.main()
