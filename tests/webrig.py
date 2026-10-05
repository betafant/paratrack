"""Helpers for testing the web app: a real uvicorn server in a thread, and a runtime filled with known flights."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn

from prack.config import Settings
from prack.ogn.simulator import PilotSpec, Simulator
from prack.runtime import Runtime

from .factory import make_flight
from .rig import T0, scratch_database

SHORT = {"flight_s": 120, "drop_m": 300.0, "stand_s": 300}
DDB = {
    "devices": [
        {
            "device_id": "D00002",
            "registration": "HB-3123",
            "cn": "X1",
            "aircraft_model": "Ozone Rush",
            "identified": "Y",
        },
        {
            "device_id": "D00003",
            "registration": "D-9999",
            "cn": "Y9",
            "aircraft_model": "Gin Boomerang",
            "identified": "N",
        },
    ]
}


@contextmanager
def serve(app, **options) -> Iterator[tuple[str, uvicorn.Server]]:
    """Run an ASGI app on a free port in a thread; yields (base url, the uvicorn server)."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on", **options)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.should_exit = True
        thread.join(15)


def make_runtime(
    tmp_path: Path, *, terrain_transport=None, auth: tuple[str, str] = ("", ""), ddb: bool = True
) -> Runtime:
    settings = Settings(
        data_dir=tmp_path / "data", ddb_enabled=ddb, terrain_enabled=terrain_transport is not None,
        auth_user=auth[0], auth_password=auth[1],
    )  # fmt: skip
    ddb_transport = httpx.MockTransport(lambda request: httpx.Response(200, json=DDB))
    runtime = Runtime(
        settings, feed=False, db=scratch_database(tmp_path, "web.db"), terrain_transport=terrain_transport,
        ddb_transport=ddb_transport,
    )  # fmt: skip
    runtime.db.init()
    if ddb:
        runtime.ddb.refresh()
    return runtime


def fly_day(runtime: Runtime, start: datetime, pilots: list[PilotSpec], *, until: int | None = None) -> Simulator:
    """Feed a simulated flying session through the tracker; with ``until``, stop that many seconds in."""
    sim = Simulator(start, pilots, runtime.regions[0], seed=3)
    for k in range(until if until is not None else sim.duration_s() + 2):
        for line in sim.lines(sim.at(k)):
            runtime.consume(sim.at(k), line)
    return sim


def populate(runtime: Runtime) -> dict:
    """Known content: 3 flights on 2026-07-15, 1 on 2026-07-14, an open flight on 2026-07-16, and things not to list."""
    launch = (46.6453, 7.6511)
    fly_day(runtime, datetime(2026, 7, 14, 9, 0, tzinfo=UTC), [PilotSpec("D00001", ("flarm",), launch=launch, **SHORT)])
    fly_day(
        runtime, datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        [
            PilotSpec("D00001", ("flarm", "fanet"), name="Mia", launch=launch, **SHORT),
            PilotSpec("D00002", ("flarm",), launch=(46.70, 7.80), delay_s=60, **SHORT),
            PilotSpec("D00003", ("fanet",), launch=(46.55, 8.20), delay_s=120, **SHORT),
        ],
    )  # fmt: skip
    runtime.tracker.close_all()
    runtime.finalizer.drain()
    sim = fly_day(
        runtime,
        datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
        [PilotSpec("D00004", ("flarm",), launch=launch, **SHORT)],
        until=260,
    )
    runtime.tracker.flush(force=True)  # the open flight, 200 s in the air
    # not to be listed: a drive (closed, not airborne) and a hop (open, hardly any altitude change); both dated 07-15
    drive = make_flight(runtime.db, address="CAR001", start=T0 + 400 * 86400, seconds=300)
    hop = make_flight(
        runtime.db, address="HOP001", start=T0 + 401 * 86400, seconds=300, status="active", close_reason=None
    )
    return {"sim": sim, "drive": drive, "hop": hop}
