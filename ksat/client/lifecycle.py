"""Thread-safe document presence, independent of exam integrity monitoring."""
from __future__ import annotations

import threading
import time


class BrowserLifetime:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._connections: set[str] = set()
        self._reservation = clock() + 120
        self._last_disconnect = clock()
        self._closing = False

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    def reserve_launch(self) -> bool:
        with self._lock:
            if self._closing:
                return False
            self._reservation = self._clock() + 120
            return True

    def connect(self, connection_id: str) -> bool:
        with self._lock:
            if self._closing:
                return False
            self._connections.add(connection_id)
            self._reservation = 0
            return True

    def disconnect(self, connection_id: str) -> None:
        with self._lock:
            if connection_id not in self._connections:
                return
            self._connections.remove(connection_id)
            if not self._connections:
                self._last_disconnect = self._clock()

    def ready_to_close(self) -> bool:
        with self._lock:
            now = self._clock()
            return (not self._closing and not self._connections
                    and now >= self._reservation and now >= self._last_disconnect + 15)

    def begin_close(self, *, maintenance_busy: bool) -> bool:
        with self._lock:
            if maintenance_busy or not self.ready_to_close():
                return False
            self._closing = True
            return True
