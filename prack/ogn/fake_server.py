"""A fake OGN APRS-IS server for tests and offline end-to-end runs (no internet needed).

It behaves like the real thing as far as the client is concerned: a banner comment, a ``logresp`` after the
login line, then APRS lines at a fixed rate, a ``#`` comment now and then, and optional hang-ups.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Callable, Iterable, Sequence


class FakeOgnServer:
    """Serve ``lines`` (a sequence, or ``source(tick) -> lines`` called once per tick) to every client.

    * ``per_tick`` lines are sent every ``interval`` seconds (a sequence is sent once per connection).
    * ``hangup_after``: close each connection after that many lines (tests reconnecting).
    * ``silent``: send nothing after the login (tests the client's read time-out).
    * ``comment_every``: seconds between ``# <time>`` comments, like the real server's ~20 s.
    Received login lines are in ``logins``, other client lines (``#keepalive``) in ``received``.
    """

    def __init__(
        self,
        lines: Sequence[str] | Callable[[int], Iterable[str]] = (),
        *,
        interval: float = 0.02,
        per_tick: int = 1,
        hangup_after: int | None = None,
        silent: bool = False,
        comment_every: float = 0.5,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.lines = lines
        self.interval = interval
        self.per_tick = per_tick
        self.hangup_after = hangup_after
        self.silent = silent
        self.comment_every = comment_every
        self.host = host
        self.port = port
        self.logins: list[str] = []
        self.received: list[str] = []
        self.connections = 0
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> FakeOgnServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> int:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen()
        server.settimeout(0.1)
        self._server = server
        self.port = server.getsockname()[1]
        self._spawn(self._accept_loop)
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        for thread in self._threads:
            thread.join(2.0)

    def _spawn(self, target: Callable[..., None], *args: object) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            self._spawn(self._serve, conn)

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(0.05)
            try:
                conn.sendall(b"# aprsc 2.1.19-g730c5c0 fake-ogn\r\n")
                login = self._read_login(conn)
                if login is None:
                    return
                self.logins.append(login)
                conn.sendall(b"# logresp FAKE unverified, server FAKE\r\n")
                if self.silent:
                    self._drain(conn)
                    return
                self._stream(conn)
            except OSError:
                return

    def _read_login(self, conn: socket.socket) -> str | None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                return None
            buffer += chunk
            if b"\n" in buffer:
                first, _, rest = buffer.partition(b"\n")
                self._remember(rest)
                return first.decode("ascii", "replace").strip()
        return None

    def _remember(self, data: bytes) -> None:
        for raw in data.split(b"\n"):
            if raw.strip():
                self.received.append(raw.decode("ascii", "replace").strip())

    def _drain(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                if not conn.recv(4096):
                    return
            except TimeoutError:
                continue

    def _stream(self, conn: socket.socket) -> None:
        sent = tick = 0
        pending = list(self.lines) if not callable(self.lines) else []
        next_comment = time.monotonic() + self.comment_every
        while not self._stop.is_set():
            batch: list[str]
            if callable(self.lines):
                batch = list(self.lines(tick))
            else:
                batch, pending = pending[: self.per_tick], pending[self.per_tick :]
            tick += 1
            for line in batch:
                conn.sendall(line.encode("utf-8") + b"\r\n")
                sent += 1
                if self.hangup_after is not None and sent >= self.hangup_after:
                    return
            now = time.monotonic()
            if now >= next_comment:
                conn.sendall(f"# {time.strftime('%d %b %Y %H:%M:%S GMT', time.gmtime())} FAKE\r\n".encode())
                next_comment = now + self.comment_every
            readable, _, _ = select.select([conn], [], [], self.interval)  # one tick; collect #keepalive
            if readable:
                data = conn.recv(4096)
                if not data:
                    return
                self._remember(data)
