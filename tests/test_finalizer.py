from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import update

from prack.models import Fix, Flight
from prack.tracking.finalizer import Finalizer

from .factory import make_flight
from .rig import T0, FlatTerrain, Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_a_closed_flight_gets_statistics_ground_and_a_preview(tmp_path):
    rig = Rig(tmp_path, terrain=FlatTerrain(1000.0))
    fid = make_flight(rig.db, seconds=600)
    rig.finalizer.finalize(fid)
    (f,) = rig.flights()
    assert f.fix_count == 601 and f.ground_filled and f.airborne and f.max_agl == pytest.approx(1000.0, abs=0.5)
    assert f.distance_km == pytest.approx(6.0, abs=0.05) and f.preview and rig.counters.get("finalizer.done") == 1
    assert f.landing_time.timestamp() == T0 + 600  # a landed flight keeps the landing time the tracker recorded


def test_a_gap_flight_lands_at_its_last_fix(rig):
    fid = make_flight(rig.db, seconds=300, close_reason="gap")
    rig.finalizer.finalize(fid)
    assert rig.flights()[0].landing_time.timestamp() == T0 + 300


def test_finalizing_twice_changes_nothing(rig):
    fid = make_flight(rig.db, seconds=300)
    rig.finalizer.finalize(fid)
    first = rig.flights()[0]
    snapshot = {c.name: getattr(first, c.name) for c in Flight.__table__.columns if c.name != "updated_at"}
    rig.finalizer.finalize(fid)
    again = rig.flights()[0]
    assert snapshot == {c.name: getattr(again, c.name) for c in Flight.__table__.columns if c.name != "updated_at"}


def test_a_flight_that_resumed_meanwhile_is_left_alone(rig):
    fid = make_flight(rig.db, seconds=300)
    original = rig.fill_ground = rig.finalizer.fill_ground

    def resume_during_the_work(flight_id: int) -> int:
        with rig.db.session() as s:  # the tracker reopens the flight while the finalizer is busy
            s.execute(update(Flight).where(Flight.id == flight_id).values(status="active"))
        return original(flight_id)

    rig.finalizer.fill_ground = resume_during_the_work
    rig.finalizer.finalize(fid)
    f = rig.flights()[0]
    assert f.status == "active" and f.preview is None and rig.counters.get("finalizer.skipped") == 1


def test_open_flights_are_not_finalized(rig):
    fid = make_flight(rig.db, status="active", close_reason=None)
    rig.finalizer.finalize(fid)
    assert rig.flights()[0].preview is None and rig.counters.get("finalizer.done") == 0
    rig.finalizer.finalize(99999)  # unknown ids are ignored too


def test_a_closed_flight_without_fixes_is_removed(rig):
    fid = make_flight(rig.db, seconds=10)
    with rig.db.session() as s:
        s.execute(Fix.__table__.delete())
    rig.finalizer.finalize(fid)
    assert rig.flights() == [] and rig.devices() == []


def test_unfinished_flights_are_found_after_a_crash(rig):
    unfinished = make_flight(rig.db, address="AAAAAA", seconds=100)
    make_flight(rig.db, address="BBBBBB", seconds=100, finalized=True)
    make_flight(rig.db, address="CCCCCC", seconds=100, status="active", close_reason=None)
    assert rig.finalizer.enqueue_unfinished() == 1
    assert rig.finalizer.drain() == 1 and rig.flights()[0].id == unfinished and rig.flights()[0].preview


def test_one_failing_flight_does_not_stop_the_others(tmp_path, caplog):
    class Broken(FlatTerrain):
        def heights(self, points):
            raise RuntimeError("tile server on fire")

    rig = Rig(tmp_path, terrain=Broken())
    first, second = (
        make_flight(rig.db, address="AAAAAA", seconds=100),
        make_flight(rig.db, address="BBBBBB", seconds=100),
    )
    rig.finalizer.enqueue(first)
    rig.finalizer.enqueue(second)
    assert rig.finalizer.drain() == 2
    assert rig.counters.get("finalizer.errors") == 2 and "Finalizing flight" in caplog.text


def test_the_worker_thread_finishes_the_queue_before_it_stops(rig):
    ids = [make_flight(rig.db, address=f"{i:06X}", seconds=100) for i in range(5)]
    stop = threading.Event()
    thread = threading.Thread(target=rig.finalizer.run, args=(stop,))
    thread.start()
    for fid in ids:
        rig.finalizer.enqueue(fid)
    stop.set()
    thread.join(10)
    assert not thread.is_alive() and all(f.preview for f in rig.flights())


def test_missing_terrain_is_reported_as_not_filled(rig):
    fid = make_flight(rig.db, seconds=100)
    assert Finalizer(rig.db, None).fill_ground(fid) == -1
    rig.finalizer.finalize(fid)
    f = rig.flights()[0]
    assert f.ground_filled is False and f.max_agl is None
    time.sleep(0)  # (nothing to wait for: the lookup never leaves the process)


def test_a_flight_waiting_in_the_queue_is_not_queued_again(rig):
    fid = make_flight(rig.db, seconds=100)
    for _ in range(5):
        rig.finalizer.enqueue(fid)
    assert rig.finalizer.queue.qsize() == 1 and rig.finalizer.drain() == 1
    rig.finalizer.enqueue(fid)  # once it has been taken, it can be requested again (the flight may have grown)
    assert rig.finalizer.queue.qsize() == 1
