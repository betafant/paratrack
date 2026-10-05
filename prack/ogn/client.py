"""Read-only APRS-IS client for the Open Glider Network.

Etiquette: one connection, ``pass -1`` (read only), the app named in ``vers``, an area filter, a
``#keepalive`` every 4 minutes and exponential back-off (5 s up to 5 min) after any error.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .. import __version__
from ..db import utcnow

log = logging.getLogger(__name__)

MAX_LINE_BYTES = 64 * 1024  # a "line" longer than this is garbage; do not let it grow without bound


@dataclass
class LinkStatus:
    state: str = "idle"  # idle | connecting | connected | waiting | error | stopped
    server: str = ""  # server banner, e.g. "aprsc 2.1.19-g730c5c0"
    login: str = ""  # server's answer to the login, e.g. "logresp PRACK1234 unverified, server GLIDERN2"
    connected_since: str | None = None
    lines: int = 0  # aircraft / station lines delivered
    comments: int = 0  # server comments (every ~20 s)
    last_line_at: str | None = None
    last_error: str | None = None  # kept after reconnecting: it explains why the link dropped
    last_error_at: str | None = None
    reconnects: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class AprsClient:
    def __init__(
        self,
        host: str,
        port: int,
        callsign: str,
        filter_expr: str,
        on_line: Callable[[str], None],
        *,
        read_timeout: float = 90.0,  # no data at all (not even a server comment) for this long: reconnect
        keepalive_interval: float = 240.0,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
        healthy_after: float = 60.0,  # a session this long resets the back-off
        connect_timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.callsign = callsign
        self.filter_expr = filter_expr
        self.on_line = on_line
        self.read_timeout = read_timeout
        self.keepalive_interval = keepalive_interval
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.healthy_after = healthy_after
        self.connect_timeout = connect_timeout
        self.status = LinkStatus()
        self._sock: socket.socket | None = None

    def login_line(self) -> str:
        return f"user {self.callsign} pass -1 vers prack {__version__} filter {self.filter_expr}\r\n"

    def run(self, stop: threading.Event) -> None:
        """Connect and read until ``stop`` is set, reconnecting with back-off after errors."""
        backoff = self.backoff_initial
        while not stop.is_set():
            started = time.monotonic()
            try:
                self._session(stop)
            except Exception as exc:  # noqa: BLE001 - the feed must survive whatever the network does
                if stop.is_set():
                    break
                self.status.state = "error"
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                self.status.last_error_at = utcnow().isoformat()
                log.warning("OGN connection problem: %s (retry in %g s)", exc, backoff)
            finally:
                self._close()
            if stop.is_set():
                break
            if time.monotonic() - started >= self.healthy_after:
                backoff = self.backoff_initial
            if self.status.state != "error":
                self.status.state = "waiting"
            if stop.wait(backoff):
                break
            self.status.reconnects += 1
            backoff = min(backoff * 2, self.backoff_max)
        self.status.state = "stopped"

    def stop(self) -> None:
        """Close the socket so a blocked read returns; call after setting the stop event."""
        self._close()

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # wakes a read that is blocked in another thread
            with contextlib.suppress(OSError):
                sock.close()

    def _session(self, stop: threading.Event) -> None:
        self.status.state = "connecting"
        log.info("Connecting to %s:%s, filter %s", self.host, self.port, self.filter_expr)
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        self._sock = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(max(0.05, min(15.0, self.read_timeout / 3, self.keepalive_interval / 3)))
        sock.sendall(self.login_line().encode("ascii"))
        self.status.state = "connected"
        self.status.connected_since = utcnow().isoformat()
        last_rx = last_keepalive = time.monotonic()
        buffer = b""
        discarding = False  # inside a line that was too long: skip everything up to its end
        while not stop.is_set():
            now = time.monotonic()
            if now - last_keepalive >= self.keepalive_interval:
                sock.sendall(b"#keepalive\r\n")
                last_keepalive = now
            try:
                chunk = sock.recv(16384)
            except TimeoutError:
                if time.monotonic() - last_rx >= self.read_timeout:
                    raise TimeoutError(f"no data from the server for {self.read_timeout:g} s") from None
                continue
            if not chunk:
                raise ConnectionError("server closed the connection")
            last_rx = time.monotonic()
            if discarding:
                _, newline, chunk = chunk.partition(b"\n")
                if not newline:
                    continue
                discarding = False
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            if len(buffer) > MAX_LINE_BYTES:
                log.warning("Discarding an over-long line from the server (%d bytes and counting)", len(buffer))
                buffer, discarding = b"", True
            for raw in lines:
                self._handle(raw.decode("utf-8", errors="replace").strip())

    def _handle(self, text: str) -> None:
        if not text:
            return
        if text.startswith("#"):
            self.status.comments += 1
            if "logresp" in text:
                self.status.login = text[1:].strip()[:120]
            elif "aprsc" in text:
                self.status.server = text[1:].strip()[:120]
            return
        self.status.lines += 1
        self.status.last_line_at = utcnow().isoformat()
        try:
            self.on_line(text)
        except Exception:  # noqa: BLE001
            log.exception("Failed to process line: %s", text[:200])
