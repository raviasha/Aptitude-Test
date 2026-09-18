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


if __name__ == "__main__":
    unittest.main()
