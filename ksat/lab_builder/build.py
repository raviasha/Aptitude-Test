"""Compile, sign, inspect and publish a lab installer without overwriting output."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from datetime import datetime, timezone

from ksat.client.install_guard import reject_links
from ksat.lab_builder.payload import ApprovedPayload, verify_payload
from ksat.lab_builder.profile import LabProfile, decode_profile, encode_profile
from ksat.lab_builder.signing import SignerSelection, sign_from_store
from ksat.public_trust import canonical_json
from ksat.windows_authenticode import verify_authenticode

INSTALLER_FILES = frozenset({"KSATClient.exe", "KSATClientUpdater.exe", "KSATClientInstallGuard.exe",
    "publisher.cer", "lab-profile.json", "coordinator-ca.pem", "coordinator-public.json", "lab-summary.ini"})


@dataclass(frozen=True)
class BuildTools:
    iscc: Path
    signtool: Path
    innoextract: Path


@dataclass(frozen=True)
class BuildRequest:
    profile: LabProfile
    payload: ApprovedPayload
    signer: SignerSelection
    tools: BuildTools
    output_dir: Path


@dataclass(frozen=True)
class BuildEvent:
    stage: str
    message: str


@dataclass(frozen=True)
class BuildResult:
    installer: Path
    receipt: Path
    sha256: str


def run_tool(args: list[str], *, timeout=300) -> str:
    result = subprocess.run([str(a) for a in args], shell=False, capture_output=True,
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        # Tool output can contain input paths; never surface the raw dump.
        raise ValueError(f"{Path(args[0]).name} failed. Check the selected build tool and public input files.")
    return result.stdout.decode("utf-8", errors="replace")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish_no_replace(source: Path, destination: Path):
    # Staging and output share a volume. Linking is atomic and fails on collision.
    os.link(source, destination)


def public_resources(profile: LabProfile) -> dict[str, bytes]:
    profile = decode_profile(encode_profile(profile), now=datetime.now(timezone.utc))
    return {"lab-profile.json": encode_profile(profile), "coordinator-ca.pem": profile.trust.ca_pem,
        "coordinator-public.json": profile.trust.metadata_json,
        "lab-summary.ini": (f"[Lab]\nName={profile.lab_name}\nURL={profile.trust.base_url}\n").encode("utf-16")}


def inspect_installer(installer: Path, extraction: Path, request: BuildRequest,
                      expected: dict[str, str]):
    identity = verify_authenticode(installer, request.payload.publisher)
    if identity.thumbprint != request.payload.thumbprint:
        raise ValueError("Installer publisher pin does not match.")
    run_tool([str(request.tools.innoextract), "--extract", "--silent", "--output-dir", str(extraction), str(installer)])
    seen = set()
    for path in extraction.rglob("*"):
        reject_links(path)
        if path.is_dir():
            continue
        if path.name not in expected or path.name in seen or digest(path) != expected[path.name]:
            raise ValueError("Extracted installer contains unexpected or changed resources.")
        seen.add(path.name)
    if seen != set(expected):
        raise ValueError("Installer is missing an approved resource.")
    # Exact executable hashes bind back to verify_payload's recursive archive and
    # Authenticode checks. No alternate/nested executable is accepted here.


def build_lab_installer(request: BuildRequest, *, cancel: threading.Event,
                        progress: Callable[[BuildEvent], None]) -> BuildResult:
    def check_cancel():
        if cancel.is_set():
            raise InterruptedError("Build cancelled; no installer published.")
    progress(BuildEvent("validation", "Checking public profile and approved payload"))
    check_cancel()
    resources = public_resources(request.profile)
    verify_payload(request.payload)
    if (request.signer.publisher, request.signer.thumbprint) != (request.payload.publisher, request.payload.thumbprint):
        raise ValueError("Signing identity does not match the approved payload.")
    output = Path(request.output_dir).absolute()
    reject_links(output)
    if not output.is_dir():
        raise ValueError("Choose an existing output folder.")
    name = f"KSATClientSetup-{request.profile.lab_slug}-{request.payload.client_version}"
    installer, receipt = output / (name + ".exe"), output / (name + ".json")
    reservation = output / (name + ".lock")
    if installer.exists() or receipt.exists():
        raise FileExistsError("An installer or receipt with this name already exists. Choose another folder.")
    with reservation.open("xb"):
        pass
    published_receipt = False
    try:
        with tempfile.TemporaryDirectory(prefix=".ksat-build-", dir=output) as temporary:
            work = Path(temporary)
            payload = work / "payload"
            payload.mkdir()
            for filename, expected in request.payload.files.items():
                shutil.copyfile(request.payload.root / filename, payload / filename)
                if digest(payload / filename) != expected:
                    raise ValueError("Approved payload changed during copying.")
            for filename, data in resources.items():
                (payload / filename).write_bytes(data)
            expected = {name: digest(payload / name) for name in INSTALLER_FILES}
            compiled = work / "compiled"
            compiled.mkdir()
            check_cancel()
            progress(BuildEvent("compilation", "Compiling preconfigured client installer"))
            compiler_output = run_tool([str(request.tools.iscc), "/DKSAT_LAB_MODE=1",
                f"/DKSAT_PAYLOAD_DIR={payload}", f"/DKSAT_OUTPUT_DIR={compiled}",
                f"/DKSAT_CLIENT_VERSION={request.payload.client_version}", str(payload / "KSATClient.iss")])
            if "Compiler engine version: Inno Setup 6.7.3" not in compiler_output:
                raise ValueError("This builder requires Inno Setup 6.7.3.")
            check_cancel()
            for filename, wanted in expected.items():
                if digest(payload / filename) != wanted:
                    raise ValueError("Installer inputs changed during compilation.")
            artifact = compiled / f"KSATClientSetup-{request.payload.client_version}.exe"
            progress(BuildEvent("signing", "Signing with the approved Windows-store certificate"))
            sign_from_store(artifact, request.signer, request.tools.signtool)
            check_cancel()
            progress(BuildEvent("verification", "Verifying signature and every embedded resource"))
            before = digest(artifact)
            inspect_installer(artifact, work / "extracted", request, expected)
            if digest(artifact) != before:
                raise ValueError("Installer changed during verification.")
            metadata = work / "receipt.json"
            metadata.write_bytes(canonical_json({"builder_version": "1.0.0", "client_version": request.payload.client_version,
                "profile_sha256": request.profile.sha256, "signer_thumbprint": request.signer.thumbprint,
                "installer_sha256": before}))
            check_cancel()
            publish_no_replace(metadata, receipt)
            published_receipt = True
            check_cancel()
            if digest(artifact) != before:
                raise ValueError("Installer changed before publication.")
            check_cancel()
            publish_no_replace(artifact, installer)
            published_receipt = False  # EXE + receipt are now the completed transaction.
            progress(BuildEvent("complete", f"Installer created. SHA-256: {before}"))
            return BuildResult(installer, receipt, before)
    finally:
        if published_receipt:
            receipt.unlink(missing_ok=True)
        reservation.unlink(missing_ok=True)
