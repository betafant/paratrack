"""APRS-IS client and ingest queue, against the fake OGN server and raw sockets (no internet)."""

from __future__ import annotations

import logging
import socket
import threading
import time

from prack import __version__
from prack.ogn.builder import build_position
from prack.ogn.client import AprsClient
from prack.ogn.fake_server import FakeOgnServer
from prack.ogn.ingest import Ingest
from prack.stats import Counters

from .conftest import utc

FILTER = "a/48.120/5.456/45.480/10.944"
NOON = utc(2026, 7, 15, 12, 0, 0)


def feed_lines(n: int) -> list[str]:
    return [build_position(NOON, 46.6, 8.2 + i * 1e-4, 2200, address=f"{i:06X}") for i in range(n)]


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def make_client(port: int, on_line=lambda line: None, **overrides) -> AprsClient:
    options = {"read_timeout": 5.0, "backoff_initial": 0.05, "backoff_max": 0.2, "connect_timeout": 2.0}
    return AprsClient("127.0.0.1", port, "PRACK1234", FILTER, on_line, **{**options, **overrides})


class RunningClient:
    """Runs a client in a thread; ``stop()`` shuts it down the way the app does."""

    def __init__(self, client: AprsClient) -> None:
        self.client = client
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=client.run, args=(self.stop_event,), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.client.stop()
        self.thread.join(3)
        assert not self.thread.is_alive()


class RecordingStop(threading.Event):
    """Stop event whose ``wait`` records the requested delay and returns at once, so back-off is testable."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.delays: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.delays.append(timeout)
        if len(self.delays) >= self.limit:
            self.set()
        return self.is_set()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- client


def test_login_line_is_read_only_and_names_the_app():
    client = make_client(1)
    assert client.login_line() == f"user PRACK1234 pass -1 vers prack {__version__} filter {FILTER}\r\n"
    assert len("PRACK1234") <= 9


def test_logs_in_and_delivers_lines():
    lines = feed_lines(5)
    got: list[str] = []
    with FakeOgnServer(lines) as server:
        running = RunningClient(make_client(server.port, got.append))
        wait_for(lambda: len(got) == 5)
        status = running.client.status
        assert (status.state, status.lines) == ("connected", 5)
        wait_for(lambda: status.comments >= 1)  # the banner and the periodic comments are not lines
        running.stop()
    assert got == lines
    assert server.logins == [make_client(1).login_line().strip()]
    assert status.server.startswith("aprsc") and status.login.startswith("logresp")
    assert status.state == "stopped" and status.connected_since and status.last_line_at


def raw_server(chunks: list[bytes]) -> tuple[socket.socket, int]:
    """A server that sends the given byte chunks (with pauses) after the login, then holds the line open."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.recv(1024)
            for chunk in chunks:
                conn.sendall(chunk)
                time.sleep(0.03)
            time.sleep(2)

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def test_lines_split_across_packets_and_mixed_line_endings():
    line_a, line_b = feed_lines(2)
    server, port = raw_server(
        [
            b"# aprsc 2.1.19\r\n",
            line_a[:30].encode(),
            (line_a[30:] + "\r\n# logresp X\r\n").encode(),
            (line_b + "\n").encode(),
        ]  # fmt: skip
    )
    got: list[str] = []
    running = RunningClient(make_client(port, got.append))
    wait_for(lambda: len(got) == 2)
    running.stop()
    server.close()
    assert got == [line_a, line_b]


def test_bytes_that_are_not_utf8_do_not_break_the_stream():
    raw = b'FNT1103CE>OGNFNT,qAS,Rx:>101520h Name="J\xfcrgen" 45.0dB\r\n'
    server, port = raw_server([raw])
    got: list[str] = []
    running = RunningClient(make_client(port, got.append))
    wait_for(lambda: len(got) == 1)
    running.stop()
    server.close()
    assert 'Name="J�rgen"' in got[0]


def test_a_line_without_end_cannot_exhaust_memory():
    good = feed_lines(1)[0]
    chunks = [b"X" * 16384] * 6 + [b"\r\n", (good + "\r\n").encode()]
    server, port = raw_server(chunks)
    got: list[str] = []
    running = RunningClient(make_client(port, got.append))
    wait_for(lambda: good in got)
    running.stop()
    server.close()
    assert all(len(line) < 1000 for line in got)


def test_an_error_in_the_line_handler_does_not_end_the_session(caplog):
    lines = feed_lines(3)
    got: list[str] = []

    def on_line(line: str) -> None:
        if line == lines[0]:
            raise RuntimeError("bad line")
        got.append(line)

    with FakeOgnServer(lines) as server, caplog.at_level(logging.ERROR, logger="prack.ogn.client"):
        running = RunningClient(make_client(server.port, on_line))
        wait_for(lambda: len(got) == 2)
        running.stop()
    assert got == lines[1:] and "Failed to process line" in caplog.text


def test_reconnects_after_the_server_hangs_up():
    lines = feed_lines(10)
    got: list[str] = []
    with FakeOgnServer(lines, hangup_after=3, interval=0.01) as server:
        running = RunningClient(make_client(server.port, got.append))
        wait_for(lambda: len(got) >= 6 and server.connections >= 2)
        running.stop()
    assert got[:6] == lines[:3] + lines[:3]
    assert running.client.status.reconnects >= 1
    assert len(set(server.logins)) == 1 and len(server.logins) >= 2  # same login every time


