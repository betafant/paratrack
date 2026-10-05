"""`prack demo` and `prack run` as the user starts them: a real process, a real port, nothing mocked."""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from prack.ogn.fake_server import FakeOgnServer
from prack.ogn.simulator import PilotSpec, Simulator

from .test_client_ingest import wait_for


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def prack(tmp_path, *args: str, **env: str):
    """Run ``python -m prack ARGS`` with a scratch data directory; yield the base URL; stop it again."""
    port = free_port()
    log = (tmp_path / "prack.log").open("w+b")
    environment = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PRACK_")},
        "PRACK_DATA_DIR": str(tmp_path / "data"),
        "PRACK_TERRAIN_ENABLED": "false",
        "PRACK_DDB_ENABLED": "false",
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        **env,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "prack", *args, "--port", str(port)],
        stdout=log, stderr=subprocess.STDOUT, env=environment,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            if process.poll() is not None:
                log.seek(0)
                raise AssertionError(f"prack exited with {process.returncode}:\n{log.read().decode(errors='replace')}")
            try:
                if httpx.get(f"{url}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                log.seek(0)
                raise AssertionError(f"prack did not come up:\n{log.read().decode(errors='replace')}")
            time.sleep(0.2)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()


def test_demo_serves_a_populated_app_offline(tmp_path):
    with prack(tmp_path, "demo", "--history-days", "1", "--pilots", "4", "--speed", "20") as url:
        client = httpx.Client(base_url=url, timeout=20)
        assert "prack" in client.get("/").text.lower()
        assert client.get("/api/config").json()["regions"][0]["id"] == "ch"
        wait_for(lambda: client.get("/api/live").json()["aircraft"], timeout=60)
        live = client.get("/api/live").json()
        assert live["aircraft"] and all(a["src"] in ("FLARM", "FANET", "OGN tracker") for a in live["aircraft"])
        days = client.get("/api/days").json()
        assert days, "the seeded history is on the calendar"
        flights = client.get(f"/api/days/{days[-1]['date']}/flights").json()
        assert flights and flights[0]["preview"]
        status = client.get("/api/status").json()
        assert status["link"]["state"] == "demo"
    assert (tmp_path / "data" / "demo.db").exists() and not (tmp_path / "data" / "prack.db").exists()


def test_demo_starts_from_scratch_each_time(tmp_path):
    yesterday = (datetime.now(ZoneInfo("Europe/Zurich")).date() - timedelta(days=1)).isoformat()
    for _ in range(2):
        with prack(tmp_path, "demo", "--history-days", "1", "--pilots", "3") as url:
            days = httpx.get(f"{url}/api/days", params={"end": yesterday}, timeout=20).json()
            assert days == [{"date": yesterday, "total": 6}]  # six at least; not 12, the old demo database was removed


def test_run_ingests_a_feed_and_serves_it(tmp_path):
    start = datetime.now(UTC) - timedelta(minutes=3)
    pilots = [PilotSpec("D00001", ("flarm",), launch=(46.6453, 7.6511), flight_s=600, drop_m=300.0, stand_s=60)]
    sim = Simulator(start, pilots, seed=5)
    lines = [line for k in range(300) for line in sim.lines(sim.at(k))]
    with (
        FakeOgnServer(lines, per_tick=100, interval=0.01) as server,
        prack(tmp_path, "run", PRACK_OGN_HOST="127.0.0.1", PRACK_OGN_PORT=str(server.port)) as url,
    ):
        client = httpx.Client(base_url=url, timeout=20)
        wait_for(lambda: client.get("/api/status").json()["counters"].get("lines", 0) >= len(lines), timeout=60)
        status = client.get("/api/status").json()
        assert status["link"]["state"] == "connected" and server.logins and "user PRACK" in server.logins[0]
        wait_for(lambda: client.get("/api/live").json()["aircraft"], timeout=30)
        (aircraft,) = client.get("/api/live").json()["aircraft"]
        assert aircraft["id"] == "FLRD00001"
    assert (tmp_path / "data" / "prack.db").exists()


# ------------------------------------------------------- how a service manager (systemd, Docker, kill) stops it


def _feed_lines() -> list[str]:
    start = datetime.now(UTC) - timedelta(minutes=14)
    pilots = [PilotSpec("D00001", ("flarm",), launch=(46.6453, 7.6511), flight_s=600, drop_m=300.0, stand_s=60)]
    sim = Simulator(start, pilots, seed=5)
    return [line for k in range(400) for line in sim.lines(sim.at(k))]


def _stopped_by_sigterm(tmp_path, *args: str) -> tuple[int, str]:
    """Run ``prack ARGS`` against a fake OGN server, send SIGTERM once it is connected, return (exit code, output)."""
    lines = _feed_lines()
    with FakeOgnServer(lines, per_tick=100, interval=0.01) as server:
        environment = {
            **{k: v for k, v in os.environ.items() if not k.startswith("PRACK_")},
            "PRACK_DATA_DIR": str(tmp_path / "data"),
            "PRACK_OGN_HOST": "127.0.0.1",
            "PRACK_OGN_PORT": str(server.port),
            "PRACK_TERRAIN_ENABLED": "false",
            "PRACK_DDB_ENABLED": "false",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "prack", *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment, text=True,
        )  # fmt: skip
        try:
            wait_for(lambda: server.connections > 0 and server.logins, timeout=30)
            time.sleep(3)  # let some lines arrive
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    return process.returncode, output


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is how POSIX service managers stop programs")
def test_sigterm_stops_track_like_ctrl_c_and_exits_normally(tmp_path):
    code, output = _stopped_by_sigterm(tmp_path, "track")
    assert code == 0 and "Stopping ..." in output, output
    with sqlite3.connect(tmp_path / "data" / "prack.db") as db:
        assert db.execute("select count(*) from fixes").fetchone()[0] > 0  # what had arrived was written


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is how POSIX service managers stop programs")
def test_sigterm_makes_diagnose_report_what_it_heard(tmp_path):
    code, output = _stopped_by_sigterm(tmp_path, "diagnose", "120")
    assert code == 0 and "Interrupted, reporting what was received so far." in output, output
    assert "Sources x aircraft types" in output


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is how POSIX service managers stop programs")
def test_sigterm_shuts_prack_run_down_cleanly(tmp_path):
    code, output = _stopped_by_sigterm(tmp_path, "run", "--port", str(free_port()))
    assert "Application shutdown complete" in output and "Traceback" not in output, output
    assert code in (0, -signal.SIGTERM, 130), code
    with sqlite3.connect(tmp_path / "data" / "prack.db") as db:
        assert db.execute("select count(*) from fixes").fetchone()[0] > 0
