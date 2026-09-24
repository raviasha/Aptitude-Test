"""Narrow privileged installer runner with verified rollback."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ksat.protocol import canonical_json
from ksat.update_protocol import parse_client_update


_INSTALL_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
_SERVICE = "KSATLabClientAuthority"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    success: bool
    stage: str
    diagnostic_code: str | None
    rolled_back: bool


class HealthVerifier:
    def __init__(self, version_probe, store_probe) -> None:
        self.version_probe = version_probe
        self.store_probe = store_probe

    def verify(
        self, expected_version: str,
        identity_digest: str, config_digest: str, state_digest: str,
        current_identity_digest: str, current_config_digest: str, current_state_digest: str,
    ) -> bool:
        value = self.version_probe()
        if value != {"version": expected_version}:
            raise ValueError("Client health version does not match the installed update.")
        if (identity_digest, config_digest, state_digest) != (
            current_identity_digest, current_config_digest, current_state_digest
        ):
            raise ValueError("Client update changed protected device data.")
        if self.store_probe() is not True:
            raise ValueError("Client authenticated store is unreadable.")
        return True


class Updater:
    def __init__(
        self, protected_root: Path, public_key_b64: str, authenticode_verifier,
        services, installers, health,
        *, installed_version: str, identity_digest: str, config_digest: str, state_digest: str,
        current_digest_probe=None,
    ) -> None:
        self.root = Path(protected_root).resolve(strict=True)
        self.public_key_b64 = public_key_b64
        self.authenticode_verifier = authenticode_verifier
        self.services = services
        self.installers = installers
        self.health = health
        self.installed_version = installed_version
        self.identity_digest = identity_digest
        self.config_digest = config_digest
        self.state_digest = state_digest
        self.current_digest_probe = current_digest_probe or (
            lambda: (self.identity_digest, self.config_digest, self.state_digest)
        )
        self.journal_path = self.root / "update-journal.json"
        self.last_known_good_path = self.root / "last-known-good" / f"KSATClientSetup-{installed_version}.exe"
        self.predecessor_path = self.root / "last-known-good" / "predecessor.exe"

    def _inside(self, path: Path, *, filename: str | None = None) -> Path:
        try:
            candidate = Path(path).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("Client update path is unavailable.") from error
        if not candidate.is_relative_to(self.root) or (filename is not None and candidate.name != filename):
            raise ValueError("Client update path is outside protected storage.")
        return candidate

    def _journal(self, stage: str, diagnostic_code: str | None = None) -> None:
        data = canonical_json({"stage":stage,"diagnostic_code":diagnostic_code})
        descriptor, name = tempfile.mkstemp(prefix=".update-journal-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor,"wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,self.journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_request(self, request_path: Path):
        request_path = self._inside(request_path, filename="install-request.json")
        try:
            raw=request_path.read_bytes(); value=json.loads(raw)
            required={"format_version","release_id","target_version","bundle_path","attempt_id"}
            if not isinstance(value,dict) or set(value)!=required or value["format_version"]!=1 or canonical_json(value)!=raw:
                raise ValueError
        except (OSError,UnicodeError,ValueError,json.JSONDecodeError) as error:
            raise ValueError("Client update install request is invalid.") from error
        bundle=self._inside(Path(value["bundle_path"]),filename="bundle.ksat-client-update")
        verified=parse_client_update(bundle,self.public_key_b64,self.authenticode_verifier)
        if verified.manifest.release_id!=value["release_id"] or verified.manifest.client_version!=value["target_version"]:
            raise ValueError("Client update install request does not match its bundle.")
        return value,verified

    def _extract_installer(self, verified) -> Path:
        destination=self.root / "candidate-installer.exe"
        with zipfile.ZipFile(verified.bundle_path) as archive:
            data=archive.read(verified.manifest.installer_filename)
        descriptor,name=tempfile.mkstemp(prefix=".candidate-",suffix=".exe",dir=self.root)
        temporary=Path(name)
        try:
            with os.fdopen(descriptor,"wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,destination)
        finally:
            temporary.unlink(missing_ok=True)
        self.authenticode_verifier(destination,verified.manifest.authenticode_publisher)
        return destination

    def _verify_health(self, version: str) -> None:
        current=self.current_digest_probe()
        self.health.verify(version,self.identity_digest,self.config_digest,self.state_digest,*current)

    def _rollback(self, publisher: str) -> bool:
        if not self.last_known_good_path.is_file():
            return False
        try:
            self._journal("rollback_installing")
            self.authenticode_verifier(self.last_known_good_path,publisher)
            self.installers.run(self.last_known_good_path,_INSTALL_ARGS,300)
            self.services.start(_SERVICE,30)
            self._verify_health(self.installed_version)
            self._journal("rolled_back")
            return True
        except Exception:
            self._journal("rollback_failed","rollback_failed")
            return False

    def run(self, request_path: Path) -> UpdateResult:
        value,verified=self._load_request(request_path)
        installer=self._extract_installer(verified)
        diagnostic=None
        try:
            self._journal("stopping_service")
            self.services.stop(_SERVICE,30)
            self._journal("installing")
            self.installers.run(installer,_INSTALL_ARGS,300)
            self._journal("starting_service")
            self.services.start(_SERVICE,30)
            self._journal("health_check")
            self._verify_health(value["target_version"])
            self.last_known_good_path.parent.mkdir(parents=True,exist_ok=True)
            if self.last_known_good_path.is_file():
                os.replace(self.last_known_good_path,self.predecessor_path)
            promoted = self.last_known_good_path.parent / f"KSATClientSetup-{value['target_version']}.exe"
            shutil.copy2(installer,promoted)
            self.last_known_good_path = promoted
            self._journal("healthy")
            return UpdateResult(True,"healthy",None,False)
        except Exception:
            diagnostic="update_install_failed"
            rolled_back=self._rollback(verified.manifest.authenticode_publisher)
            return UpdateResult(False,"rolled_back" if rolled_back else "rollback_failed",diagnostic,rolled_back)


__all__=["HealthVerifier","Updater","UpdateResult"]
