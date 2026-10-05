"""Demo mode: simulated pilots through the real tracker, offline, in a database of its own."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from prack.config import Settings
from prack.demo import Demo, demo_database_path, remove_demo_database, terrain_works
from prack.models import Flight
from prack.runtime import Runtime
from prack.terrain import TerrainService

from .helpers import terrarium_png
from .rig import scratch_database

URL = "https://tiles.example/{z}/{x}/{y}.png"


def make_demo_runtime(tmp_path, transport=None) -> Runtime:
    settings = Settings(data_dir=tmp_path / "data", ddb_enabled=False, terrain_enabled=transport is not None)
    return Runtime(settings, feed=False, db=scratch_database(tmp_path, "demo.db"), terrain_transport=transport)


def flights_by_date(runtime: Runtime) -> dict:
    with runtime.db.session() as s:
        return dict(s.execute(select(Flight.date, func.count()).group_by(Flight.date)).all())


def test_past_days_are_recorded_by_the_real_tracker_and_only_once(tmp_path):
    runtime = make_demo_runtime(tmp_path)
    messages = []
    demo = Demo(runtime, pilots=6, history_days=2, say=messages.append)
    demo.prepare()
    today = datetime.now(runtime.regions[0].tz).date()
    by_date = flights_by_date(runtime)
    assert set(by_date) == {today - timedelta(days=2), today - timedelta(days=1)} and set(by_date.values()) == {6}
    with runtime.db.session() as s:
        flights = list(s.scalars(select(Flight)))
    assert all(f.status == "closed" and f.close_reason == "landed" and f.airborne and f.preview for f in flights)
    assert len({f.source for f in flights}) >= 2  # FLARM, FANET and OGN trackers
    assert any("invented altitudes" in m for m in messages) and sum("Recording" in m for m in messages) == 2
    demo.seed_history()  # again: nothing new
    assert flights_by_date(runtime) == by_date


def test_with_terrain_available_the_flights_follow_it(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=terrarium_png(lambda x, y: 1500.0)))
    runtime = make_demo_runtime(tmp_path, transport)
    messages = []
    demo = Demo(runtime, pilots=6, history_days=1, say=messages.append)
    demo.prepare()
    assert demo.ground is not None and any("follow the real mountains" in m for m in messages)
    with runtime.db.session() as s:
        flights = list(s.scalars(select(Flight)))
    assert len(flights) == 6
    for f in flights:  # planned as height above ground, so the finalizer sees real AGL
        assert f.ground_filled and f.airborne and 50 < f.max_agl < 1500 and 1500 < f.max_alt < 3500


def test_without_terrain_the_demo_still_starts_quickly(tmp_path):
    def offline(request):
        raise httpx.ConnectError("no network")

    runtime = make_demo_runtime(tmp_path, httpx.MockTransport(offline))
    started = time.monotonic()
    demo = Demo(runtime, pilots=6, history_days=0, say=lambda m: None)
    demo.prepare()
    assert demo.ground is None and runtime.terrain.enabled is False and time.monotonic() - started < 8


def test_a_hanging_tile_server_does_not_hold_the_demo_up(tmp_path):
    release = threading.Event()

    def hangs(request):
        release.wait(30)
        raise httpx.ConnectError("gave up")

    terrain = TerrainService(URL, 12, tmp_path / "dem", transport=httpx.MockTransport(hangs))
    started = time.monotonic()
    assert terrain_works(terrain, 46.8, 8.2, timeout=0.3) is False
    assert time.monotonic() - started < 2
    release.set()


def test_the_live_feed_catches_up_and_keeps_flying(tmp_path):
    runtime = make_demo_runtime(tmp_path)
    demo = Demo(runtime, speed=30.0, pilots=4, history_days=0, say=lambda m: None)
    demo.prepare()
    assert runtime.status()["link"]["state"] == "demo"
    runtime.start()
    demo.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            live = runtime.tracker.live()["aircraft"]
            if len([a for a in live if a["flying"]]) >= 3:
                break
            time.sleep(0.1)
        flying = [a for a in live if a["flying"]]
        assert len(flying) >= 3 and all(a["id"][:3] in {"FLR", "FNT", "OGN"} for a in live)
        assert not any(a["address"] in {"A00001", "A00002", "A00003", "3FF19F"} for a in live)  # the noise is filtered
        assert runtime.counters.get("drop.type") > 0 and runtime.counters.get("drop.stealth") > 0
    finally:
        demo.stop()
        runtime.stop()
    assert not demo._thread.is_alive()


def test_every_wave_has_its_own_pilots(tmp_path):
    demo = Demo(make_demo_runtime(tmp_path), pilots=5, history_days=0, say=lambda m: None)
    start = datetime(2026, 7, 15, 9, 0).astimezone()
    first, second = demo._new_wave(0, start), demo._new_wave(1, start)
    addresses = [
        {
            p.spec.address
            for p in w.pilots
            if p.spec.sender == "pilot" and p.spec.aircraft_type == 7 and not p.spec.stealth
        }
        for w in (first, second)
    ]
    assert (
        addresses[0].isdisjoint(addresses[1]) and len(addresses[0]) == 5
    )  # the stealth device is the same in every wave


@pytest.mark.parametrize("speed", [0.5, 61, 0])
def test_speed_must_be_sensible(tmp_path, speed):
    with pytest.raises(ValueError, match="speed"):
        Demo(make_demo_runtime(tmp_path), speed=speed)


def test_the_demo_database_is_its_own_file_and_can_be_removed(tmp_path):
    path = demo_database_path(tmp_path)
    assert path.name == "demo.db" and path.parent == tmp_path
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"demo.db{suffix}").write_text("x")
    (tmp_path / "prack.db").write_text("the real one")
    remove_demo_database(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prack.db"]
    remove_demo_database(tmp_path)  # and nothing to remove is fine
