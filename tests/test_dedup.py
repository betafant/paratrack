"""One pilot on several protocols is one aircraft: one device, one flight, one track."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from prack.models import Device, Flight
from prack.ogn.builder import build_position, build_status

from .dual import FANET, FLARM, at, fanet, flarm, position
from .rig import LAT0, LON0, M_PER_DEG_LAT, M_PER_DEG_LON, T0, Rig, beacon


def sources(rig: Rig, flight_id: int | None = None) -> list[tuple[int, int]]:
    return [(x.ts, x.src) for x in rig.fixes(flight_id)]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- the scenario of the brief


def test_flarm_every_second_plus_fanet_every_four_is_one_aircraft(rig):
    for t in range(T0, T0 + 1000):
        flarm(rig, t)  # FLARM first within a second: it is heard before its FANET twin
        if t % 4 == 0:
            fanet(rig, t)
        if t == T0 + 20:
            rig.line(build_status(at(t), "Mia", address="112880"), t)
    live = rig.tracker.live()
    assert [(a["id"], a["name"], a["pilot"], a["address"]) for a in live["aircraft"]] == [
        ("FLR112880", "Mia", "Mia", "112880")
    ]  # exactly one aircraft
    rig.finish()
    (device,) = rig.devices()
    assert (device.callsign, device.pilot_name, device.source) == ("FLR112880", "Mia", "FLARM")
    (f,) = rig.flights()
    stamps = [x.ts for x in rig.fixes()]
    assert len(stamps) == len(set(stamps)) and stamps == list(range(stamps[0], stamps[-1] + 1))  # one fix per second
    assert {src for _, src in sources(rig)} == {FLARM}  # FANET never stored: FLARM was always heard
    assert rig.counters.get("drop.lower_source") == 250  # every FANET position, none stored
    assert f.fix_count == len(stamps) == 999  # from 60 s before take-off (T0+1) to the last position


def test_the_pilot_name_arrives_before_the_aircraft_is_tracked(rig):
    rig.line(build_status(at(T0), "Mia", address="112880"), T0)  # a name for a device we have not seen yet
    assert rig.devices() == []  # nothing is stored for aircraft that are not tracked
    for t in range(T0, T0 + 100):
        fanet(rig, t) if t % 4 == 0 else None
    assert rig.devices()[0].pilot_name == "Mia"
    flarm(rig, T0 + 100)
    assert [d.pilot_name for d in rig.devices()] == ["Mia"]  # and it survives the switch to FLARM


def test_the_name_of_a_stealth_device_is_never_stored(rig):
    rig.line(build_status(at(T0), "Secret", address="999999"), T0)
    for t in range(T0, T0 + 60):
        rig.line(build_position(at(t), LAT0, LON0, 2000, address="999999", stealth=True), t)
    assert rig.devices() == [] and rig.counters.get("drop.stealth") == 60


# ---------------------------------------------------------------- the better protocol takes over


def test_fanet_first_then_flarm_switches_identity_in_place(rig):
    for t in range(T0, T0 + 200):
        if t % 4 == 0:
            fanet(rig, t)
        if t >= T0 + 100:
            flarm(rig, t)
    rig.finish()
    (device,) = rig.devices()  # one row, renamed - not a second one
    assert (device.callsign, device.source) == ("FLR112880", "FLARM")
    (f,) = rig.flights()
    assert f.source == "FLARM"
    by_src = sources(rig, f.id)
    before = [t for t, s in by_src if s == FANET]
    assert before and max(before) <= T0 + 100 and all(b - a == 4 for a, b in zip(before, before[1:], strict=False))
    # at T0+100 both arrive in the same second; the FANET fix came first and the equal time stamp is a duplicate
    assert [t for t, s in by_src if s == FLARM] == list(range(T0 + 101, T0 + 200))
    assert rig.counters.get("identity.switched") == 1


def test_the_old_callsign_is_announced_as_gone_so_the_browser_can_follow(rig):
    fanet(rig, T0)
    first = rig.tracker.live()
    assert [a["id"] for a in first["aircraft"]] == ["FNT112880"]
    flarm(rig, T0 + 1)
    update = rig.tracker.live(since=first["seq"])
    assert update["removed"] == ["FNT112880"] and [a["id"] for a in update["aircraft"]] == ["FLR112880"]
    assert update["aircraft"][0]["address"] == "112880"  # the address is what the selection follows


def test_fanet_only_fills_the_gaps_when_flarm_is_silent(rig):
    for t in range(T0, T0 + 600):
        silent = T0 + 200 <= t < T0 + 320  # FLARM drops out for two minutes
        if not silent:
            flarm(rig, t)
        if t % 4 == 0:
            fanet(rig, t)
    rig.finish()
    (f,) = rig.flights()
    by_src = sources(rig, f.id)
    fanet_times = [t for t, s in by_src if s == FANET]
    assert fanet_times and min(fanet_times) > T0 + 199 + 30 and max(fanet_times) < T0 + 320  # only inside the hole
    assert all(b - a == 4 for a, b in zip(fanet_times, fanet_times[1:], strict=False))
    assert [t for t, s in by_src if s == FLARM] == [t for t in range(T0 + 1, T0 + 600) if not T0 + 200 <= t < T0 + 320]


def test_lower_ranked_protocols_are_ranked_flarm_fanet_ogn_adsl(tmp_path):
    rig = Rig(tmp_path)
    for t in range(T0, T0 + 120):
        rig.feed(beacon(t, LAT0, LON0 + 5.0 * (t - T0) / M_PER_DEG_LON, 2000, 36.0, source="OGN tracker (ADS-L)"))
        if t % 2 == 0:
            rig.feed(beacon(t, LAT0, LON0 + 5.0 * (t - T0) / M_PER_DEG_LON, 2000, 36.0, source="OGN tracker"))
        if t % 4 == 0:
            rig.feed(beacon(t, LAT0, LON0 + 5.0 * (t - T0) / M_PER_DEG_LON, 2000, 36.0, source="FANET"))
    rig.finish()
    (device,) = rig.devices()
    assert device.source == "FANET"  # the best one that was heard; FLARM never was
    (f,) = rig.flights()
    assert f.fix_count < 120  # the 1 Hz ADS-L stream was ignored while FANET (every 4 s) was heard


def test_the_same_address_far_away_is_another_aircraft(rig):
    for t in range(T0, T0 + 300):
        flarm(rig, t)
        lat, lon, alt, speed = position(t)
        rig.feed(beacon(t, lat + 0.2, lon, alt, speed, source="OGN tracker"))  # same address, 22 km north
    rig.finish()
    assert len(rig.devices()) == 2 and len(rig.flights()) == 2
    assert rig.counters.get("identity.routed") == 0 and rig.counters.get("drop.lower_source") == 0


def test_the_same_address_ten_minutes_later_is_another_aircraft(rig):
    flarm(rig, T0)
    rig.feed(beacon(T0 + 601, LAT0, LON0, 2300.0, 0.0, source="FANET"))  # close by, but 10 minutes and 1 s later
    assert len(rig.tracker.states) == 2


# ---------------------------------------------------------------- two flights of one pilot


def test_two_flights_that_turn_out_to_be_one_are_merged_while_running(rig):
    for t in range(T0, T0 + 240):  # FLARM and an OGN tracker of the same address, 5 km apart: two aircraft
        lat, lon, alt, speed = position(t)
        rig.feed(beacon(t, lat, lon, alt, speed, source="FLARM"))
        rig.feed(beacon(t, lat + 5000 / M_PER_DEG_LAT, lon, alt, speed, source="OGN tracker"))
    assert len(rig.tracker.states) == 2
    rig.tracker.flush(force=True)
    assert rig.count(Flight) == 2
    for t in range(T0 + 240, T0 + 400):  # the tracker's position now agrees with FLARM
        lat, lon, alt, speed = position(t)
        rig.feed(beacon(t, lat, lon, alt, speed, source="FLARM"))
        rig.feed(beacon(t, lat + 100 / M_PER_DEG_LAT, lon, alt, speed, source="OGN tracker"))
    rig.finish()
    (f,) = rig.flights()
    (device,) = rig.devices()
    assert (device.callsign, f.source) == ("FLR112880", "FLARM")
    assert rig.counters.get("identity.merged") == 1
    assert {s for _, s in sources(rig)} == {FLARM}  # the tracker's fixes: none, FLARM had no gap longer than 30 s
    stamps = [x.ts for x in rig.fixes()]
    assert stamps == sorted(set(stamps))


def test_a_restart_keeps_the_identity_match(rig):
    for t in range(T0, T0 + 300):
        flarm(rig, t)
        if t % 4 == 0:
            fanet(rig, t)
    (flight_id,) = rig.restart()
    assert list(rig.tracker.states) == ["FLR112880"]
    for t in range(T0 + 330, T0 + 450):  # after the restart FLARM is silent for a while: FANET carries on
        if t % 4 == 0:
            fanet(rig, t)
    rig.finish()
    (f,) = rig.flights()
    assert f.id == flight_id and ts_end(f) >= T0 + 440
    assert len(rig.devices()) == 1 and rig.devices()[0].callsign == "FLR112880"
    assert rig.counters.get("flights.resumed") == 1 and rig.counters.get("identity.routed") >= 1


def ts_end(flight: Flight) -> int:
    return int(flight.end_time.timestamp())


def test_a_pilot_name_survives_a_restart_and_a_protocol_change(rig):
    for t in range(T0, T0 + 120):
        fanet(rig, t) if t % 4 == 0 else None
        if t == T0 + 8:
            rig.line(build_status(at(t), "Mia", address="112880"), t)
    rig.restart()
    for t in range(T0 + 130, T0 + 200):
        flarm(rig, t)
    rig.finish()
    assert [(d.callsign, d.pilot_name) for d in rig.devices()] == [("FLR112880", "Mia")]


def test_yesterdays_device_row_is_reused_when_the_better_protocol_returns(rig):
    for t in range(T0, T0 + 200):
        flarm(rig, t, address="AAAAAA")
    rig.finish()
    assert [d.callsign for d in rig.devices()] == ["FLRAAAAAA"]
    rig.tracker = rig.make_tracker()  # next day: this time the pilot is only heard on FANET first
    day2 = T0 + 86400
    for t in range(day2, day2 + 120):
        if t % 4 == 0:
            lat, lon, alt, speed = position(t - 86400)
            rig.feed(beacon(t, lat, lon, alt, speed, address="AAAAAA", source="FANET"))
    for t in range(day2 + 120, day2 + 200):
        lat, lon, alt, speed = position(t - 86400)
        rig.feed(beacon(t, lat, lon, alt, speed, address="AAAAAA", source="FLARM"))
    rig.finish()
    assert [d.callsign for d in rig.devices()] == ["FLRAAAAAA"]  # the FANET-named row was folded into it
    with rig.db.session() as s:
        assert {fl.device_id for fl in s.scalars(select(Flight))} == {rig.devices()[0].id}
        assert s.scalar(select(Device.id).where(Device.callsign == "FNTAAAAAA")) is None
