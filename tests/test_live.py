"""The live view: what the browser is told about, second by second."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from prack.ogn.ddb import DeviceDatabase

from .rig import LAT0, LON0, M_PER_DEG_LON, T0, FlatTerrain, Rig, beacon, fly


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def airborne(rig, start=T0, seconds=60, address="112880", **kw):
    for i in range(seconds):
        rig.feed(
            beacon(start + i, LAT0, LON0 + 10.0 * i / M_PER_DEG_LON, 2000 - i, 36.0, address=address, climb=-1.0, **kw)
        )
    return start + seconds


def by_id(message):
    return {a["id"]: a for a in message["aircraft"]}


def test_a_new_aircraft_appears_in_the_first_message_and_then_only_when_it_changes(rig):
    first = rig.tracker.live()
    assert first["full"] is True and first["aircraft"] == [] and first["removed"] == []
    airborne(rig, seconds=5)
    msg = rig.tracker.live(since=first["seq"])
    assert msg["full"] is False and list(by_id(msg)) == ["FLR112880"]
    quiet = rig.tracker.live(since=msg["seq"])
    assert quiet["aircraft"] == [] and quiet["seq"] == msg["seq"]
    airborne(rig, start=T0 + 5, seconds=1, address="112880")
    assert list(by_id(rig.tracker.live(since=msg["seq"]))) == ["FLR112880"]


def test_the_message_has_the_fields_the_map_needs(tmp_path):
    rig = Rig(tmp_path, terrain=FlatTerrain(1500.0))
    airborne(rig, seconds=61)
    (a,) = rig.tracker.live()["aircraft"]
    assert a["id"] == "FLR112880" and a["address"] == "112880" and a["name"] == "FLR112880" and a["pilot"] is None
    assert (a["reg"], a["cn"], a["src"]) == (None, None, "FLARM")
    assert (a["flying"], a["takeoff"], a["flight_id"]) == (True, T0, 1)  # the first fast fix, not the buffer start
    assert (a["t"], a["lat"], a["alt"], a["gnd"], a["spd"], a["vs"], a["hdg"]) == (
        T0 + 60,
        LAT0,
        1940,
        1500,
        36.0,
        -1.0,
        90,
    )
    assert a["lon"] == pytest.approx(LON0 + 600 / M_PER_DEG_LON, abs=1e-6)


def test_every_position_is_pushed_even_when_not_every_one_is_stored(tmp_path):
    rig = Rig(tmp_path, min_fix_interval=10.0)
    cursor = rig.tracker.live()["seq"]
    seen = []
    for i in range(60):
        rig.feed(beacon(T0 + i, LAT0, LON0 + 10.0 * i / M_PER_DEG_LON, 2000 - i, 36.0))
        msg = rig.tracker.live(since=cursor)
        cursor = msg["seq"]
        seen += [p for a in msg["aircraft"] for p in a["pts"]]
    assert [p[0] for p in seen] == list(range(T0, T0 + 60))  # all 60, one per second, nothing missing or repeated
    t, lon, lat, alt, spd, vs, hdg, gnd = seen[7]
    assert (t, lat, alt, spd, vs, hdg, gnd) == (T0 + 7, LAT0, 1993, 36.0, 0.0, 90, None)
    rig.tracker.flush(force=True)
    assert rig.count(__import__("prack.models", fromlist=["Fix"]).Fix) < 20  # while the database got one per 10 s


def test_a_full_snapshot_carries_the_recent_trail_instead_of_pts(rig):
    airborne(rig, seconds=100)
    (a,) = rig.tracker.live()["aircraft"]
    assert "pts" not in a and 33 <= len(a["trail"]) <= 34  # every third point, the newest included
    assert [len(p) for p in a["trail"]] == [4] * len(a["trail"]) and a["trail"][0][0] == T0
    assert a["trail"][-1][0] == T0 + 99


def test_the_trail_is_limited_to_six_minutes(rig):
    airborne(rig, seconds=500)
    state = rig.tracker.states["FLR112880"]
    assert len(state.trail) == 360 and state.trail[0][1] == T0 + 140


def test_grounded_aircraft_are_shown_and_leave_after_ten_minutes(rig):
    for i in range(30):
        rig.feed(beacon(T0 + i, speed=0.0))
    (a,) = rig.tracker.live()["aircraft"]
    assert a["flying"] is False and a["flight_id"] is None and a["takeoff"] is None
    cursor = rig.tracker.live()["seq"]
    rig.tracker.sweep(datetime.fromtimestamp(T0 + 29 + 599, UTC))
    assert rig.tracker.live(since=cursor)["removed"] == []
    rig.tracker.sweep(datetime.fromtimestamp(T0 + 29 + 601, UTC))
    assert rig.tracker.live(since=cursor)["removed"] == ["FLR112880"]
    assert rig.tracker.live()["aircraft"] == [] and "FLR112880" in rig.tracker.states  # remembered for resuming


def test_it_comes_back_when_heard_again(rig):
    rig.feed(beacon(T0, speed=0.0))
    rig.tracker.sweep(datetime.fromtimestamp(T0 + 700, UTC))
    cursor = rig.tracker.live()["seq"]
    rig.feed(beacon(T0 + 701, speed=0.0), received=T0 + 701)
    assert list(by_id(rig.tracker.live(since=cursor))) == ["FLR112880"]


def test_aircraft_in_the_margin_of_the_feed_are_not_shown_but_a_recorded_flight_is(rig):
    rig.feed(beacon(T0, 48.5, 11.0, 2000, 0.0, address="AAAAAA"))  # Bavaria
    assert rig.tracker.live()["aircraft"] == []
    for i in range(80):  # taking off just inside the west border and flying out over France at 10 m/s
        rig.feed(beacon(T0 + i, LAT0, 5.856 - 10.0 * i / M_PER_DEG_LON, 2000, 36.0, track=270.0))
    state = rig.tracker.states["FLR112880"]
    assert state.last.lon < 5.85 and not state.in_region and state.flight is not None
    assert [a["id"] for a in rig.tracker.live()["aircraft"]] == ["FLR112880"]  # outside the box, but a recorded flight


def test_an_aircraft_that_walks_out_of_the_region_is_removed_from_the_map(rig):
    rig.feed(beacon(T0, LAT0, 5.851, 1500, 0.0))
    cursor = rig.tracker.live()["seq"]
    assert [a["id"] for a in rig.tracker.live()["aircraft"]] == ["FLR112880"]
    rig.feed(beacon(T0 + 1, LAT0, 5.849, 1500, 0.0))
    assert rig.tracker.live(since=cursor)["removed"] == ["FLR112880"]


def test_a_disqualified_device_disappears(rig):
    airborne(rig, seconds=30)
    cursor = rig.tracker.live()["seq"]
    for i in range(2):
        rig.feed(beacon(T0 + 40 + i, LAT0, LON0, 1500, 190.0))
    msg = rig.tracker.live(since=cursor)
    assert msg["removed"] == ["FLR112880"] and rig.tracker.live()["aircraft"] == []


def test_the_removal_list_is_only_for_clients_that_already_know_the_aircraft(rig):
    airborne(rig, seconds=5)
    rig.tracker.sweep(datetime.fromtimestamp(T0 + 5000, UTC))
    assert rig.tracker.live()["removed"] == []  # a full snapshot has no removals: it only lists what is there


def test_the_summary_counts(rig):
    airborne(rig, seconds=70)
    for i in range(5):
        rig.feed(beacon(T0 + i, speed=0.0, address="BBBBBB"))
    summary = rig.tracker.summary()
    assert (summary["devices"], summary["live"], summary["flying"]) == (2, 2, 1) and summary["pending_fixes"] > 0


# ---------------------------------------------------------------- names and identity from the device database


def ddb_with(entries):
    ddb = DeviceDatabase(
        None, "https://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"devices": entries}))
    )
    ddb.refresh()
    return ddb


DDB = [
    {"device_id": "AAAAAA", "registration": "HB-1234", "cn": "X1", "aircraft_model": "Ozone Rush", "identified": "Y"},
    {"device_id": "BBBBBB", "registration": "D-5678", "cn": "Y2", "aircraft_model": "Gin Boomerang", "identified": "N"},
    {"device_id": "CCCCCC", "registration": "OE-9", "identified": "Y"},
]


def test_names_follow_the_priority_pilot_then_competition_number_and_registration(tmp_path):
    rig = Rig(tmp_path, ddb=ddb_with(DDB))
    for address in ("AAAAAA", "BBBBBB", "CCCCCC", "DDDDDD"):
        rig.feed(beacon(T0, address=address, speed=0.0))
    names = {a["address"]: a["name"] for a in rig.tracker.live()["aircraft"]}
    assert names == {"AAAAAA": "X1 HB-1234", "BBBBBB": "FLRBBBBBB", "CCCCCC": "OE-9", "DDDDDD": "FLRDDDDDD"}
    rig.tracker.process_status(
        __import__("prack.ogn.parser", fromlist=["Status"]).Status(
            "FNTAAAAAA", "OGNFNT", "FANET", "AAAAAA", None, "Mia"
        )
    )
    assert {a["address"]: a["name"] for a in rig.tracker.live(since=0)["aircraft"]}["AAAAAA"] == "Mia"


def test_registration_and_competition_number_are_stored_only_for_identified_devices(tmp_path):
    rig = Rig(tmp_path, ddb=ddb_with(DDB))
    for address in ("AAAAAA", "BBBBBB"):
        rig.feed(beacon(T0, address=address, speed=0.0))
    stored = {d.address: (d.registration, d.competition_id, d.model) for d in rig.devices()}
    assert stored == {"AAAAAA": ("HB-1234", "X1", "Ozone Rush"), "BBBBBB": (None, None, "Gin Boomerang")}
    live = {a["address"]: (a["reg"], a["cn"]) for a in rig.tracker.live()["aircraft"]}
    assert live == {"AAAAAA": ("HB-1234", "X1"), "BBBBBB": (None, None)}


def test_a_long_flight_is_recorded_through_the_live_view_too(rig):
    fly(rig, T0)
    assert [a["flying"] for a in rig.tracker.live()["aircraft"]] == [False]  # landed by now
