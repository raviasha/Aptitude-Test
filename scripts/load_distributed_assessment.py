"""Reproducible distributed-assessment load and outage release gate.

The isolated mode deliberately injects only the network boundary.  Requests still
flow through ``CoordinatorClient`` (including request signatures and strict
response parsing), the production FastAPI routes, SQLite/WAL, ``ClientStore``,
``AssessmentRuntime``, ``OutboxWorker``, and ``SubmissionWriter``.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import patch

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import psutil
from fastapi.testclient import TestClient

import app as faculty_app
from client_app import (
    _load_production_services,
    create_client_app,
    install_client_configuration,
)
from ksat.client.coordinator import CoordinatorClient
from ksat.client.identity import DeviceIdentityStore
from ksat.client.outbox import OutboxWorker
from ksat.client.runtime import AssessmentRuntime, SystemClock
from ksat.client.store import ClientStore
from ksat.coordinator.releases import prepare_release
from ksat.coordinator.submissions import freeze_release_answer_state
from ksat.coordinator.tls import load_or_create_coordinator_security
from ksat.protocol import PublicQuestion


_API_PREFIX = "/api/client/v1"
_UTC = timezone.utc
_PERFORMANCE_THRESHOLDS = frozenset(
    {
        "local_answer_p99_below_100_ms",
        "submission_ack_p95_below_10_s",
    }
)


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return a Hyndman-Fan type 7 sample percentile, rounded to milliseconds.

    Type 7 is the default estimator used by R and NumPy.  It linearly
    interpolates at the zero-based rank ``(n - 1) * p`` instead of promoting a
    high percentile to the sample maximum merely because the sample is small.
    """

    if not values:
        return 0.0
    if not 0.0 <= percentile <= 100.0:
        raise ValueError("Percentile must be from 0 through 100.")
    ordered = sorted(float(item) for item in values)
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = rank - lower
    return round(
        ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction), 3
    )


def _failed_threshold_names(
    thresholds: dict[str, bool], enforce_performance_thresholds: bool
) -> list[str]:
    """Return failed enforced gates, preserving reported smoke-run latency results."""

    return [
        name
        for name, passed in thresholds.items()
        if not passed
        and (
            enforce_performance_thresholds or name not in _PERFORMANCE_THRESHOLDS
        )
    ]


def _latencies(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "maximum": round(max(values, default=0.0), 3),
    }


class LoadGateFailure(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        super().__init__(json.dumps(report, sort_keys=True))
        self.report = report


class _CallMetrics:
    """Record whole production-client calls without replacing their TLS transport."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.request_counts: collections.Counter[str] = collections.Counter()
        self.status_counts: collections.Counter[str] = collections.Counter()
        self.errors: collections.Counter[str] = collections.Counter()
        self.request_latencies_ms: list[float] = []
        self.submission_latencies_ms: list[float] = []

    def call(self, key: str, operation):
        started = time.perf_counter()
        try:
            result = operation()
        except Exception as error:
            code = str(getattr(error, "code", "transport_error"))
            status = getattr(error, "status_code", None)
            with self._lock:
                self.request_counts[key] += 1
                self.errors[code] += 1
                self.status_counts[str(status or 0)] += 1
            raise
        elapsed = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self.request_counts[key] += 1
            self.status_counts["200"] += 1
            self.request_latencies_ms.append(elapsed)
            if key == f"POST {_API_PREFIX}/submissions":
                self.submission_latencies_ms.append(elapsed)
        return result


class _MeasuredCoordinator:
    def __init__(self, coordinator: CoordinatorClient, metrics: _CallMetrics) -> None:
        self._coordinator = coordinator
        self._metrics = metrics

    def submit_bundle(self, bundle):
        return self._metrics.call(
            f"POST {_API_PREFIX}/submissions",
            lambda: self._coordinator.submit_bundle(bundle),
        )

    def enroll(self, *args, **kwargs):
        return self._metrics.call(
            f"POST {_API_PREFIX}/devices/enroll",
            lambda: self._coordinator.enroll(*args, **kwargs),
        )

    def login(self, *args, **kwargs):
        return self._metrics.call(
            f"POST {_API_PREFIX}/session",
            lambda: self._coordinator.login(*args, **kwargs),
        )

    def logout(self):
        return self._metrics.call(
            f"DELETE {_API_PREFIX}/session", self._coordinator.logout
        )

    def prefetch_catalog(self):
        return self._metrics.call(
            f"GET {_API_PREFIX}/releases", self._coordinator.prefetch_catalog
        )

    def assessments(self):
        return self._metrics.call(
            f"GET {_API_PREFIX}/assessments", self._coordinator.assessments
        )

    def start_attempt(self, *args, **kwargs):
        return self._metrics.call(
            f"POST {_API_PREFIX}/attempts/start",
            lambda: self._coordinator.start_attempt(*args, **kwargs),
        )

    def deadline_update(self, attempt_id):
        return self._metrics.call(
            f"GET {_API_PREFIX}/attempts/{attempt_id}/deadline-update",
            lambda: self._coordinator.deadline_update(attempt_id),
        )

    def download_pack(self, entry, destination):
        return self._metrics.call(
            f"GET {_API_PREFIX}/releases/{entry.release_id}/pack",
            lambda: self._coordinator.download_pack(entry, destination),
        )

    def probe_build(self):
        return self._metrics.call(
            f"GET {_API_PREFIX}/build", self._coordinator.probe_build
        )

    def close(self):
        return self._coordinator.close()

    def __getattr__(self, name):
        return getattr(self._coordinator, name)


class _MutableClock:
    def __init__(self) -> None:
        self.offset = timedelta(0)

    def utcnow(self) -> datetime:
        return datetime.now(_UTC) + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


class _RouteTransport:
    """Synchronous HTTPX adapter around the real coordinator ASGI routes."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.available = True
        self._lock = threading.Lock()
        self.request_counts: collections.Counter[str] = collections.Counter()
        self.status_counts: collections.Counter[str] = collections.Counter()
        self.errors: collections.Counter[str] = collections.Counter()
        self.request_latencies_ms: list[float] = []
        self.submission_latencies_ms: list[float] = []
        self.drop_submission_ack_for: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        key = f"{request.method} {path}"
        started = time.perf_counter()
        with self._lock:
            self.request_counts[key] += 1
        if not self.available:
            with self._lock:
                self.errors["coordinator_unavailable"] += 1
            raise httpx.ConnectError("coordinator unavailable", request=request)
        response = self.client.request(
            request.method,
            path,
            params=list(request.url.params.multi_items()),
            headers=dict(request.headers),
            content=request.content,
        )
        device_id = request.headers.get("X-KSAT-Device", "")
        if (
            path == f"{_API_PREFIX}/submissions"
            and response.status_code == 200
            and device_id in self.drop_submission_ack_for
        ):
            with self._lock:
                self.drop_submission_ack_for.remove(device_id)
                self.errors["lost_acknowledgment"] += 1
            raise httpx.ConnectError("acknowledgment lost", request=request)
        elapsed = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self.status_counts[str(response.status_code)] += 1
            self.request_latencies_ms.append(elapsed)
            if path == f"{_API_PREFIX}/submissions":
                self.submission_latencies_ms.append(elapsed)
            if response.status_code >= 400:
                try:
                    code = response.json()["detail"]["code"]
                except Exception:
                    code = f"http_{response.status_code}"
                self.errors[str(code)] += 1
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )


