"""Dial Windows short names when single-label .local resolution is unavailable.

Only the dial address changes. The configured HTTPS identity remains the TLS
SNI/verification name and HTTP Host. No IP address is persisted or trusted.
"""

from __future__ import annotations

import re
import socket
import time

import httpx


class HostnameFallbackTransport(httpx.HTTPTransport):
    def __init__(self, hostname: str, **kwargs):
        super().__init__(**kwargs)
        self._hostname = hostname.lower()
        match = re.fullmatch(r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.local", self._hostname)
        self._short_hostname = match[1] if match else None
        self._prefer_short = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (not self._short_hostname or request.url.scheme != "https"
                or request.url.host != self._hostname):
            return super().handle_request(request)

        started = time.monotonic()
        timeouts = dict(request.extensions.get("timeout", {}))
        if not self._prefer_short:
            try:
                return super().handle_request(request)
            except httpx.ConnectError as error:
                # DNS failed before connecting/sending anything: retrying even a
                # POST is safe. Never retry TLS, HTTP, read/write or refusal errors.
                cause = error.__cause__
                seen = set()
                while cause is not None and not isinstance(cause, socket.gaierror):
                    if id(cause) in seen:
                        cause = None
                        break
                    seen.add(id(cause))
                    # httpcore suppresses display of the socket exception with
                    # `raise ... from None`; its typed context is still retained.
                    cause = cause.__cause__ or cause.__context__
                if cause is None:
                    raise
                budget = timeouts.get("connect")
                if budget is not None:
                    remaining = budget - (time.monotonic() - started)
                    # OS DNS may outlast the socket timeout. Still permit this
                    # one pre-send fallback for one-shot setup/build probes,
                    # with at most one second of extra socket-connect budget.
                    timeouts["connect"] = remaining if remaining > 0 else min(budget, 1.0)

        alternate = httpx.Request(
            request.method, request.url.copy_with(host=self._short_hostname),
            headers=request.headers, stream=request.stream,
            extensions={**request.extensions, "timeout": timeouts, "sni_hostname": self._hostname},
        )
        try:
            response = super().handle_request(alternate)
        except httpx.TransportError:
            # Next call may retry the original name if local name service recovers.
            # Do not replay this request: it could already have reached the server.
            self._prefer_short = False
            raise
        # Remember the name, not its address. The OS resolves it on each new TCP
        # connection; existing keepalive connections remain reusable after a probe.
        self._prefer_short = True
        return response
