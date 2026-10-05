"""The whole chain, no mocks: simulator -> fake OGN server (TCP) -> client -> queue -> filters -> tracker -> SQL."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from prack.config import Settings
from prack.models import Device, Fix, Flight
from prack.ogn.fake_server import FakeOgnServer
from prack.ogn.simulator import PilotSpec, Simulator
from prack.runtime import Runtime

from .test_client_ingest import wait_for

SHORT = {"flight_s": 120, "drop_m": 300.0, "stand_s": 300}  # about 12 minutes from first fix to the end


def short_scenario(start: datetime) -> Simulator:
    launch = (46.6453, 7.6511)
    pilots = [
        PilotSpec("D00001", ("flarm",), launch=launch, **SHORT),
        PilotSpec("D00002", ("flarm", "fanet"), name="Mia", launch=(46.70, 7.80), **SHORT),
        PilotSpec("D00003", ("fanet",), name="Jonas", launch=(46.55, 8.20), **SHORT),
        PilotSpec("A00001", aircraft_type=1, launch=launch, **SHORT),  # a glider
        PilotSpec("A00002", aircraft_type=6, launch=launch, **SHORT),  # a hang glider
        PilotSpec("A00003", stealth=True, launch=launch, **SHORT),  # does not want to be tracked
        PilotSpec("3FF19F", sender="adsb"),  # an ADS-B target that says paraglider, at 300 km/h
    ]
    return Simulator(start, pilots, seed=11)


def settings_for(tmp_path, port: int | None = None) -> Settings:
    s = Settings(data_dir=tmp_path / "data", ddb_enabled=False, terrain_enabled=False)
    if port is not None:
        s.ogn_host, s.ogn_port = "127.0.0.1", port
    return s


def all_lines(sim: Simulator) -> list[str]:
    return [line for k in range(sim.duration_s() + 1) for line in sim.lines(sim.at(k))]


def test_a_feed_over_tcp_becomes_flights_in_the_database(tmp_path):
    sim = short_scenario(datetime.now(UTC) - timedelta(seconds=sim_span()))
    lines = all_lines(sim)
    with FakeOgnServer(lines, per_tick=150, interval=0.005) as server:
        runtime = Runtime(settings_for(tmp_path, server.port))
        runtime.start()
        wait_for(lambda: runtime.counters.get("lines") >= len(lines) and runtime.ingest.queue.empty(), timeout=60)
        runtime.tracker.sweep(datetime.now(UTC))
        runtime.stop()
    runtime.finalizer.drain()
    db = runtime.db

    with db.session() as s:
        flights = s.execute(
            select(Flight, Device).join(Device, Device.id == Flight.device_id).order_by(Device.callsign)
        ).all()
    assert [d.callsign for _, d in flights] == ["FLRD00001", "FLRD00002", "FNTD00003"]  # the noise left no trace
    for flight, _ in flights:
        assert (flight.status, flight.close_reason, flight.airborne) == ("closed", "landed", True)
        assert flight.preview and flight.max_alt > flight.min_alt + 250 and flight.distance_km > 2
    names = {d.callsign: d.pilot_name for _, d in flights}
    assert names == {"FLRD00001": None, "FLRD00002": "Mia", "FNTD00003": "Jonas"}

    drops = runtime.status()["drops"]
    assert drops["type"] > 0 and drops["stealth"] > 0 and drops["lower_source"] > 0
    groups = {(g["source"], g["aircraft_type"], g["reported_type"]) for g in runtime.status()["sources"]["groups"]}
    assert ("ADS-B", 0, 7) in groups and ("FLARM", 1, 1) in groups  # what arrived is visible, with the reason
    assert runtime.counters.get("ingest.queue_dropped") == 0 and runtime.counters.get("ingest.consumer_errors") == 0

    # the dual-protocol pilot: one device, one flight, one fix per second, FLARM only
    with db.session() as s:
        flight_id = s.scalar(select(Flight.id).join(Device).where(Device.callsign == "FLRD00002"))
        stamps = list(s.scalars(select(Fix.ts).where(Fix.flight_id == flight_id).order_by(Fix.ts)))
        fanet_fixes = s.scalar(
            text("SELECT count(*) FROM fixes_v WHERE flight_id = :f AND source = 'FANET'"), {"f": flight_id}
        )
    assert stamps == list(range(stamps[0], stamps[-1] + 1)) and fanet_fixes == 0

    # and the views show real units
    with db.session() as s:
        row = (
            s.execute(
                text("SELECT * FROM fixes_v WHERE flight_id = :f ORDER BY ts LIMIT 1 OFFSET 200"), {"f": flight_id}
            )
            .mappings()
            .one()
        )
    assert (
        1500 < row["alt_m"] < 2600
        and 20 < row["speed_kmh"] < 45
        and row["pilot_name"] == "Mia"
        and row["source"] == "FLARM"
    )
    runtime.db.dispose()


def sim_span() -> int:
    """Seconds from the first to the last simulated packet of the short scenario (it must end at 'now')."""
    return short_scenario(datetime(2026, 1, 1, tzinfo=UTC)).duration_s()


def test_a_restart_in_the_middle_of_a_flight_continues_the_same_flight(tmp_path):
    sim = short_scenario(datetime.now(UTC) - timedelta(seconds=sim_span()))
    cut = 200  # the program stops 200 s into the flight and starts again
    first = Runtime(settings_for(tmp_path), feed=False)
    first.prepare()
    for k in range(0, 60 + 12 + cut):
        now = sim.at(k)
        for line in sim.lines(now):
            first.consume(now, line)
    first.tracker.flush(force=True)
    assert first.tracker.summary()["flying"] == 3
    first.db.dispose()

    second = Runtime(settings_for(tmp_path), feed=False)
    repairs = second.prepare()
    assert repairs["reopened"] == 3  # flights left open are closed as gaps ...
    for k in range(60 + 12 + cut + 30, sim.duration_s() + 1):  # (30 s of silence while restarting)
        now = sim.at(k)
        for line in sim.lines(now):
            second.consume(now, line)
    second.tracker.sweep(sim.at(sim.duration_s() + 1))
    second.finalizer.drain()
    with second.db.session() as s:
        flights = list(s.scalars(select(Flight)))
    assert len(flights) == 3 and all(f.close_reason == "landed" for f in flights)  # ... and resumed: still 3 flights
    assert second.counters.get("flights.resumed") == 3
    second.db.dispose()


def test_status_has_everything_the_status_page_needs(tmp_path):
    runtime = Runtime(settings_for(tmp_path), feed=False)
    runtime.prepare()
    st = runtime.status()
    assert set(st) >= {
        "version",
        "link",
        "counters",
        "drops",
        "sources",
        "tracker",
        "ddb",
        "queue",
        "finalizer",
        "database",
    }
    assert st["link"]["state"] == "off" and st["ddb"] is None and st["database"]["size_bytes"] > 0
    assert st["database"]["url"].startswith("sqlite:///") and st["tracker"]["devices"] == 0


def test_the_database_comes_back_after_an_outage_without_losing_fixes(tmp_path, monkeypatch):
    runtime = Runtime(settings_for(tmp_path), feed=False)
    runtime.prepare()
    sim = short_scenario(datetime.now(UTC) - timedelta(seconds=sim_span()))
    for k in range(0, 100):
        for line in sim.lines(sim.at(k)):
            runtime.consume(sim.at(k), line)
    queued = len(runtime.tracker.pending)
    assert queued > 0
    real = runtime.db.session

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.db, "session", broken)
    with pytest.raises(OSError):
        runtime.tracker.flush(force=True)
    assert len(runtime.tracker.pending) == queued  # still queued, not lost
    monkeypatch.setattr(runtime.db, "session", real)
    runtime.tracker.flush(force=True)
    assert runtime.tracker.pending == [] and runtime.counters.get("fixes.stored") == queued
    with runtime.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Fix)) == queued


def test_the_start_up_repairs_run(tmp_path):
    from .factory import make_flight

    runtime = Runtime(settings_for(tmp_path), feed=False)
    runtime.db.init()
    now = int(time.time())
    make_flight(runtime.db, address="AAAAAA", source="FLARM", start=now - 3600, seconds=300)
    make_flight(runtime.db, address="AAAAAA", source="FANET", start=now - 3600, seconds=300, step=4)  # a duplicate
    make_flight(runtime.db, address="BBBBBB", start=now - 3000, seconds=300, spikes=3)  # not a paraglider
    make_flight(runtime.db, address="CCCCCC", start=now - 2000, seconds=300, status="active", close_reason=None)
    repairs = runtime.prepare()
    assert repairs == {"purged": 1, "merged": 1, "reopened": 1, "unfinished": repairs["unfinished"]}
    assert repairs["unfinished"] >= 2
    runtime.finalizer.drain()
    with runtime.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Flight)) == 2
        assert s.scalar(select(func.count()).select_from(Flight).where(Flight.preview.is_(None))) == 0
    runtime.db.dispose()
