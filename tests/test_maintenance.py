"""Repairs: merging a pilot's flights recorded twice, purging impossible ones."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from prack.models import Device, Fix, Flight
from prack.tracking.maintenance import delete_flight, merge_duplicate_flights, merge_flights, purge_implausible

from .dual import FANET, FLARM, fanet, flarm
from .factory import make_flight
from .rig import T0, Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def record_twice(rig: Rig, *, flarm_hole: tuple[int, int] | None = None, seconds: int = 600) -> None:
    """Two flights of one pilot, as an older version or two restarts would leave them: the FANET flight and, from
    a tracker that knew nothing about it, the FLARM flight."""
    for t in range(T0, T0 + seconds):
        if t % 4 == 0:
            fanet(rig, t)
    rig.finish()
    rig.tracker = rig.make_tracker()
    for t in range(T0, T0 + seconds):
        if flarm_hole is None or not flarm_hole[0] <= t < flarm_hole[1]:
            flarm(rig, t)
    rig.finish()


# ---------------------------------------------------------------- merging flights


def test_duplicate_flights_of_one_address_are_merged_into_the_preferred_protocol(rig):
    record_twice(rig)
    assert len(rig.flights()) == 2 and [d.callsign for d in rig.devices()] == ["FNT112880", "FLR112880"]
    kept = []
    assert merge_duplicate_flights(rig.db, on_merged=kept.append) == 1
    (flight,) = rig.flights()
    assert flight.source == "FLARM" and kept == [flight.id]
    assert [d.callsign for d in rig.devices()] == ["FLR112880"]  # the FANET device had nothing left
    assert {x.src for x in rig.fixes()} == {FLARM}  # FLARM had no gap, so nothing was copied from FANET
    rig.finalizer.finalize(flight.id)
    assert merge_duplicate_flights(rig.db) == 0  # and it is done for good


def test_the_dropped_flight_fills_only_real_holes_of_the_kept_one(rig):
    record_twice(rig, flarm_hole=(T0 + 200, T0 + 320))
    merge_duplicate_flights(rig.db)
    (flight,) = rig.flights()
    copied = [x.ts for x in rig.fixes() if x.src == FANET]
    # FLARM was last heard at T0+199 and again at T0+320: only FANET fixes more than 30 s from both are copied
    assert copied == list(range(T0 + 232, T0 + 289, 4))
    assert flight.fix_count == len(rig.fixes())
    stamps = [x.ts for x in rig.fixes()]
    assert stamps == sorted(set(stamps))


def test_merge_flights_keeps_the_start_and_end_of_both(tmp_path):
    rig = Rig(tmp_path)
    keep = make_flight(rig.db, source="FLARM", start=T0 + 100, seconds=200)
    drop = make_flight(rig.db, source="FANET", start=T0, seconds=500, step=4)
    copied = merge_flights(rig.db, keep, drop)
    with rig.db.session() as s:
        flight = s.get(Flight, keep)
        assert (int(flight.start_time.timestamp()), int(flight.end_time.timestamp())) == (T0, T0 + 500)
        assert flight.fix_count == 201 + copied and s.get(Flight, drop) is None
        assert s.scalar(select(func.count()).select_from(Fix).where(Fix.flight_id == drop)) == 0
    assert copied == len([t for t in range(0, 501, 4) if t < 70 or t > 330])  # FLARM covers T0+100..T0+300 (+-30 s)
    assert merge_flights(rig.db, keep, drop) == 0 and merge_flights(rig.db, keep, keep) == 0  # gone or the same


def test_three_protocols_end_up_as_one_flight(tmp_path):
    rig = Rig(tmp_path)
    make_flight(rig.db, source="OGN tracker (ADS-L)", seconds=400)
    make_flight(rig.db, source="FANET", seconds=400, step=4)
    flarm_id = make_flight(rig.db, source="FLARM", seconds=400)
    assert merge_duplicate_flights(rig.db) == 2
    assert [f.id for f in rig.flights()] == [flarm_id] and [d.callsign for d in rig.devices()] == ["FLR112880"]


@pytest.mark.parametrize(
    ("second", "merged"),
    [
        ({"address": "112880", "start": T0 + 100}, True),  # overlapping, same place
        (
            {"address": "112880", "start": T0 + 330},
            True,
        ),  # starts a minute after the first one ends: still the same flight
        ({"address": "112880", "start": T0 + 3600}, False),  # an hour later: the next flight of the day
        ({"address": "112880", "start": T0 + 100, "lat": 46.9}, False),  # same time, 30 km north
        ({"address": "AAAAAA", "start": T0 + 100}, False),  # another pilot
    ],
)
def test_only_flights_of_the_same_address_in_the_same_place_and_time_merge(tmp_path, second, merged):
    rig = Rig(tmp_path)
    make_flight(rig.db, source="FLARM", seconds=270)
    make_flight(rig.db, source="FANET", seconds=200, step=4, **second)
    assert merge_duplicate_flights(rig.db) == (1 if merged else 0)


def test_start_up_repairs_only_recent_flights(tmp_path):
    rig = Rig(tmp_path)
    make_flight(rig.db, source="FLARM", start=T0, seconds=300)
    make_flight(rig.db, source="FANET", start=T0, seconds=300, step=4)
    assert merge_duplicate_flights(rig.db, since=datetime.fromtimestamp(T0 + 86400, UTC)) == 0
    assert merge_duplicate_flights(rig.db, since=datetime.fromtimestamp(T0 - 3600, UTC)) == 1


def test_deleting_a_flight_keeps_a_device_that_has_other_flights(tmp_path):
    rig = Rig(tmp_path)
    first = make_flight(rig.db, start=T0)
    second = make_flight(rig.db, start=T0 + 5000)
    with rig.db.session() as s:
        delete_flight(s, first)
    assert [f.id for f in rig.flights()] == [second] and len(rig.devices()) == 1
    with rig.db.session() as s:
        delete_flight(s, second)
    assert rig.flights() == [] and rig.devices() == [] and rig.count(Fix) == 0


# ---------------------------------------------------------------- purging impossible flights


def test_flights_of_paragliders_that_fly_like_aeroplanes_are_purged(tmp_path):
    rig = Rig(tmp_path)
    keep_spike = make_flight(rig.db, address="AAAAAA", spikes=1)  # one bad fix: a glitch, not a bad device
    make_flight(rig.db, address="BBBBBB", spikes=2)  # two fast fixes: not a paraglider
    make_flight(rig.db, address="CCCCCC", spikes=5, spike_speed=129.0)  # fast but within the limit
    make_flight(rig.db, address="DDDDDD", aircraft_type=6, spikes=3, spike_speed=150.0)  # a hang glider can do that
    make_flight(rig.db, address="EEEEEE", aircraft_type=6, spikes=2, spike_speed=200.0)  # but not that
    assert purge_implausible(rig.db) == 2
    assert {d.address for d in rig.devices()} == {"AAAAAA", "CCCCCC", "DDDDDD"}
    assert keep_spike in {f.id for f in rig.flights()}


def test_anything_recorded_over_adsb_is_purged(tmp_path):
    rig = Rig(tmp_path)
    make_flight(rig.db, address="AAAAAA", source="ADS-B")
    ok = make_flight(rig.db, address="BBBBBB")
    assert purge_implausible(rig.db) == 1 and [f.id for f in rig.flights()] == [ok]


def test_purging_can_be_limited_to_recent_flights(tmp_path):
    rig = Rig(tmp_path)
    make_flight(rig.db, address="AAAAAA", spikes=3, start=T0)
    assert purge_implausible(rig.db, since=datetime.fromtimestamp(T0 + 86400, UTC)) == 0
    assert purge_implausible(rig.db) == 1


def test_the_device_count_is_unchanged_by_clean_data(tmp_path):
    rig = Rig(tmp_path)
    make_flight(rig.db)
    assert purge_implausible(rig.db) == 0 and merge_duplicate_flights(rig.db) == 0
    assert rig.count(Device) == 1 and rig.count(Flight) == 1