def test_a_silent_server_triggers_a_reconnect():
    with FakeOgnServer(silent=True) as server:
        running = RunningClient(make_client(server.port, read_timeout=0.3))
        wait_for(lambda: server.connections >= 2)
        assert "no data from the server for 0.3 s" in (running.client.status.last_error or "")
        running.stop()


def test_keepalive_is_sent_while_connected():
    with FakeOgnServer() as server:
        running = RunningClient(make_client(server.port, keepalive_interval=0.2))
        wait_for(lambda: "#keepalive" in server.received)
        running.stop()


def test_stop_is_prompt_even_while_waiting_for_data():
    with FakeOgnServer(silent=True) as server:
        client = make_client(server.port, read_timeout=60.0, keepalive_interval=240.0)
        running = RunningClient(client)
        wait_for(lambda: client.status.state == "connected")
        started = time.monotonic()
        running.stop()
        assert time.monotonic() - started < 1.5
    assert client.status.state == "stopped"


def test_connection_failures_back_off_exponentially_up_to_five_minutes():
    stop = RecordingStop(limit=9)
    client = AprsClient("127.0.0.1", closed_port(), "PRACK1234", FILTER, lambda line: None, connect_timeout=1.0)
    client.run(stop)
    assert stop.delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert client.status.last_error and client.status.state == "stopped"
    assert client.status.reconnects == 8


def test_back_off_starts_over_after_a_healthy_session():
    lines = feed_lines(20)
    with FakeOgnServer(lines, hangup_after=4, interval=0.1) as server:
        stop = RecordingStop(limit=3)
        make_client(server.port, backoff_initial=5.0, backoff_max=300.0, healthy_after=0.2).run(stop)
    assert stop.delays == [5.0, 5.0, 5.0]  # every session lasted > 0.2 s, so the delay never doubled
    with FakeOgnServer(lines, hangup_after=4, interval=0.001) as server:
        stop = RecordingStop(limit=3)
        make_client(server.port, backoff_initial=5.0, backoff_max=300.0, healthy_after=60.0).run(stop)
    assert stop.delays == [5.0, 10.0, 20.0]  # short sessions count as failures


# ---------------------------------------------------------------- ingest


def test_ingest_hands_every_line_over_in_order_with_its_reception_time():
    lines = feed_lines(10)
    received: list[tuple] = []
    counters = Counters()
    with FakeOgnServer(lines) as server:
        client = make_client(server.port)
        ingest = Ingest(client, lambda t, line: received.append((t, line)), counters)
        before = time.time()
        ingest.start()
        wait_for(lambda: len(received) == 10)
        ingest.stop()
    assert [line for _, line in received] == lines
    assert all(t.tzinfo is not None and before - 1 <= t.timestamp() <= time.time() + 1 for t, _ in received)
    assert counters.get("ingest.lines") == 10 and counters.get("ingest.queue_dropped") == 0


def test_a_full_queue_drops_and_counts_instead_of_growing():
    counters = Counters()
    ingest = Ingest(make_client(1), lambda t, line: None, counters, max_queue=5)
    for i in range(20):
        ingest.put(f"line {i}")
    assert ingest.queue.qsize() == 5
    assert (counters.get("ingest.lines"), counters.get("ingest.queue_dropped")) == (20, 15)


def test_a_failing_consumer_does_not_stop_the_feed(caplog):
    lines = feed_lines(6)
    seen: list[str] = []

    def consumer(t, line: str) -> None:
        if line in (lines[1], lines[4]):
            raise ValueError("boom")
        seen.append(line)

    counters = Counters()
    with FakeOgnServer(lines) as server, caplog.at_level(logging.ERROR, logger="prack.ogn.ingest"):
        ingest = Ingest(make_client(server.port), consumer, counters)
        ingest.start()
        wait_for(lambda: counters.get("ingest.lines") == 6 and ingest.queue.empty())
        ingest.stop()
    assert seen == [lines[0], lines[2], lines[3], lines[5]] and counters.get("ingest.consumer_errors") == 2
    assert "Failed to process line #1" in caplog.text


def test_stop_lets_the_consumer_finish_what_is_queued():
    lines = feed_lines(40)
    done: list[str] = []

    def slow(t, line: str) -> None:
        time.sleep(0.01)
        done.append(line)

    counters = Counters()
    with FakeOgnServer(lines, per_tick=40, interval=0.01) as server:
        ingest = Ingest(make_client(server.port), slow, counters)
        ingest.start()
        wait_for(lambda: counters.get("ingest.lines") == 40)
        ingest.stop()
    assert done == lines


def test_ingest_without_traffic_stops_cleanly():
    with FakeOgnServer() as server:
        ingest = Ingest(make_client(server.port), lambda t, line: None, Counters())
        ingest.start()
        time.sleep(0.2)
        started = time.monotonic()
        ingest.stop()
        assert time.monotonic() - started < 2
