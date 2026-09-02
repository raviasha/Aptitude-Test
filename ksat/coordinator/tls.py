"""Fail-closed coordinator TLS and public trust material."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

from ksat.crypto import CoordinatorKeyring, load_or_create_coordinator_keyring

if os.name == "nt":
    import msvcrt
else:
    import fcntl


APP_VERSION = "2.0.0"
COORDINATOR_SIGNING_KEY_OID = ObjectIdentifier("1.3.6.1.4.1.57264.1.1")
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)
_PRIVATE_KEY_NAMES = (
    "protocol-signing.key",
    "pack-master.key",
    "enrollment.code",
    "coordinator-ca.key.pem",
    "coordinator-server.key.pem",
    "browser-session.key",
    "client-session.key",
)
_CERTIFICATE_NAMES = ("coordinator-ca.crt.pem", "coordinator-server.crt.pem")
_thread_lock = threading.RLock()


@dataclass(frozen=True)
class CoordinatorSecurity:
    signing_private_key_b64: str
    signing_public_key_b64: str
    pack_master_key: bytes
    enrollment_code: str
    browser_session_secret: str
    client_session_secret: str
    ca_certificate_pem: bytes
    ca_private_key_pem: bytes
    server_certificate_pem: bytes
    server_private_key_pem: bytes
    ca_certificate_path: Path
    ca_private_key_path: Path
    server_certificate_path: Path
    server_private_key_path: Path
    public_export_dir: Path
    hostname: str
    port: int


def _utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Coordinator security time must include a UTC offset.")
    return result.astimezone(timezone.utc)


def _normalize_hostname(value: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("Coordinator hostname is invalid.")
    normalized = value.lower()
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("Coordinator hostname is invalid.") from error
    if not _DNS_NAME.fullmatch(normalized):
        raise ValueError("Coordinator hostname is invalid.")
    return normalized


def _normalize_port(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("Coordinator port is invalid.")
    return value


def current_private_lan_addresses() -> tuple[str, ...]:
    """Return stable, explicitly enumerated private LAN addresses for the host."""
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        records = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
    except OSError:
        records = []
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        except ValueError:
            continue
        if (
            address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_unspecified
            and not address.is_multicast
        ):
            addresses.add(address)
    return tuple(str(item) for item in sorted(addresses, key=lambda item: (item.version, int(item))))


def _normalize_lan_addresses(values: Iterable[str] | None) -> tuple[ipaddress._BaseAddress, ...]:
    source = current_private_lan_addresses() if values is None else values
    result: set[ipaddress._BaseAddress] = set()
    for value in source:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("Coordinator LAN address is invalid.") from error
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
        ):
            raise ValueError("Coordinator LAN address is invalid.")
        result.add(address)
    return tuple(sorted(result, key=lambda item: (item.version, int(item))))


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        with path.open("rb+") as stream:
            os.fsync(stream.fileno())
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _security_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".coordinator-security.lock"
    with _thread_lock:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                os.fsync(handle.fileno())
            deadline = time.monotonic() + 10.0
            while True:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise ValueError("Coordinator security setup is already running.") from error
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _load_rsa_private(path: Path, label: str) -> tuple[rsa.RSAPrivateKey, bytes]:
    try:
        raw = path.read_bytes()
        key = serialization.load_pem_private_key(raw, password=None)
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"Coordinator {label} private key is invalid.") from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise ValueError(f"Coordinator {label} private key is invalid.")
    if _private_pem(key) != raw:
        raise ValueError(f"Coordinator {label} private key is invalid.")
    return key, raw


def _load_certificate(path: Path, label: str) -> tuple[x509.Certificate, bytes]:
    try:
        raw = path.read_bytes()
        certificate = x509.load_pem_x509_certificate(raw)
    except (OSError, ValueError) as error:
        raise ValueError(f"Coordinator {label} certificate is invalid.") from error
    if certificate.public_bytes(serialization.Encoding.PEM) != raw:
        raise ValueError(f"Coordinator {label} certificate is invalid.")
    return certificate, raw


def _valid_at(certificate: x509.Certificate, now: datetime, label: str) -> None:
    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        raise ValueError(f"Coordinator {label} certificate is not currently valid.")


def _create_ca(
    signing_public_key_b64: str, now: datetime
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    signing_public = base64.b64decode(signing_public_key_b64, validate=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KSAT Coordinator Local CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.UnrecognizedExtension(COORDINATOR_SIGNING_KEY_OID, signing_public),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _create_server(
    ca_key: rsa.RSAPrivateKey,
    ca_certificate: x509.Certificate,
    hostname: str,
    addresses: tuple[ipaddress._BaseAddress, ...],
    now: datetime,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    san_values: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    san_values.extend(x509.IPAddress(address) for address in addresses)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(min(now + timedelta(days=825), ca_certificate.not_valid_after_utc))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_values), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, certificate


def _validate_ca(
    key: rsa.RSAPrivateKey,
    certificate: x509.Certificate,
    signing_public_key_b64: str,
    now: datetime,
) -> None:
    _valid_at(certificate, now, "CA")
    expected_subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "KSAT Coordinator Local CA")]
    )
    if (
        certificate.subject != expected_subject
        or certificate.issuer != expected_subject
        or certificate.serial_number <= 0
        or not isinstance(certificate.public_key(), rsa.RSAPublicKey)
        or certificate.public_key().key_size < 3072
    ):
        raise ValueError("Coordinator CA certificate is invalid.")
    if certificate.signature_hash_algorithm.name != "sha256":
        raise ValueError("Coordinator CA certificate is invalid.")
    try:
        certificate.public_key().verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            padding.PKCS1v15(),
            certificate.signature_hash_algorithm,
        )
    except Exception as error:
        raise ValueError("Coordinator CA certificate is invalid.") from error
    if key.public_key().public_numbers() != certificate.public_key().public_numbers():
        raise ValueError("Coordinator CA private key does not match its certificate.")
    try:
        constraints_extension = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage_extension = certificate.extensions.get_extension_for_class(x509.KeyUsage)
        subject_key_extension = certificate.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        linked_extension = certificate.extensions.get_extension_for_oid(COORDINATOR_SIGNING_KEY_OID)
    except x509.ExtensionNotFound as error:
        raise ValueError("Coordinator CA certificate is invalid.") from error
    constraints = constraints_extension.value
    usages = usage_extension.value
    expected_oids = {
        x509.ExtensionOID.BASIC_CONSTRAINTS,
        x509.ExtensionOID.KEY_USAGE,
        x509.ExtensionOID.SUBJECT_KEY_IDENTIFIER,
        COORDINATOR_SIGNING_KEY_OID,
    }
    if (
        {extension.oid for extension in certificate.extensions} != expected_oids
        or not constraints_extension.critical
        or not usage_extension.critical
        or subject_key_extension.critical
        or linked_extension.critical
        or not constraints.ca
        or constraints.path_length != 0
        or not usages.digital_signature
        or usages.content_commitment
        or usages.key_encipherment
        or usages.data_encipherment
        or usages.key_agreement
        or not usages.key_cert_sign
        or not usages.crl_sign
        or subject_key_extension.value.digest
        != x509.SubjectKeyIdentifier.from_public_key(certificate.public_key()).digest
    ):
        raise ValueError("Coordinator CA certificate is invalid.")
    if linked_extension.value.value != base64.b64decode(signing_public_key_b64, validate=True):
        raise ValueError("Coordinator CA trust does not match the protocol signing key.")


def _validate_server(
    key: rsa.RSAPrivateKey,
    certificate: x509.Certificate,
    ca_certificate: x509.Certificate,
    hostname: str,
    addresses: tuple[ipaddress._BaseAddress, ...],
    now: datetime,
) -> None:
    _valid_at(certificate, now, "server")
    if certificate.issuer != ca_certificate.subject or certificate.serial_number <= 0:
        raise ValueError("Coordinator server certificate is invalid.")
    if certificate.signature_hash_algorithm.name != "sha256":
        raise ValueError("Coordinator server certificate is invalid.")
    try:
        ca_certificate.public_key().verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            padding.PKCS1v15(),
            certificate.signature_hash_algorithm,
        )
        constraints_extension = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage_extension = certificate.extensions.get_extension_for_class(x509.KeyUsage)
        extended_extension = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        names_extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        subject_key_extension = certificate.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        authority_key_extension = certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    except (ValueError, x509.ExtensionNotFound) as error:
        raise ValueError("Coordinator server certificate is invalid.") from error
    if key.public_key().public_numbers() != certificate.public_key().public_numbers():
        raise ValueError("Coordinator server private key does not match its certificate.")
    constraints = constraints_extension.value
    usages = usage_extension.value
    extended = extended_extension.value
    names = names_extension.value
    expected_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    expected_oids = {
        x509.ExtensionOID.BASIC_CONSTRAINTS,
        x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
        x509.ExtensionOID.EXTENDED_KEY_USAGE,
        x509.ExtensionOID.KEY_USAGE,
        x509.ExtensionOID.SUBJECT_KEY_IDENTIFIER,
        x509.ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
    }
    if (
        certificate.subject != expected_subject
        or {extension.oid for extension in certificate.extensions} != expected_oids
        or not constraints_extension.critical
        or names_extension.critical
        or not extended_extension.critical
        or not usage_extension.critical
        or subject_key_extension.critical
        or authority_key_extension.critical
        or constraints.ca
        or constraints.path_length is not None
        or list(extended) != [ExtendedKeyUsageOID.SERVER_AUTH]
    ):
        raise ValueError("Coordinator server certificate is invalid.")
    if (
        not usages.digital_signature
        or usages.content_commitment
        or not usages.key_encipherment
        or usages.data_encipherment
        or usages.key_agreement
        or usages.key_cert_sign
        or usages.crl_sign
        or subject_key_extension.value.digest
        != x509.SubjectKeyIdentifier.from_public_key(certificate.public_key()).digest
        or authority_key_extension.value.key_identifier
        != x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_certificate.public_key()).key_identifier
    ):
        raise ValueError("Coordinator server certificate is invalid.")
    if names.get_values_for_type(x509.DNSName) != [hostname]:
        raise ValueError("Coordinator server certificate does not match its hostname.")
    expected_addresses = {*addresses, ipaddress.ip_address("127.0.0.1")}
    if set(names.get_values_for_type(x509.IPAddress)) != expected_addresses:
        raise ValueError("Coordinator server certificate does not match current LAN addresses.")


def _validate_browser_secret(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("Coordinator browser session secret is invalid.") from error
    if len(raw) != 32:
        raise ValueError("Coordinator browser session secret is invalid.")
    return raw, base64.urlsafe_b64encode(raw).decode("ascii")


def _validate_client_session_secret(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("Coordinator client session secret is invalid.") from error
    if len(raw) != 32:
        raise ValueError("Coordinator client session secret is invalid.")
    return raw, base64.urlsafe_b64encode(raw).decode("ascii")


def _all_required_exist(secrets_dir: Path) -> bool:
    paths = [secrets_dir / name for name in (*_PRIVATE_KEY_NAMES, *_CERTIFICATE_NAMES)]
    exists = [path.is_file() for path in paths]
    directory_entries = [os.path.lexists(path) for path in paths]
    if any(directory_entries) and not all(exists):
        raise ValueError("Coordinator private security material is incomplete.")
    return all(exists)


def _publish_public_export(
    public_dir: Path,
    ca_pem: bytes,
    hostname: str,
    port: int,
    signing_public_key_b64: str,
) -> None:
    public_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(public_dir / "coordinator-ca.pem", ca_pem)
    signing_raw = base64.b64decode(signing_public_key_b64, validate=True)
    metadata = {
        "version": APP_VERSION,
        "hostname": hostname,
        "port": port,
        "coordinator_url": f"https://{hostname}:{port}",
        "ca_sha256": hashlib.sha256(ca_pem).hexdigest(),
        "signing_public_key_b64": signing_public_key_b64,
        "signing_public_key_sha256": hashlib.sha256(signing_raw).hexdigest(),
    }
    _atomic_write(
        public_dir / "coordinator-public.json",
        json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        ),
    )
    allowed = {"coordinator-ca.pem", "coordinator-public.json"}
    unexpected = [path for path in public_dir.iterdir() if path.name not in allowed]
    if unexpected:
        raise ValueError("Coordinator public export directory contains unexpected files.")


def _build_result(
    data_dir: Path,
    keyring: CoordinatorKeyring,
    browser_secret: str,
    client_session_secret: str,
    ca_key: rsa.RSAPrivateKey,
    ca_key_pem: bytes,
    ca_certificate: x509.Certificate,
    ca_pem: bytes,
    server_key: rsa.RSAPrivateKey,
    server_key_pem: bytes,
    server_certificate: x509.Certificate,
    server_pem: bytes,
    hostname: str,
    port: int,
) -> CoordinatorSecurity:
    del ca_key, ca_certificate, server_key, server_certificate
    secrets_dir = data_dir / "secrets"
    public_dir = data_dir / "public"
    return CoordinatorSecurity(
        signing_private_key_b64=keyring.signing_private_key_b64,
        signing_public_key_b64=keyring.signing_public_key_b64,
        pack_master_key=keyring.pack_master_key,
        enrollment_code=keyring.enrollment_code,
        browser_session_secret=browser_secret,
        client_session_secret=client_session_secret,
        ca_certificate_pem=ca_pem,
        ca_private_key_pem=ca_key_pem,
        server_certificate_pem=server_pem,
        server_private_key_pem=server_key_pem,
        ca_certificate_path=public_dir / "coordinator-ca.pem",
        ca_private_key_path=secrets_dir / "coordinator-ca.key.pem",
        server_certificate_path=secrets_dir / "coordinator-server.crt.pem",
        server_private_key_path=secrets_dir / "coordinator-server.key.pem",
        public_export_dir=public_dir,
        hostname=hostname,
        port=port,
    )


def load_or_create_coordinator_security(
    data_dir: Path,
    *,
    hostname: str,
    port: int = 8443,
    lan_ip_addresses: Iterable[str] | None = None,
    now: datetime | None = None,
) -> CoordinatorSecurity:
    data_dir = Path(data_dir).resolve()
    hostname = _normalize_hostname(hostname)
    port = _normalize_port(port)
    addresses = _normalize_lan_addresses(lan_ip_addresses)
    checked_now = _utc(now)
    with _security_lock(data_dir):
        secrets_dir = data_dir / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        complete = _all_required_exist(secrets_dir)
        if not complete:
            if any(secrets_dir.iterdir()):
                raise ValueError("Coordinator private security material is incomplete.")
            keyring = load_or_create_coordinator_keyring(secrets_dir)
            ca_key, ca_certificate = _create_ca(
                keyring.signing_public_key_b64, checked_now
            )
            server_key, server_certificate = _create_server(
                ca_key, ca_certificate, hostname, addresses, checked_now
            )
            browser_raw = os.urandom(32)
            client_session_raw = os.urandom(32)
            _atomic_write(secrets_dir / "coordinator-ca.key.pem", _private_pem(ca_key))
            _atomic_write(
                secrets_dir / "coordinator-ca.crt.pem",
                ca_certificate.public_bytes(serialization.Encoding.PEM),
            )
            _atomic_write(
                secrets_dir / "coordinator-server.key.pem", _private_pem(server_key)
            )
            _atomic_write(
                secrets_dir / "coordinator-server.crt.pem",
                server_certificate.public_bytes(serialization.Encoding.PEM),
            )
            _atomic_write(secrets_dir / "browser-session.key", browser_raw)
            _atomic_write(secrets_dir / "client-session.key", client_session_raw)
        else:
            keyring = load_or_create_coordinator_keyring(secrets_dir)

        ca_key, ca_key_pem = _load_rsa_private(
            secrets_dir / "coordinator-ca.key.pem", "CA"
        )
        ca_certificate, ca_pem = _load_certificate(
            secrets_dir / "coordinator-ca.crt.pem", "CA"
        )
        server_key, server_key_pem = _load_rsa_private(
            secrets_dir / "coordinator-server.key.pem", "server"
        )
        server_certificate, server_pem = _load_certificate(
            secrets_dir / "coordinator-server.crt.pem", "server"
        )
        _raw_browser, browser_secret = _validate_browser_secret(
            secrets_dir / "browser-session.key"
        )
        _raw_client, client_session_secret = _validate_client_session_secret(
            secrets_dir / "client-session.key"
        )
        _validate_ca(ca_key, ca_certificate, keyring.signing_public_key_b64, checked_now)
        _validate_server(
            server_key,
            server_certificate,
            ca_certificate,
            hostname,
            addresses,
            checked_now,
        )
        _publish_public_export(
            data_dir / "public",
            ca_pem,
            hostname,
            port,
            keyring.signing_public_key_b64,
        )
        return _build_result(
            data_dir,
            keyring,
            browser_secret,
            client_session_secret,
            ca_key,
            ca_key_pem,
            ca_certificate,
            ca_pem,
            server_key,
            server_key_pem,
            server_certificate,
            server_pem,
            hostname,
            port,
        )


def renew_coordinator_server_certificate(
    data_dir: Path,
    *,
    hostname: str,
    port: int = 8443,
    lan_ip_addresses: Iterable[str] | None = None,
    now: datetime | None = None,
    confirmed: bool = False,
) -> CoordinatorSecurity:
    """Explicitly replace only the server key/certificate; preserve client CA trust."""
    if confirmed is not True:
        raise ValueError("Explicit confirmation is required to renew coordinator TLS.")
    data_dir = Path(data_dir).resolve()
    hostname = _normalize_hostname(hostname)
    port = _normalize_port(port)
    addresses = _normalize_lan_addresses(lan_ip_addresses)
    checked_now = _utc(now)
    with _security_lock(data_dir):
        secrets_dir = data_dir / "secrets"
        if not _all_required_exist(secrets_dir):
            raise ValueError("Coordinator security must exist before renewal.")
        keyring = load_or_create_coordinator_keyring(secrets_dir)
        ca_key, ca_key_pem = _load_rsa_private(
            secrets_dir / "coordinator-ca.key.pem", "CA"
        )
        ca_certificate, ca_pem = _load_certificate(
            secrets_dir / "coordinator-ca.crt.pem", "CA"
        )
        _validate_ca(ca_key, ca_certificate, keyring.signing_public_key_b64, checked_now)
        _raw_browser, browser_secret = _validate_browser_secret(
            secrets_dir / "browser-session.key"
        )
        _raw_client, client_session_secret = _validate_client_session_secret(
            secrets_dir / "client-session.key"
        )
        server_key, server_certificate = _create_server(
            ca_key, ca_certificate, hostname, addresses, checked_now
        )
        server_key_pem = _private_pem(server_key)
        server_pem = server_certificate.public_bytes(serialization.Encoding.PEM)
        _atomic_write(secrets_dir / "coordinator-server.key.pem", server_key_pem)
        _atomic_write(secrets_dir / "coordinator-server.crt.pem", server_pem)
        _validate_server(
            server_key,
            server_certificate,
            ca_certificate,
            hostname,
            addresses,
            checked_now,
        )
        _publish_public_export(
            data_dir / "public",
            ca_pem,
            hostname,
            port,
            keyring.signing_public_key_b64,
        )
        return _build_result(
            data_dir,
            keyring,
            browser_secret,
            client_session_secret,
            ca_key,
            ca_key_pem,
            ca_certificate,
            ca_pem,
            server_key,
            server_key_pem,
            server_certificate,
            server_pem,
            hostname,
            port,
        )
