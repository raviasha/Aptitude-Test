"""Canonical, hash-bound public lab configuration; never contains private keys."""
import base64
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from ksat.public_trust import PublicTrustBundle, canonical_json, strict_json, validate_public_bundle


@dataclass(frozen=True)
class LabProfile:
    format_version: int
    lab_name: str
    lab_slug: str
    trust: PublicTrustBundle
    sha256: str


def _body(name, slug, trust):
    return {"format_version": 1, "lab_name": name, "lab_slug": slug,
            "base_url": trust.base_url, "ca_pem_b64": base64.b64encode(trust.ca_pem).decode("ascii"),
            "metadata_b64": base64.b64encode(trust.metadata_json).decode("ascii")}


def make_lab_profile(lab_name: str, trust: PublicTrustBundle) -> LabProfile:
    if not isinstance(lab_name, str):
        raise ValueError("Enter a lab name.")
    name = lab_name.strip()
    if not 1 <= len(name) <= 80 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("Use a lab name of 1–80 printable characters.")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:64].rstrip("-")
    if not slug or re.fullmatch(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", slug):
        raise ValueError("Rename the lab using a non-reserved name with English letters or digits.")
    digest = hashlib.sha256(canonical_json(_body(name, slug, trust))).hexdigest()
    return LabProfile(1, name, slug, trust, digest)


def encode_profile(profile: LabProfile) -> bytes:
    value = _body(profile.lab_name, profile.lab_slug, profile.trust)
    value["sha256"] = profile.sha256
    return canonical_json(value)


def decode_profile(data: bytes, *, now: datetime) -> LabProfile:
    try:
        value = strict_json(data, 512 * 1024)
        expected = {"format_version", "lab_name", "lab_slug", "base_url", "ca_pem_b64", "metadata_b64", "sha256"}
        if (not isinstance(value, dict) or set(value) != expected
                or type(value["format_version"]) is not int or value["format_version"] != 1):
            raise ValueError
        trust = validate_public_bundle(value["base_url"], base64.b64decode(value["ca_pem_b64"], validate=True),
                                       base64.b64decode(value["metadata_b64"], validate=True), now=now)
        profile = make_lab_profile(value["lab_name"], trust)
        if (value["sha256"] != profile.sha256 or value["lab_slug"] != profile.lab_slug
                or value["lab_name"] != profile.lab_name):
            raise ValueError
        return profile
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("Lab profile is invalid or has changed.") from error
