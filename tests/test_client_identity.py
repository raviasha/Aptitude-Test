import base64
import ctypes
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ksat.client.identity import DeviceIdentityStore, WindowsDpapiProtector


class PrefixProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("Protected device key is invalid.")
        return value.removeprefix(b"protected:")


class OpaqueProtector:
    def protect(self, value: bytes) -> bytes:
        return bytes(item ^ 0xA5 for item in value)

    def unprotect(self, value: bytes) -> bytes:
        return bytes(item ^ 0xA5 for item in value)


class DeviceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_identity_is_generated_once_and_private_key_is_not_plaintext(self):
        store = DeviceIdentityStore(self.directory, PrefixProtector())
        first = store.load_or_create()
        second = store.load_or_create()
        self.assertEqual(first, second)
        raw_file = (self.directory / "device-key.bin").read_bytes()
        self.assertNotIn(base64.b64decode(first.private_key_b64), raw_file)

    def test_simultaneous_creation_publishes_one_identity(self):
        def create_identity(_):
            return DeviceIdentityStore(self.directory, PrefixProtector()).load_or_create()

        with ThreadPoolExecutor(max_workers=12) as executor:
            identities = list(executor.map(create_identity, range(36)))
        self.assertEqual(1, len({identity.private_key_b64 for identity in identities}))
        self.assertEqual(1, len({identity.public_key_b64 for identity in identities}))

    def test_simultaneous_processes_load_the_same_dpapi_identity(self):
        script = (
            "from pathlib import Path; "
            "from ksat.client.identity import DeviceIdentityStore; "
            f"print(DeviceIdentityStore(Path({str(self.directory)!r})).load_or_create().public_key_b64)"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        results = [process.communicate(timeout=30) for process in processes]
        self.assertEqual([0] * len(processes), [process.returncode for process in processes])
        self.assertEqual("", "".join(stderr for _stdout, stderr in results))
        self.assertEqual(1, len({stdout.strip() for stdout, _stderr in results}))
        self.assertEqual([], list(self.directory.glob(".device-key.bin.*.tmp")))

    def test_complete_crash_temporary_is_recovered_instead_of_regenerating(self):
        store = DeviceIdentityStore(self.directory, PrefixProtector())
        original = store.load_or_create()
        key_path = self.directory / "device-key.bin"
        crash_path = self.directory / ".device-key.bin.interrupted.tmp"
        key_path.replace(crash_path)
        recovered = DeviceIdentityStore(self.directory, PrefixProtector()).load_or_create()
        self.assertEqual(original, recovered)
        self.assertTrue(key_path.exists())
        self.assertFalse(crash_path.exists())

    def test_corrupt_protected_material_is_not_replaced(self):
        key_path = self.directory / "device-key.bin"
        key_path.write_bytes(b"not-protected")
        with self.assertRaisesRegex(ValueError, r"^Protected device identity is invalid\.$"):
            DeviceIdentityStore(self.directory, PrefixProtector()).load_or_create()
        self.assertEqual(b"not-protected", key_path.read_bytes())

    def test_invalid_base64_and_public_key_mismatch_are_rejected(self):
        for private_key, public_key in (("%%%", "%%%"), self._mismatched_pair()):
            with self.subTest(private_key=private_key[:8]):
                payload = json.dumps(
                    {
                        "coordinator_public_key_b64": None,
                        "device_id": None,
                        "format_version": 1,
                        "private_key_b64": private_key,
                        "public_key_b64": public_key,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                (self.directory / "device-key.bin").write_bytes(PrefixProtector().protect(payload))
                with self.assertRaisesRegex(ValueError, r"^Protected device identity is invalid\.$"):
                    DeviceIdentityStore(self.directory, PrefixProtector()).load_or_create()

    def test_enrollment_binding_is_protected_and_strictly_validated(self):
        store = DeviceIdentityStore(self.directory, OpaqueProtector())
        identity = store.load_or_create()
        device_id = str(uuid.uuid4())
        coordinator_public = self._public_key_b64()
        enrolled = store.save_enrollment(device_id, coordinator_public)
        self.assertEqual(device_id, enrolled.device_id)
        self.assertEqual(coordinator_public, enrolled.coordinator_public_key_b64)
        raw_file = (self.directory / "device-key.bin").read_bytes()
        self.assertNotIn(device_id.encode("ascii"), raw_file)
        self.assertNotIn(coordinator_public.encode("ascii"), raw_file)
        self.assertEqual(identity.private_key_b64, enrolled.private_key_b64)
        self.assertEqual(enrolled, DeviceIdentityStore(self.directory, OpaqueProtector()).load_or_create())

        with self.assertRaisesRegex(ValueError, r"^Device enrollment is invalid\.$"):
            store.save_enrollment("not-a-uuid", coordinator_public)
        with self.assertRaisesRegex(ValueError, r"^Device enrollment is invalid\.$"):
            store.save_enrollment(device_id, "not-base64")

    def test_conflicting_reenrollment_is_rejected(self):
        store = DeviceIdentityStore(self.directory, PrefixProtector())
        store.load_or_create()
        first_id = str(uuid.uuid4())
        first_key = self._public_key_b64()
        store.save_enrollment(first_id, first_key)
        with self.assertRaisesRegex(ValueError, r"^Device enrollment conflicts with the stored identity\.$"):
            store.save_enrollment(str(uuid.uuid4()), self._public_key_b64())
        self.assertEqual(first_id, store.load_or_create().device_id)

    def test_failed_atomic_enrollment_write_preserves_old_identity(self):
        store = DeviceIdentityStore(self.directory, PrefixProtector())
        original = store.load_or_create()
        with patch("ksat.client.identity.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, r"^Unable to save protected device identity\.$"):
                store.save_enrollment(str(uuid.uuid4()), self._public_key_b64())
        self.assertEqual(original, store.load_or_create())
        for temporary in self.directory.glob(".device-key.bin.*"):
            self.assertNotIn(original.private_key_b64.encode("ascii"), temporary.read_bytes())

    def test_default_protector_is_windows_dpapi_and_non_windows_requires_injection(self):
        store = DeviceIdentityStore(self.directory)
        self.assertIsInstance(store.protector, WindowsDpapiProtector)
        with patch("ksat.client.identity.os.name", "posix"):
            with self.assertRaisesRegex(RuntimeError, "Windows DPAPI or an injected SecretProtector"):
                DeviceIdentityStore(self.directory)

    def test_dpapi_uses_machine_scope_no_ui_and_releases_native_output(self):
        class NativeFunction:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        class FakeCrypt32:
            def __init__(self):
                self.buffers = []
                self.flags = []
                self.descriptions = []
                self.CryptProtectData = NativeFunction(self._protect)
                self.CryptUnprotectData = NativeFunction(self._unprotect)

            def _result(self, output_pointer, value):
                buffer = ctypes.create_string_buffer(value)
                self.buffers.append(buffer)
                output = output_pointer._obj
                output.cbData = len(value)
                output.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
                return 1

            def _protect(self, input_pointer, description, _entropy, _reserved, _prompt, flags, output):
                self.flags.append(flags)
                self.descriptions.append(description)
                value = ctypes.string_at(input_pointer._obj.pbData, input_pointer._obj.cbData)
                return self._result(output, b"opaque:" + value)

            def _unprotect(self, input_pointer, description, _entropy, _reserved, _prompt, flags, output):
                self.flags.append(flags)
                self.descriptions.append(description)
                value = ctypes.string_at(input_pointer._obj.pbData, input_pointer._obj.cbData)
                return self._result(output, value.removeprefix(b"opaque:"))

        class FakeKernel32:
            def __init__(self):
                self.freed = []
                self.LocalFree = NativeFunction(self._free)

            def _free(self, pointer):
                self.freed.append(pointer)
                return None

        crypt32 = FakeCrypt32()
        kernel32 = FakeKernel32()
        protector = WindowsDpapiProtector(crypt32=crypt32, kernel32=kernel32)
        protected = protector.protect(b"test-secret")
        self.assertEqual(b"test-secret", protector.unprotect(protected))
        self.assertEqual([5, 5], crypt32.flags)
        self.assertIsNone(crypt32.descriptions[0])
        self.assertIsNotNone(crypt32.descriptions[1])
        self.assertEqual(2, len(kernel32.freed))
        self.assertEqual(ctypes.c_int, crypt32.CryptProtectData.restype)

    def test_real_dpapi_round_trip_uses_non_key_test_material(self):
        protector = WindowsDpapiProtector()
        plaintext = b"ksat-dpapi-contract-test"
        protected = protector.protect(plaintext)
        self.assertNotEqual(plaintext, protected)
        self.assertEqual(plaintext, protector.unprotect(protected))

    @staticmethod
    def _public_key_b64() -> str:
        private = Ed25519PrivateKey.generate()
        return base64.b64encode(
            private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode("ascii")

    @classmethod
    def _mismatched_pair(cls) -> tuple[str, str]:
        first = Ed25519PrivateKey.generate()
        private = base64.b64encode(first.private_bytes_raw()).decode("ascii")
        return private, cls._public_key_b64()


if __name__ == "__main__":
    unittest.main()