@dataclass
class _ClientMachine:
    root: Path
    identity_store: DeviceIdentityStore
    store: ClientStore
    coordinator: CoordinatorClient
    runtime: AssessmentRuntime
    outbox: OutboxWorker
    clock: _MutableClock
    student_id: str
    content_hash: str
    attempt_id: str | None = None
    program_data: Path | None = None
    metrics: _CallMetrics | None = None
    production_services: Any | None = None
    client_application: Any | None = None
    local_client: TestClient | None = None
    local_client_entered: bool = False

    def restart(self, transport: httpx.BaseTransport | None = None) -> None:
        if self.local_client is not None:
            if self.local_client_entered:
                self.local_client.__exit__(None, None, None)
            else:
                self.local_client.close()
        self.coordinator.close()
        self.store.close()
        if self.production_services is not None:
            lifecycle_lock = self.production_services.lifecycle_lock
            if lifecycle_lock is not None:
                lifecycle_lock.release()
            assert self.program_data is not None and self.metrics is not None
            with patch.dict(os.environ, {"ProgramData": str(self.program_data)}):
                services = _load_production_services()
            services.coordinator = _MeasuredCoordinator(services.coordinator, self.metrics)
            services.background_prefetch = False
            if services.runtime is None:
                raise RuntimeError("Enrolled production client did not restore its runtime.")
            self.production_services = services
            self.identity_store = services.identity_store
            self.store = services.store
            self.coordinator = services.coordinator
            self.runtime = services.runtime
            self.outbox = OutboxWorker(
                self.store,
                self.coordinator,
                self.clock,
                random_source=lambda: 0.5,
            )
            services.outbox = self.outbox
            self.client_application = create_client_app(services)
            self.local_client = TestClient(
                self.client_application, base_url="http://127.0.0.1:8010"
            )
            self.local_client.__enter__()
            self.local_client_entered = True
            context = self.client_application.state.client_context
            context._stop_control_thread()
            context._stop_outbox()
            recovered = self.runtime.recover()
            if recovered is None or recovered.attempt_id != self.attempt_id:
                raise RuntimeError("Client restart did not recover its exact local attempt.")
            return
        assert transport is not None
        self.store = ClientStore(self.root / "state" / "client.sqlite3")
        self.coordinator = CoordinatorClient(
            "http://coordinator.test",
            self.root / "unused-ca.pem",
            self.identity_store,
            transport=transport,
        )
        self.runtime = AssessmentRuntime(
            self.store, self.identity_store.load_or_create(), SystemClock()
        )
        recovered = self.runtime.recover()
        if recovered is None or recovered.attempt_id != self.attempt_id:
            raise RuntimeError("Client restart did not recover its exact local attempt.")
        self.outbox = OutboxWorker(
            self.store, self.coordinator, self.clock, random_source=lambda: 0.5
        )

    def close(self) -> None:
        self.outbox.stop(1.0)
        if self.local_client is not None:
            if self.local_client_entered:
                self.local_client.__exit__(None, None, None)
                self.local_client_entered = False
            else:
                self.local_client.close()
        self.coordinator.close()
        self.store.close()
        if self.production_services is not None:
            lifecycle_lock = self.production_services.lifecycle_lock
            if lifecycle_lock is not None and lifecycle_lock.held:
                lifecycle_lock.release()


