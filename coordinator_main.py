"""HTTPS entry point for the installed KSAT faculty coordinator."""

from __future__ import annotations

import argparse
import importlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ksat.coordinator.process_lock import CoordinatorProcessLock
from ksat.coordinator.tls import (
    CoordinatorSecurity,
    load_or_create_coordinator_security,
    renew_coordinator_server_certificate,
)


APP_VERSION = "2.0.0"
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)


def _environment_value(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class CoordinatorRuntimeSettings:
    data_dir: Path
    hostname: str
    bind_host: str = "0.0.0.0"
    port: int = 8443
    interactive: bool = True

    def __post_init__(self) -> None:
        data_dir = Path(self.data_dir).resolve()
        object.__setattr__(self, "data_dir", data_dir)
        if not isinstance(self.hostname, str) or not self.hostname.strip():
            raise ValueError("Coordinator hostname is required.")
        if self.hostname != self.hostname.strip():
            raise ValueError("Coordinator hostname is invalid.")
        hostname = self.hostname.lower()
        try:
            hostname.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("Coordinator hostname is invalid.") from error
        if not _HOSTNAME.fullmatch(hostname):
            raise ValueError("Coordinator hostname is invalid.")
        object.__setattr__(self, "hostname", hostname)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Coordinator port is invalid.")
        if self.bind_host != "0.0.0.0":
            try:
                bind = ipaddress.ip_address(self.bind_host)
            except ValueError as error:
                raise ValueError("Coordinator bind host is invalid.") from error
            if not (bind.is_private or bind.is_loopback) or bind.is_unspecified:
                raise ValueError("Coordinator bind host is invalid.")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "CoordinatorRuntimeSettings":
        values = os.environ if environ is None else environ
        program_data = _environment_value(values, "ProgramData", "PROGRAMDATA")
        configured_data = values.get("KSAT_COORDINATOR_DATA_DIR")
        if configured_data:
            data_dir = Path(configured_data)
        elif program_data:
            data_dir = Path(program_data) / "KSAT Coordinator"
        else:
            raise ValueError("ProgramData is unavailable.")
        persisted = _load_runtime_settings(data_dir)
        hostname = values.get("KSAT_COORDINATOR_HOSTNAME")
        if hostname is None and persisted is not None:
            hostname = persisted.hostname
        if hostname is None:
            computer_name = values.get("COMPUTERNAME") or socket.gethostname()
            hostname = f"{computer_name.lower()}.local"
        raw_port = values.get(
            "KSAT_COORDINATOR_PORT",
            str(persisted.port if persisted is not None else 8443),
        )
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as error:
            raise ValueError("Coordinator port is invalid.") from error
        if str(port) != raw_port:
            raise ValueError("Coordinator port is invalid.")
        noninteractive = values.get(
            "KSAT_NONINTERACTIVE",
            "0" if persisted is None or persisted.interactive else "1",
        )
        if noninteractive not in {"0", "1"}:
            raise ValueError("KSAT_NONINTERACTIVE must be 0 or 1.")
        return cls(
            data_dir=data_dir,
            hostname=hostname,
            bind_host=values.get(
                "KSAT_COORDINATOR_BIND_HOST",
                persisted.bind_host if persisted is not None else "0.0.0.0",
            ),
            port=port,
            interactive=noninteractive != "1",
        )


def _runtime_config_path(data_dir: Path) -> Path:
    return Path(data_dir) / "coordinator-runtime.json"


def _load_runtime_settings(data_dir: Path) -> CoordinatorRuntimeSettings | None:
    path = _runtime_config_path(data_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("Coordinator runtime configuration is invalid.") from error
    if len(raw) > 64 * 1024:
        raise ValueError("Coordinator runtime configuration is invalid.")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("Coordinator runtime configuration is invalid.") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"bind_host", "hostname", "interactive", "port", "version"}
        or value.get("version") != APP_VERSION
        or type(value.get("interactive")) is not bool
    ):
        raise ValueError("Coordinator runtime configuration is invalid.")
    settings = CoordinatorRuntimeSettings(
        data_dir=Path(data_dir),
        hostname=value["hostname"],
        bind_host=value["bind_host"],
        port=value["port"],
        interactive=value["interactive"],
    )
    expected = json.dumps(
        {
            "bind_host": settings.bind_host,
            "hostname": settings.hostname,
            "interactive": settings.interactive,
            "port": settings.port,
            "version": APP_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if raw != expected:
        raise ValueError("Coordinator runtime configuration is invalid.")
    return settings


def save_runtime_settings(
    program_data: Path,
    *,
    hostname: str,
    port: int = 8443,
) -> CoordinatorRuntimeSettings:
    data_dir = Path(program_data).resolve() / "KSAT Coordinator"
    if os.path.lexists(data_dir) and (data_dir.is_symlink() or not data_dir.is_dir()):
        raise ValueError("Coordinator data directory is invalid.")
    settings = CoordinatorRuntimeSettings(
        data_dir=data_dir,
        hostname=hostname,
        bind_host="0.0.0.0",
        port=port,
        interactive=True,
    )
    raw = json.dumps(
        {
            "bind_host": settings.bind_host,
            "hostname": settings.hostname,
            "interactive": settings.interactive,
            "port": settings.port,
            "version": APP_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _runtime_config_path(data_dir)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".coordinator-runtime.", suffix=".tmp", dir=data_dir
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        with path.open("rb+") as stream:
            os.fsync(stream.fileno())
        loaded = _load_runtime_settings(data_dir)
        if loaded != settings:
            raise ValueError("Coordinator runtime configuration is invalid.")
        return loaded
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_faculty_application():
    return importlib.import_module("app").app


def _open_browser(url: str) -> None:
    webbrowser.open(url, new=1)


def run_coordinator(
    settings: CoordinatorRuntimeSettings | None = None,
    *,
    security_loader: Callable[..., CoordinatorSecurity] = load_or_create_coordinator_security,
    app_loader: Callable[[], object] = _load_faculty_application,
    uvicorn_runner: Callable[..., None] | None = None,
    browser_opener: Callable[[str], None] = _open_browser,
    lock_factory: Callable[[Path], CoordinatorProcessLock] = CoordinatorProcessLock,
) -> None:
    runtime = settings or CoordinatorRuntimeSettings.from_environment()
    runtime.data_dir.mkdir(parents=True, exist_ok=True)
    process_lock = lock_factory(runtime.data_dir).acquire()
    previous = {
        name: os.environ.get(name)
        for name in (
            "KSAT_DATA_DIR",
            "SESSION_SECRET",
            "KSAT_SESSION_SECRET",
            "KSAT_HTTPS_ONLY",
        )
    }
    try:
        security = security_loader(
            runtime.data_dir,
            hostname=runtime.hostname,
            port=runtime.port,
        )
        os.environ["KSAT_DATA_DIR"] = str(runtime.data_dir)
        os.environ["SESSION_SECRET"] = security.browser_session_secret
        os.environ["KSAT_SESSION_SECRET"] = security.client_session_secret
        os.environ["KSAT_HTTPS_ONLY"] = "1"
        application = app_loader()
        application.state.coordinator_process_lock = process_lock
        application.state.coordinator_process_lock_release_on_shutdown = False
        if runtime.interactive:
            browser_url = f"https://127.0.0.1:{runtime.port}"
            timer = threading.Timer(1.2, browser_opener, args=(browser_url,))
            timer.daemon = True
            timer.start()
        if uvicorn_runner is None:
            import uvicorn

            uvicorn_runner = uvicorn.run
        uvicorn_runner(
            application,
            host=runtime.bind_host,
            port=runtime.port,
            ssl_certfile=str(security.server_certificate_path),
            ssl_keyfile=str(security.server_private_key_path),
            log_level="warning",
            use_colors=False,
        )
    finally:
        try:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        finally:
            process_lock.release()


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    coordinator_runner: Callable[..., None] = run_coordinator,
    certificate_renewer: Callable[..., CoordinatorSecurity] = renew_coordinator_server_certificate,
    lock_factory: Callable[[Path], CoordinatorProcessLock] = CoordinatorProcessLock,
) -> int:
    parser = argparse.ArgumentParser(prog="KSATCoordinator")
    parser.add_argument("--install-config", action="store_true")
    parser.add_argument("--renew-certificate", action="store_true")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--confirm-renewal", action="store_true")
    parser.add_argument("--hostname")
    parser.add_argument("--port", type=int, default=8443)
    arguments = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    if sum(bool(value) for value in (
        arguments.install_config, arguments.renew_certificate, arguments.validate_config
    )) > 1:
        parser.error("configuration and certificate renewal are separate operations")
    if arguments.install_config:
        if not arguments.hostname:
            parser.error("--install-config requires --hostname")
        program_data = _environment_value(values, "ProgramData", "PROGRAMDATA")
        if not program_data:
            raise ValueError("ProgramData is unavailable.")
        save_runtime_settings(
            Path(program_data), hostname=arguments.hostname, port=arguments.port
        )
        return 0
    if arguments.renew_certificate:
        if not arguments.confirm_renewal:
            parser.error("--renew-certificate requires --confirm-renewal")
        if arguments.hostname is not None or arguments.port != 8443:
            parser.error("certificate renewal uses the saved coordinator settings")
        runtime = CoordinatorRuntimeSettings.from_environment(values)
        with lock_factory(runtime.data_dir):
            certificate_renewer(
                runtime.data_dir,
                hostname=runtime.hostname,
                port=runtime.port,
                confirmed=True,
            )
        return 0
    if arguments.validate_config:
        if arguments.hostname is not None or arguments.port != 8443:
            parser.error("configuration validation uses the saved coordinator settings")
        runtime = CoordinatorRuntimeSettings.from_environment(values)
        if not _runtime_config_path(runtime.data_dir).is_file():
            raise ValueError("Coordinator runtime configuration is missing.")
        return 0
    if arguments.confirm_renewal:
        parser.error("--confirm-renewal requires --renew-certificate")
    if arguments.hostname is not None or arguments.port != 8443:
        parser.error("runtime settings require --install-config")
    coordinator_runner(CoordinatorRuntimeSettings.from_environment(values))
    return 0


if __name__ == "__main__":
    main()
