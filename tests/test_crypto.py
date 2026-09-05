import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ksat.crypto import (
    decrypt_pack,
    encrypt_pack,
    generate_ed25519_keypair,
    load_or_create_coordinator_keyring,
    sha256_hex,
    sign_json,
    verify_json,
)


class CryptoTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.secrets_dir = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_signatures_and_encryption_detect_tampering(self):
        private_b64, public_b64 = generate_ed25519_keypair()
        payload = {"release_id": "r1", "questions": [1, 2]}
        signature = sign_json(private_b64, payload)
        verify_json(public_b64, payload, signature)
        with self.assertRaises(ValueError):
            verify_json(public_b64, {"release_id": "r2"}, signature)

        key = os.urandom(32)
        encrypted = encrypt_pack(key, "r1", b"question content")
        self.assertEqual(decrypt_pack(key, "r1", encrypted), b"question content")
        damaged = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
        with self.assertRaises(ValueError):
            decrypt_pack(key, "r1", damaged)
        self.assertEqual(len(sha256_hex(encrypted)), 64)

    def test_coordinator_keyring_is_stable_and_refuses_corruption(self):
        first = load_or_create_coordinator_keyring(self.secrets_dir)
        second = load_or_create_coordinator_keyring(self.secrets_dir)
        self.assertEqual(first, second)
        (self.secrets_dir / "protocol-signing.key").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            load_or_create_coordinator_keyring(self.secrets_dir)

    def test_crypto_helpers_reject_malformed_key_material(self):
        with self.assertRaises(ValueError):
            sign_json("not valid base64", {"release_id": "r1"})
        with self.assertRaises(ValueError):
            encrypt_pack(b"too short", "r1", b"content")
        with self.assertRaises(ValueError):
            decrypt_pack(b"too short", "r1", b"too short")

    def test_keyring_writes_only_public_signing_metadata(self):
        keyring = load_or_create_coordinator_keyring(self.secrets_dir)
        metadata = json.loads((self.secrets_dir / "protocol-public.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["signing_public_key_b64"], keyring.signing_public_key_b64)
        self.assertNotIn(keyring.signing_private_key_b64, metadata.values())
