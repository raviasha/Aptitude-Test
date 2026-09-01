"""Restart-safe submission delivery from the client's durable outbox."""

from __future__ import annotations

import math
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import httpx

from ksat.client.coordinator import CoordinatorProblem


_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


def _utc_now(clock) -> datetime:
    value = clock.utcnow()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Outbox clock must return a timezone-aware time.")
    return value.astimezone(timezone.utc)


class OutboxWorker:
    def __init__(self, store, coordinator, clock, random_source=random.random):
        if not callable(random_source):
            raise TypeError("Outbox random source must be callable.")
        self.store = store
        self.coordinator = coordinator
        self.clock = clock
        self.random_source = random_source
        self._condition = threading.Condition()
        self._process_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_requested = False

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._run, name="ksat-submission-outbox", daemon=True
            )
            self._thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError("Outbox stop timeout must be a nonnegative finite number.")
        with self._condition:
            self._stop_requested = True
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(float(timeout_seconds))
        with self._condition:
            if self._thread is thread and thread is not None and not thread.is_alive():
                self._thread = None

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def process_due_once(self) -> int:
        if not self._process_lock.acquire(blocking=False):
            return 0
        try:
            now = _utc_now(self.clock)
            pending = self.store.pending_submissions(due_at=now)
            processed = 0
            for item in pending:
                processed += 1
                try:
                    receipt = self.coordinator.submit_bundle(item.bundle)
                except CoordinatorProblem as error:
                    if error.retryable:
                        self._record_retry(item, now, self._safe_problem(error), error.retry_after)
                    else:
                        self.store.require_faculty_intervention(
                            item.attempt_id, last_error=self._safe_problem(error)
                        )
                except (httpx.TimeoutException, httpx.TransportError, ConnectionError) as error:
                    self._record_retry(item, now, self._safe_exception(error), None)
                except (sqlite3.Error, OSError) as error:
                    self._record_retry(item, now, self._safe_exception(error), None)
                except (ValueError, KeyError, TypeError) as error:
                    self.store.require_faculty_intervention(
                        item.attempt_id,
                        last_error=(
                            f"invalid_submission_response: {self._safe_exception(error)}"
                        )[:2000],
                    )
                else:
                    try:
                        self.store.acknowledge(item.attempt_id, receipt)
                    except (sqlite3.Error, OSError) as error:
                        # Replaying a valid duplicate submission returns the same receipt.
                        self._record_retry(item, now, self._safe_exception(error), None)
                    except (ValueError, KeyError, TypeError) as error:
                        self.store.require_faculty_intervention(
                            item.attempt_id,
                            last_error=(
                                f"invalid_submission_receipt: {self._safe_exception(error)}"
                            )[:2000],
                        )
            return processed
        finally:
            self._process_lock.release()

    def _record_retry(self, item, now: datetime, last_error: str, retry_after: float | None) -> None:
        sampled = self.random_source()
        if (
            not isinstance(sampled, (int, float))
            or isinstance(sampled, bool)
            or not math.isfinite(sampled)
            or sampled < 0
            or sampled > 1
        ):
            raise ValueError("Outbox random source returned an invalid value.")
        base = _BACKOFF_SECONDS[min(item.retry_count, len(_BACKOFF_SECONDS) - 1)]
        delay = base * (0.8 + 0.4 * float(sampled))
        if retry_after is not None:
            if not math.isfinite(retry_after) or retry_after < 0:
                raise ValueError("Coordinator retry delay is invalid.")
            delay = max(delay, min(300.0, retry_after))
        next_attempt = now + timedelta(seconds=max(0.0, delay))
        self.store.record_retry(
            item.attempt_id, next_attempt_at=next_attempt, last_error=last_error
        )

    @staticmethod
    def _safe_problem(error: CoordinatorProblem) -> str:
        value = f"{error.code}: {error.message}".strip()
        return value[:2000]

    @staticmethod
    def _safe_exception(error: BaseException) -> str:
        message = str(error).strip()
        if not message:
            message = error.__class__.__name__
        return message[:2000]

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    if self._stop_requested:
                        return
                self.process_due_once()
                with self._condition:
                    if self._stop_requested:
                        return
                    now = _utc_now(self.clock)
                    pending = [
                        item for item in self.store.pending_submissions()
                        if item.status == "pending"
                    ]
                    if not pending:
                        self._condition.wait()
                    else:
                        delay = max(
                            0.0, (pending[0].next_attempt_at - now).total_seconds()
                        )
                        if delay == 0:
                            # A concurrent caller may hold the processing lock. Wait for a wake
                            # or a small bounded interval instead of spinning.
                            delay = 0.05
                        self._condition.wait(delay)
        finally:
            with self._condition:
                if self._thread is threading.current_thread():
                    self._thread = None
                self._condition.notify_all()


__all__ = ["OutboxWorker"]
