"""Side-effect-free validation shared by installed clients and lab packaging.

The wire format is intentionally independent of the client product version.
"""
import base64
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from ksat.coordinator.tls import COORDINATOR_SIGNING_KEY_OID


def strict_json(data: bytes, limit: int):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field.")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("Non-finite JSON number.")

    if not isinstance(data, bytes) or len(data) > limit:
        raise ValueError("Public data exceeds the supported size.")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("Public JSON is invalid.") from error


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def normalize_coordinator_base_url(value: str) -> str:
    message = "Client configuration is invalid."
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(message)
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
        if (parsed.scheme != "https" or not hostname or parsed.username is not None
                or parsed.password is not None or "?" in value or "#" in value
                or parsed.path not in {"", "/"} or parsed.netloc.endswith(":")
                or port is not None and not 1 <= port <= 65535):
            raise ValueError(message)
        hostname.encode("ascii")
    except (ValueError, UnicodeError) as error:
        raise ValueError(message) from error
    host = hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host):
            raise ValueError(message)
        authority = host
    else:
        if "%" in host:
            raise ValueError(message)
        authority = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return "https://" + authority + (f":{port}" if port is not None else "")


def validate_ca(ca_bytes: bytes, signing_key_b64: str, *, now: datetime) -> None:
    """Validate the existing KSAT CA profile, signature, time and protocol-key binding."""
    message = "Client configuration is invalid."
    try:
        if not ca_bytes or len(ca_bytes) > 256 * 1024 or now.utcoffset() is None:
            raise ValueError(message)
        key = base64.b64decode(signing_key_b64.encode("ascii"), validate=True)
        cert = x509.load_pem_x509_certificate(ca_bytes)
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
        subject_key = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        linked = cert.extensions.get_extension_for_oid(COORDINATOR_SIGNING_KEY_OID)
        public = cert.public_key()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KSAT Coordinator Local CA")])
        expected = {x509.ExtensionOID.BASIC_CONSTRAINTS, x509.ExtensionOID.KEY_USAGE,
                    x509.ExtensionOID.SUBJECT_KEY_IDENTIFIER, COORDINATOR_SIGNING_KEY_OID}
        if (len(key) != 32 or cert.public_bytes(serialization.Encoding.PEM) != ca_bytes
                or not isinstance(public, rsa.RSAPublicKey) or public.key_size < 3072
                or cert.subject != subject or cert.issuer != subject or cert.serial_number <= 0
                or cert.signature_hash_algorithm.name != "sha256"
                or not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc
                or {e.oid for e in cert.extensions} != expected
                or not constraints.critical or not usage.critical or subject_key.critical or linked.critical
                or not constraints.value.ca or constraints.value.path_length != 0
                or not usage.value.digital_signature or usage.value.content_commitment
                or usage.value.key_encipherment or usage.value.data_encipherment or usage.value.key_agreement
                or not usage.value.key_cert_sign or not usage.value.crl_sign
                or subject_key.value.digest != x509.SubjectKeyIdentifier.from_public_key(public).digest
                or linked.value.value != key):
            raise ValueError(message)
        public.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
    except Exception as error:
        raise ValueError(message) from error


@dataclass(frozen=True)
class PublicTrustBundle:
    base_url: str
    ca_pem: bytes
    metadata_json: bytes
    ca_sha256: str
    signing_public_key_b64: str


def validate_public_bundle(base_url: str, ca_pem: bytes, metadata_json: bytes, *, now: datetime) -> PublicTrustBundle:
    message = "Coordinator public trust bundle is invalid."
    try:
        metadata = strict_json(metadata_json, 64 * 1024)
        expected = {"ca_sha256", "coordinator_url", "hostname", "port", "signing_public_key_b64",
                    "signing_public_key_sha256", "version"}
        url = normalize_coordinator_base_url(base_url)
        parsed = urlsplit(url)
        digest = hashlib.sha256(ca_pem).hexdigest()
        if (not isinstance(metadata, dict) or set(metadata) != expected
                or metadata["version"] != "2.1.0" or metadata["coordinator_url"] != url
                or metadata["ca_sha256"] != digest or type(metadata["port"]) is not int
                or metadata["port"] != (parsed.port or 443) or metadata["hostname"] != parsed.hostname):
            raise ValueError(message)
        key_b64 = metadata["signing_public_key_b64"]
        key = base64.b64decode(key_b64.encode("ascii"), validate=True)
        if len(key) != 32 or metadata["signing_public_key_sha256"] != hashlib.sha256(key).hexdigest():
            raise ValueError(message)
        validate_ca(ca_pem, key_b64, now=now)
        return PublicTrustBundle(url, ca_pem, canonical_json(metadata), digest, key_b64)
    except (ValueError, TypeError, AttributeError, UnicodeError) as error:
        raise ValueError(message) from error
