"""Create a persistent private-lab release identity outside the repository."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


LAB_SIGNING_PUBLISHER = "CN=KSAT LAB RELEASE SIGNING"


def _protect_directory(path: Path) -> None:
    if os.name != "nt":
        return
    user_domain = os.environ.get("USERDOMAIN", "").strip()
    user_name = os.environ.get("USERNAME", "").strip()
    current_user = f"{user_domain}\\{user_name}" if user_domain else user_name
    if not current_user:
        raise OSError("Could not identify the lab-signing build user.")
    completed = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
            f"{current_user}:(OI)(CI)F",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError("Could not protect the lab-signing directory.")


def create_lab_signing_identity(directory: Path, *, years: int = 5) -> dict[str, str]:
    if years < 2 or years > 10:
        raise ValueError("Lab signing validity must be between two and ten years.")
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise FileExistsError("Lab-signing directory must be empty.")
    now = datetime.now(timezone.utc)
    code_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "KSAT LAB RELEASE SIGNING")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(code_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365 * years))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False
        )
        .sign(code_key, hashes.SHA256())
    )
    password = secrets.token_urlsafe(48)
    (root / "KSATLabReleaseSigning.pfx").write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"KSAT Lab Release Signing",
            code_key,
            certificate,
            None,
            serialization.BestAvailableEncryption(password.encode("ascii")),
        )
    )
    (root / "KSATLabReleaseSigning.cer").write_bytes(
        certificate.public_bytes(serialization.Encoding.DER)
    )
    (root / "pfx-password.txt").write_text(password + "\n", encoding="ascii")

    update_private = ed25519.Ed25519PrivateKey.generate()
    private_raw = update_private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = update_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    (root / "update-signing-private.key").write_bytes(private_raw)
    (root / "update-signing-public.key").write_bytes(public_raw)

    result = {
        "publisher": certificate.subject.rfc4514_string(),
        "thumbprint": certificate.fingerprint(hashes.SHA1()).hex().upper(),
        "not_before": certificate.not_valid_before_utc.isoformat(),
        "not_after": certificate.not_valid_after_utc.isoformat(),
        "certificate_sha256": hashlib.sha256(
            certificate.public_bytes(serialization.Encoding.DER)
        ).hexdigest(),
        "update_public_key_b64": base64.b64encode(public_raw).decode("ascii"),
    }
    (root / "lab-signing.json").write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    _protect_directory(root)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args()
    result = create_lab_signing_identity(args.directory, years=args.years)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