class _Fixture:
    def __init__(
        self, root: Path, clients: int, questions: int, *, real_https: bool = False
    ) -> None:
        if os.environ.get("KSAT_LOAD_TEST") != "1":
            raise RuntimeError("Load fixtures require KSAT_LOAD_TEST=1.")
        self.root = Path(root).resolve()
        self.clients = clients
        self.questions = questions
        self.real_https = real_https
        # Student authentication canonicalizes identifiers to upper case.
        self.namespace = f"LOAD-{str(uuid.uuid4()).upper()}"
        self.password = f"{self.namespace}-password"
        self.enrollment_code = f"{self.namespace}-enroll"
        self._originals: tuple[Any, ...] | None = None
        self._original_environment: dict[str, str | None] = {}
        self.test_client: TestClient | None = None
        self.transport_adapter: _RouteTransport | None = None
        self.transport: httpx.MockTransport | None = None
        self.release_id = ""
        self.test_id = 0
        self.question_ids: list[int] = []
        self.student_ids: list[str] = []
        self.pack_path: Path | None = None
        self.metrics = _CallMetrics()
        self.base_url = "http://coordinator.test"
        self.security = None
        self.server_process: subprocess.Popen | None = None
        self.server_processes: list[psutil.Process] = []
        self.stopped_server_cpu_seconds = 0.0
        self.server_peak_rss = 0
        self.writer_metrics_path = self.root / "coordinator-writer-metrics.json"
        self._owns_setup = False
        self._fixture_configured = False

    def __enter__(self) -> "_Fixture":
        try:
            return self._enter()
        except BaseException:
            if not self._owns_setup:
                raise
            try:
                self._stop_https_coordinator()
                if faculty_app.DB_PATH.is_file():
                    self.cleanup()
            finally:
                self.__exit__(*sys.exc_info())
            raise

    def _enter(self) -> "_Fixture":
        self.root.mkdir(parents=True, exist_ok=True)
        self._originals = (
            faculty_app.DATA_DIR,
            faculty_app.DB_PATH,
            faculty_app.BACKUP_DIR,
            faculty_app.QUESTION_BANKS_DIR,
            faculty_app.app.state.coordinator_config,
        )
        for name in ("KSAT_DEVICE_ENROLLMENT_CODE", "KSAT_SESSION_SECRET"):
            self._original_environment[name] = os.environ.get(name)
        self._owns_setup = True
        faculty_app.DATA_DIR = self.root / "coordinator"
        faculty_app.DB_PATH = faculty_app.DATA_DIR / "aptitude.db"
        faculty_app.BACKUP_DIR = faculty_app.DATA_DIR / "backups"
        faculty_app.QUESTION_BANKS_DIR = faculty_app.DATA_DIR / "Question Banks"
        os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = self.enrollment_code
        os.environ["KSAT_SESSION_SECRET"] = f"{self.namespace}-session"
        faculty_app.ensure_schema()
        if self.real_https:
            self.security = load_or_create_coordinator_security(
                faculty_app.DATA_DIR,
                hostname="localhost",
                port=self._free_port(),
                lan_ip_addresses=[],
            )
            self.base_url = f"https://localhost:{self.security.port}"
        faculty_app.configure_coordinator_state(faculty_app.app)
        self._fixture_configured = True
        writer = faculty_app.app.state.coordinator_config.submission_writer
        writer.start()
        self._seed()
        self.test_client = TestClient(faculty_app.app)
        if self.real_https:
            writer.stop(10.0)
            self._start_https_coordinator()
        else:
            self.transport_adapter = _RouteTransport(self.test_client)
            self.transport = httpx.MockTransport(self.transport_adapter)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._owns_setup:
            return
        if self._fixture_configured:
            config = faculty_app.app.state.coordinator_config
            writer = getattr(config, "submission_writer", None)
            if writer is not None:
                writer.stop(10.0)
        if self.test_client is not None:
            self.test_client.close()
        self._stop_https_coordinator()
        if self._originals is not None:
            (
                faculty_app.DATA_DIR,
                faculty_app.DB_PATH,
                faculty_app.BACKUP_DIR,
                faculty_app.QUESTION_BANKS_DIR,
                faculty_app.app.state.coordinator_config,
            ) = self._originals
        for name, value in self._original_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._owns_setup = False
        self._fixture_configured = False

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.bind(("127.0.0.1", 0))
            return int(stream.getsockname()[1])

    def _start_https_coordinator(self) -> None:
        assert self.security is not None
        environment = os.environ.copy()
        environment["KSAT_DATA_DIR"] = str(faculty_app.DATA_DIR)
        environment["KSAT_DEVICE_ENROLLMENT_CODE"] = self.enrollment_code
        environment["KSAT_SESSION_SECRET"] = f"{self.namespace}-session"
        environment["KSAT_HTTPS_ONLY"] = "1"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve-fixture",
            "--serve-port",
            str(self.security.port),
            "--serve-certfile",
            str(self.security.server_certificate_path),
            "--serve-keyfile",
            str(self.security.server_private_key_path),
            "--serve-metrics-file",
            str(self.writer_metrics_path),
        ]
        self.server_process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process = psutil.Process(self.server_process.pid)
        self.server_processes.append(process)
        deadline = time.monotonic() + 20.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.server_process.poll() is not None:
                raise RuntimeError("Disposable HTTPS coordinator exited during startup.")
            try:
                response = httpx.get(
                    f"{self.base_url}/api/build",
                    verify=str(self.security.ca_certificate_path),
                    timeout=1.0,
                )
                if response.status_code == 200:
                    return
            except Exception as error:
                last_error = error
            time.sleep(0.05)
        raise RuntimeError("Disposable HTTPS coordinator did not become healthy.") from last_error

    def _stop_https_coordinator(self) -> None:
        if self.server_process is None:
            return
        try:
            cpu_seconds, rss = self._server_tree_resources()
            self.stopped_server_cpu_seconds += cpu_seconds
            self.server_peak_rss = max(self.server_peak_rss, rss)
        except (psutil.Error, OSError):
            pass
        self.server_process.terminate()
        try:
            self.server_process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self.server_process.kill()
            self.server_process.wait(timeout=5.0)
        self.server_process = None

    def sample_server_resources(self) -> tuple[float, int]:
        cpu_seconds = self.stopped_server_cpu_seconds
        if self.server_process is not None:
            try:
                current_cpu, current_rss = self._server_tree_resources()
                cpu_seconds += current_cpu
                self.server_peak_rss = max(self.server_peak_rss, current_rss)
            except (psutil.Error, OSError):
                pass
        return cpu_seconds, self.server_peak_rss

    def _server_tree_resources(self) -> tuple[float, int]:
        assert self.server_process is not None
        root = psutil.Process(self.server_process.pid)
        processes = [root, *root.children(recursive=True)]
        cpu_seconds = 0.0
        rss = 0
        for process in processes:
            try:
                cpu = process.cpu_times()
                cpu_seconds += cpu.user + cpu.system
                rss += process.memory_info().rss
            except (psutil.Error, OSError):
                continue
        return cpu_seconds, rss

    def writer_queue_depth(self) -> int:
        try:
            value = json.loads(self.writer_metrics_path.read_text(encoding="utf-8"))
            return int(value["maximum_writer_queue_depth"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return 0

    def _seed(self) -> None:
        created = datetime.now(_UTC).isoformat(timespec="seconds")
        selected: list[PublicQuestion] = []
        with faculty_app.db() as connection:
            self.student_ids = [
                f"{self.namespace}-STUDENT-{index:03d}" for index in range(self.clients)
            ]
            password_hash = faculty_app.hash_password(self.password)
            connection.executemany(
                """INSERT INTO students
                   (student_id,name,password_hash,class,section,created_at)
                   VALUES (?,?,?,?,?,?)""",
                [
                    (student_id, f"{self.namespace} Student {index}", password_hash,
                     "LOAD", "LOAD", created)
                    for index, student_id in enumerate(self.student_ids)
                ],
            )
            self.test_id = int(
                connection.execute(
                    """INSERT INTO tests
                       (test_name,composition,created_at,active,launched,mode)
                       VALUES (?,?,?,1,0,'faculty')""",
                    (f"{self.namespace}-TEST", "{}", created),
                ).lastrowid
            )
            for index in range(self.questions):
                correct = "ABCD"[index % 4]
                options = {"A": "one", "B": "two", "C": "three", "D": "four"}
                question_id = int(
                    connection.execute(
                        """INSERT INTO questions
                           (question_text,source_key,category,chapter,difficulty,
                            option_a,option_b,option_c,option_d,options_json,
                            correct_answer,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"{self.namespace} Question {index + 1}?",
                            f"{self.namespace}-Q-{index + 1:03d}",
                            "Load", "Load", "Easy",
                            options["A"], options["B"], options["C"], options["D"],
                            json.dumps(options, sort_keys=True, separators=(",", ":")),
                            correct, created,
                        ),
                    ).lastrowid
                )
                self.question_ids.append(question_id)
                selected.append(
                    PublicQuestion(
                        question_id=question_id,
                        source_key=f"{self.namespace}-Q-{index + 1:03d}",
                        category="Load",
                        chapter="Load",
                        difficulty="Easy",
                        question_text=f"{self.namespace} Question {index + 1}?",
                        question_html=f"<p>{self.namespace} Question {index + 1}?</p>",
                        options=options,
                    )
                )
            release = prepare_release(
                connection,
                test_id=self.test_id,
                selected_questions=selected,
                assets={},
                pack_dir=faculty_app.assessment_packs_dir(),
                signing_private_key_b64=(
                    faculty_app.app.state.coordinator_config.signing_private_key_b64
                ),
                pack_master_key=faculty_app.app.state.coordinator_config.pack_master_key,
                now_iso=created,
            )
            self.release_id = release.release_id
            freeze_release_answer_state(connection, self.release_id)
            now = datetime.now(_UTC)
            connection.execute(
                """UPDATE assessment_releases
                   SET state='launched', launch_opens_at=?, launch_closes_at=?
                   WHERE release_id=?""",
                (
                    (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
                    (now + timedelta(minutes=9)).isoformat(timespec="seconds"),
                    self.release_id,
                ),
            )
            connection.execute(
                """UPDATE tests SET launched=1, launch_expires_at=? WHERE test_id=?""",
                ((now + timedelta(minutes=9)).isoformat(timespec="seconds"), self.test_id),
            )
        self.pack_path = faculty_app.assessment_packs_dir() / f"{self.release_id}.ksatpack"

    def stop_coordinator(self) -> None:
        """Make the transport unavailable and stop the durable server writer."""

        if self.real_https:
            self._stop_https_coordinator()
            return
        assert self.transport_adapter is not None
        self.transport_adapter.available = False
        faculty_app.app.state.coordinator_config.submission_writer.stop(10.0)
        if self.test_client is not None:
            self.test_client.close()

    def restart_coordinator(self) -> None:
        """Recreate persisted coordinator services before restoring connectivity."""

        if self.real_https:
            self._start_https_coordinator()
            return
        assert self.transport_adapter is not None
        faculty_app.configure_coordinator_state(faculty_app.app)
        faculty_app.app.state.coordinator_config.submission_writer.start()
        self.test_client = TestClient(faculty_app.app)
        self.transport_adapter.client = self.test_client
        self.transport_adapter.available = True

    def make_machine(self, index: int, *, start: bool = True) -> _ClientMachine:
        if self.real_https:
            return self._make_production_machine(index, start=start)
        assert self.transport is not None
        root = self.root / "clients" / f"machine-{index:03d}"
        (root / "state").mkdir(parents=True)
        (root / "packs").mkdir(parents=True)
        identity_store = DeviceIdentityStore(root / "identity")
        coordinator = CoordinatorClient(
            "http://coordinator.test",
            root / "unused-ca.pem",
            identity_store,
            transport=self.transport,
        )
        store: ClientStore | None = None
        try:
            coordinator.enroll(f"{self.namespace}-DEVICE-{index:03d}", self.enrollment_code)
            coordinator.login(self.student_ids[index], self.password)
            catalog = coordinator.prefetch_catalog()
            entry = next(item for item in catalog if item.release_id == self.release_id)
            destination = root / "packs" / entry.filename
            coordinator.download_pack(entry, destination)
            store = ClientStore(root / "state" / "client.sqlite3")
            runtime = AssessmentRuntime(store, identity_store.load_or_create(), SystemClock())
            runtime.prepare(entry.descriptor, destination)
            clock = _MutableClock()
            machine = _ClientMachine(
                root=root,
                identity_store=identity_store,
                store=store,
                coordinator=coordinator,
                runtime=runtime,
                outbox=OutboxWorker(store, coordinator, clock, random_source=lambda: 0.5),
                clock=clock,
                student_id=self.student_ids[index],
                content_hash=entry.content_hash,
            )
            if start:
                self.start_machine(machine)
            return machine
        except Exception:
            coordinator.close()
            if store is not None:
                store.close()
            raise

    def _make_production_machine(
        self, index: int, *, start: bool = True
    ) -> _ClientMachine:
        assert self.security is not None
        root = self.root / "clients" / f"machine-{index:03d}"
        program_data = root / "program-data"
        install_client_configuration(
            program_data,
            base_url=self.base_url,
            ca_source=self.security.ca_certificate_path,
            metadata_source=self.security.public_export_dir / "coordinator-public.json",
        )
        client_dir = program_data / "KSAT Client"
        for name in ("identity", "state", "packs"):
            (client_dir / name).mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"ProgramData": str(program_data)}):
            services = _load_production_services()
        services.coordinator = _MeasuredCoordinator(services.coordinator, self.metrics)
        services.background_prefetch = False
        machine: _ClientMachine | None = None
        try:
            services.coordinator.enroll(
                f"{self.namespace}-DEVICE-{index:03d}", self.enrollment_code
            )
            identity = services.identity_store.load_or_create()
            services.runtime = AssessmentRuntime(services.store, identity, SystemClock())
            services.coordinator.login(
                self.student_ids[index], self.password
            )
            catalog = services.coordinator.prefetch_catalog()
            entry = next(item for item in catalog if item.release_id == self.release_id)
            destination = client_dir / "packs" / entry.filename
            services.coordinator.download_pack(entry, destination)
            services.runtime.prepare(entry.descriptor, destination)
            clock = _MutableClock()
            services.outbox = OutboxWorker(
                services.store,
                services.coordinator,
                clock,
                random_source=lambda: 0.5,
            )
            client_application = create_client_app(services)
            machine = _ClientMachine(
                root=client_dir,
                identity_store=services.identity_store,
                store=services.store,
                coordinator=services.coordinator,
                runtime=services.runtime,
                outbox=services.outbox,
                clock=clock,
                student_id=self.student_ids[index],
                content_hash=entry.content_hash,
                program_data=program_data,
                metrics=self.metrics,
                production_services=services,
                client_application=client_application,
                local_client=TestClient(
                    client_application, base_url="http://127.0.0.1:8010"
                ),
            )
            machine.local_client.__enter__()
            machine.local_client_entered = True
            context = client_application.state.client_context
            context._stop_control_thread()
            context._stop_outbox()
            if start:
                self.start_machine(machine)
            return machine
        except Exception:
            if machine is not None:
                machine.close()
            else:
                services.coordinator.close()
                services.store.close()
                if services.lifecycle_lock is not None:
                    services.lifecycle_lock.release()
            raise

    def start_machine(self, machine: _ClientMachine) -> datetime:
        """Start a prepared machine through the production client protocol."""

        catalog = machine.coordinator.assessments()
        if not any(item.release_id == self.release_id for item in catalog):
            raise RuntimeError("Prepared release disappeared before the scheduled start.")
        response = machine.coordinator.start_attempt(
            self.release_id, machine.content_hash
        )
        snapshot = machine.runtime.start(response, student_id=machine.student_id)
        machine.attempt_id = snapshot.attempt_id
        return response.ticket.ticket.started_at

    def cleanup(self) -> dict[str, int]:
        attempt_ids: list[str]
        with faculty_app.db() as connection:
            attempt_ids = [
                row["attempt_id"]
                for row in connection.execute(
                    "SELECT attempt_id FROM attempts WHERE test_id=?", (self.test_id,)
                ).fetchall()
            ]

            def count(table: str, clause: str, values: tuple[Any, ...]) -> int:
                return int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table} WHERE {clause}", values
                    ).fetchone()["count"]
                )

            selectors = [
                ("responses", "attempt_id IN (SELECT attempt_id FROM attempts WHERE test_id=?)", (self.test_id,)),
                ("exam_violations", "attempt_id IN (SELECT attempt_id FROM attempts WHERE test_id=?)", (self.test_id,)),
                ("submissions", "attempt_id IN (SELECT attempt_id FROM attempts WHERE test_id=?)", (self.test_id,)),
                ("audit_events", "test_id=? OR attempt_id IN (SELECT attempt_id FROM attempts WHERE test_id=?)", (self.test_id, self.test_id)),
                ("attempts", "test_id=?", (self.test_id,)),
                ("release_questions", "release_id=?", (self.release_id,)),
                ("assessment_releases", "release_id=?", (self.release_id,)),
                ("tests", "test_id=? AND test_name=?", (self.test_id, f"{self.namespace}-TEST")),
                ("devices", "label LIKE ?", (f"{self.namespace}-DEVICE-%",)),
                ("students", "student_id LIKE ?", (f"{self.namespace}-STUDENT-%",)),
                ("questions", "source_key LIKE ?", (f"{self.namespace}-Q-%",)),
                ("admins", "username=?", (f"{self.namespace.casefold()}-admin",)),
            ]
            created_rows = sum(count(*selector) for selector in selectors)
            deleted_rows = 0
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table, clause, values in selectors:
                    cursor = connection.execute(f"DELETE FROM {table} WHERE {clause}", values)
                    deleted_rows += cursor.rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            residual_rows = sum(count(*selector) for selector in selectors)
        if self.pack_path is not None:
            self.pack_path.unlink(missing_ok=True)
        return {
            "created_rows": created_rows,
            "deleted_rows": deleted_rows,
            "residual_rows": residual_rows,
            "attempt_ids": len(attempt_ids),
        }


def _validate_dimensions(clients: int, questions: int) -> None:
    if type(clients) is not int or not 1 <= clients <= 100:
        raise ValueError("Client count must be from 1 through 100.")
    if type(questions) is not int or not 1 <= questions <= 100:
        raise ValueError("Question count must be from 1 through 100.")


def run_isolated_gate(
    data_dir: Path,
    *,
    clients: int = 100,
    questions: int = 100,
    outage: bool = False,
    start_spread_seconds: float = 30.0,
    submission_spread_seconds: float = 0.0,
    enforce_performance_thresholds: bool = True,
) -> dict[str, Any]:
    """Run a namespaced release gate without mutating an installed system."""

    if os.environ.get("KSAT_LOAD_TEST") != "1":
        raise RuntimeError("Load fixtures require KSAT_LOAD_TEST=1.")
    _validate_dimensions(clients, questions)
    if start_spread_seconds < 0 or submission_spread_seconds < 0:
        raise ValueError("Load spread values must not be negative.")

    wall_started = time.perf_counter()
    memory_peak = 0
    machines: list[_ClientMachine] = []
    local_answer_ms: list[float] = []
    max_writer_queue_depth = 0
    monitor_stop = threading.Event()
    cleanup: dict[str, int] = {}

    with _Fixture(Path(data_dir), clients, questions, real_https=True) as fixture:
        adapter = fixture.metrics
        transport = fixture.transport
        server_cpu_before, memory_peak = fixture.sample_server_resources()

        def monitor() -> None:
            nonlocal max_writer_queue_depth, memory_peak
            while not monitor_stop.wait(0.001):
                max_writer_queue_depth = max(
                    max_writer_queue_depth,
                    fixture.writer_queue_depth(),
                )
                _server_cpu, server_rss = fixture.sample_server_resources()
                memory_peak = max(memory_peak, server_rss)

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        try:
            for index in range(clients):
                machines.append(fixture.make_machine(index, start=False))

            launch_epoch = time.perf_counter()

            def start_scheduled(item: tuple[int, _ClientMachine]) -> datetime:
                index, machine = item
                if clients > 1:
                    target = launch_epoch + start_spread_seconds * index / (clients - 1)
                    remaining = target - time.perf_counter()
                    if remaining > 0:
                        time.sleep(remaining)
                return fixture.start_machine(machine)

            with ThreadPoolExecutor(max_workers=clients) as executor:
                observed_starts = list(executor.map(start_scheduled, enumerate(machines)))
            observed_start_spread = (
                (max(observed_starts) - min(observed_starts)).total_seconds()
                if len(observed_starts) > 1
                else 0.0
            )

            for machine in machines:
                for position, question_id in enumerate(machine.runtime.snapshot().question_order):
                    started = time.perf_counter()
                    answer = "ABCD"[position % 4]
                    if machine.local_client is None:
                        machine.runtime.answer(question_id, answer)
                    else:
                        context = machine.client_application.state.client_context
                        response = machine.local_client.put(
                            f"/api/attempts/{machine.attempt_id}/responses/{question_id}",
                            headers={
                                "Origin": "http://127.0.0.1:8010",
                                "X-KSAT-CSRF": context.csrf_token,
                            },
                            json={"answer": answer},
                        )
                        if response.status_code != 200:
                            raise RuntimeError(
                                f"Local client answer failed: {response.status_code}"
                            )
                    local_answer_ms.append((time.perf_counter() - started) * 1000.0)
                machine.restart(transport)
                if machine.local_client is None:
                    sealed = machine.runtime.submit()
                else:
                    context = machine.client_application.state.client_context
                    response = machine.local_client.post(
                        f"/api/attempts/{machine.attempt_id}/submit",
                        headers={
                            "Origin": "http://127.0.0.1:8010",
                            "X-KSAT-CSRF": context.csrf_token,
                        },
                        json={"confirmed": True},
                    )
                    if response.status_code != 202:
                        raise RuntimeError(
                            f"Local client submit failed: {response.status_code}"
                        )
                    sealed = machine.runtime.snapshot()
                if sealed.state != "sealed_pending":
                    raise RuntimeError("Client did not persist a sealed pending submission.")

            # Recovery deliberately rounds remaining time down.  That can make the
            # trusted local seal instant a few seconds ahead of wall time; advance
            # the injectable outbox clock instead of sleeping in an automated gate.
            for machine in machines:
                machine.clock.advance(10.0)

            with faculty_app.db() as connection:
                submissions_before_barrier = int(
                    connection.execute(
                        """SELECT COUNT(*) AS count FROM submissions s
                           JOIN attempts a ON a.attempt_id=s.attempt_id
                           WHERE a.test_id=?""",
                        (fixture.test_id,),
                    ).fetchone()["count"]
                )

            sealed_before_restart = 0
            coordinator_service_restarted = False
            if outage:
                fixture.stop_coordinator()
                with ThreadPoolExecutor(max_workers=clients) as executor:
                    list(executor.map(lambda item: item.outbox.process_due_once(), machines))
                sealed_before_restart = sum(
                    machine.store.load_attempt(machine.attempt_id).state == "sealed_pending"
                    for machine in machines
                )
                fixture.restart_coordinator()
                coordinator_service_restarted = True
                for machine in machines:
                    machine.clock.advance(31.0)

            barrier = threading.Barrier(clients) if submission_spread_seconds == 0 else None
            submission_epoch = time.perf_counter()

            def submit(item: tuple[int, _ClientMachine]) -> None:
                index, machine = item
                if barrier is not None:
                    barrier.wait(timeout=30.0)
                elif submission_spread_seconds:
                    target = (
                        submission_epoch
                        + submission_spread_seconds * index / max(1, clients - 1)
                    )
                    remaining = target - time.perf_counter()
                    if remaining > 0:
                        time.sleep(remaining)
                machine.outbox.process_due_once()

            with ThreadPoolExecutor(max_workers=clients) as executor:
                list(executor.map(submit, enumerate(machines)))
            monitor_stop.set()
            monitor_thread.join(5.0)

            records = [machine.store.load_attempt(machine.attempt_id) for machine in machines]
            acknowledged = [record for record in records if record.state == "acknowledged"]
            editable_sealed = sum(record.state == "in_progress" for record in records)
            with faculty_app.db() as connection:
                submission_rows = int(
                    connection.execute(
                        """SELECT COUNT(*) AS count FROM submissions s
                           JOIN attempts a ON a.attempt_id=s.attempt_id
                           WHERE a.test_id=?""",
                        (fixture.test_id,),
                    ).fetchone()["count"]
                )
                distinct_rows = int(
                    connection.execute(
                        """SELECT COUNT(DISTINCT s.attempt_id) AS count FROM submissions s
                           JOIN attempts a ON a.attempt_id=s.attempt_id
                           WHERE a.test_id=?""",
                        (fixture.test_id,),
                    ).fetchone()["count"]
                )
            duplicate_count = max(0, submission_rows - distinct_rows)
            missing_attempt_count = clients - len(acknowledged)
            database_size = sum(
                path.stat().st_size
                for path in (
                    faculty_app.DB_PATH,
                    Path(f"{faculty_app.DB_PATH}-wal"),
                    Path(f"{faculty_app.DB_PATH}-shm"),
                )
                if path.is_file()
            )
            cleanup = fixture.cleanup()
            elapsed = max(0.001, time.perf_counter() - wall_started)
            server_cpu_after, server_rss = fixture.sample_server_resources()
            memory_peak = max(memory_peak, server_rss)
            cpu_seconds = max(0.0, server_cpu_after - server_cpu_before)
            report = {
                "schema_version": 1,
                "run_id": fixture.namespace,
                "mode": "production_https_subprocess",
                "network": {
                    "endpoint": "disposable_loopback_https",
                    "trust": "generated_file_pinned_ca",
                    "certificate_store_modified": False,
                },
                "clients": clients,
                "questions": questions,
                "start_spread_seconds": start_spread_seconds,
                "observed_start_spread_seconds": round(observed_start_spread, 3),
                "submission_spread_seconds": submission_spread_seconds,
                "accepted_results": len(acknowledged),
                "missing_attempt_count": missing_attempt_count,
                "duplicate_count": duplicate_count,
                "submissions_before_barrier": submissions_before_barrier,
                "editable_sealed_attempt_count": editable_sealed,
                "corruption_or_signature_error_count": sum(
                    count
                    for code, count in adapter.errors.items()
                    if "corrupt" in code or "signature" in code
                ),
                "request_counts": dict(sorted(adapter.request_counts.items())),
                "status_counts": dict(sorted(adapter.status_counts.items())),
                "errors": dict(sorted(adapter.errors.items())),
                "latency_ms": {
                    "request": _latencies(adapter.request_latencies_ms),
                    "submission_ack": _latencies(adapter.submission_latencies_ms),
                    "local_answer": _latencies(local_answer_ms),
                },
                "coordinator": {
                    "scope": "coordinator_subprocess_only",
                    "cpu_percent_process_average": round(100.0 * cpu_seconds / elapsed, 3),
                    "memory_rss_bytes_peak": memory_peak,
                    "sqlite_bytes": database_size,
                },
                "maximum_writer_queue_depth": max_writer_queue_depth,
                "performance_thresholds_enforced": enforce_performance_thresholds,
                "outage": {
                    "enabled": outage,
                    "coordinator_service_restarted": coordinator_service_restarted,
                    "sealed_pending_before_restart": sealed_before_restart,
                    "acknowledged_after_restart": len(acknowledged),
                },
                "cleanup": cleanup,
                "thresholds": {
                    "all_results_acknowledged": len(acknowledged) == clients,
                    "zero_missing": missing_attempt_count == 0,
                    "zero_duplicates": duplicate_count == 0,
                    "zero_submissions_before_barrier": (
                        submissions_before_barrier == 0
                    ),
                    "zero_corruption_or_signature_errors": not any(
                        "corrupt" in code or "signature" in code
                        for code in adapter.errors
                    ),
                    "zero_database_locked_errors": not any(
                        "database is locked" in code.casefold() for code in adapter.errors
                    ) and adapter.errors.get("submission_failed", 0) == 0,
                    "local_answer_p99_below_100_ms": _percentile(local_answer_ms, 99) < 100,
                    "submission_ack_p95_below_10_s": (
                        _percentile(adapter.submission_latencies_ms, 95) < 10_000
                    ),
                    "all_submission_calls_observed": (
                        adapter.request_counts.get(
                            f"POST {_API_PREFIX}/submissions", 0
                        )
                        >= clients
                    ),
                    "all_submission_latencies_observed": (
                        len(adapter.submission_latencies_ms) == clients
                    ),
                    "requested_start_spread_observed": (
                        clients < 2
                        or start_spread_seconds == 0
                        or abs(observed_start_spread - start_spread_seconds)
                        <= max(2.0, start_spread_seconds * 0.1)
                    ),
                    "cleanup_zero_residual_rows": cleanup["residual_rows"] == 0,
                },
            }
            failed_thresholds = _failed_threshold_names(
                report["thresholds"], enforce_performance_thresholds
            )
            report["failed_enforced_thresholds"] = failed_thresholds
            if failed_thresholds:
                raise LoadGateFailure(report)
            return report
        finally:
            monitor_stop.set()
            monitor_thread.join(5.0)
            close_errors: list[BaseException] = []
            try:
                for machine in machines:
                    try:
                        machine.close()
                    except BaseException as error:
                        close_errors.append(error)
            finally:
                if not cleanup:
                    fixture.cleanup()
            if close_errors and sys.exc_info()[0] is None:
                raise RuntimeError("One or more load clients did not close cleanly.") \
                    from close_errors[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--questions", type=int, default=100)
    parser.add_argument("--start-spread-seconds", type=float, default=30.0)
    parser.add_argument("--submission-spread-seconds", type=float, default=0.0)
    parser.add_argument("--report", type=Path, default=Path("load-report.json"))
    parser.add_argument("--outage", action="store_true")
    parser.add_argument("--serve-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--serve-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--serve-certfile", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--serve-keyfile", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--serve-metrics-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--isolated",
        action="store_true",
        help=(
            "Use a disposable database, separate loopback HTTPS coordinator, "
            "and generated file-pinned CA."
        ),
    )
    return parser


def _serve_fixture(arguments: argparse.Namespace) -> int:
    import uvicorn

    if (
        arguments.serve_port is None
        or arguments.serve_certfile is None
        or arguments.serve_keyfile is None
        or arguments.serve_metrics_file is None
        or os.environ.get("KSAT_LOAD_TEST") != "1"
    ):
        raise SystemExit("Disposable coordinator fixture arguments are incomplete.")
    stop = threading.Event()
    maximum = 0

    def monitor_writer() -> None:
        nonlocal maximum
        while not stop.wait(0.001):
            maximum = max(
                maximum,
                faculty_app.app.state.coordinator_config.submission_writer.pending_count,
            )
            arguments.serve_metrics_file.write_text(
                json.dumps({"maximum_writer_queue_depth": maximum}),
                encoding="utf-8",
            )

    monitor = threading.Thread(target=monitor_writer, daemon=True)
    monitor.start()
    try:
        uvicorn.run(
            faculty_app.app,
            host="127.0.0.1",
            port=arguments.serve_port,
            ssl_certfile=str(arguments.serve_certfile),
            ssl_keyfile=str(arguments.serve_keyfile),
            log_level="warning",
        )
    finally:
        stop.set()
        monitor.join(2.0)
        arguments.serve_metrics_file.write_text(
            json.dumps({"maximum_writer_queue_depth": maximum}),
            encoding="utf-8",
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.serve_fixture:
        return _serve_fixture(arguments)
    if not arguments.isolated:
        raise SystemExit(
            "External fixture provisioning is intentionally disabled; use --isolated, or "
            "follow the operations runbook for an authorized disposable coordinator."
        )
    if arguments.base_url is not None or arguments.ca_file is not None:
        raise SystemExit(
            "--base-url and --ca-file are not used by --isolated; the gate creates "
            "and reports a disposable loopback HTTPS endpoint with a file-pinned CA."
        )
    failed = False
    with tempfile.TemporaryDirectory(prefix="ksat-load-gate-") as directory:
        try:
            report = run_isolated_gate(
                Path(directory),
                clients=arguments.clients,
                questions=arguments.questions,
                outage=arguments.outage,
                start_spread_seconds=arguments.start_spread_seconds,
                submission_spread_seconds=arguments.submission_spread_seconds,
            )
        except LoadGateFailure as error:
            report = error.report
            failed = True
    destination = arguments.report.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(destination)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
