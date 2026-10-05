"""Flight detection: take-off, landing, gaps, restarts, region rule, sanity filters."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from prack.models import Device, Fix, Flight

from .rig import LAT0, LON0, M_PER_DEG_LON, T0, FlatTerrain, Rig, beacon, fly


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def ts_of(dt: datetime) -> int:
    return int(dt.timestamp())


# ---------------------------------------------------------------- take-off and landing


def test_a_flight_from_launch_run_to_landing(rig):
    fly(rig, T0)  # 90 s standing, 900 s flying at 36 km/h, 400 s standing in the landing field
    rig.finish()
    (f,) = rig.flights()
    assert (f.status, f.close_reason, f.region, f.source) == ("closed", "landed", "ch", "FLARM")
    assert ts_of(f.takeoff_time) == T0 + 90  # the first fast fix
    assert ts_of(f.start_time) == T0 + 31  # the pre-take-off buffer: 60 s before the second fast fix
    assert ts_of(f.landing_time) == T0 + 990  # when standing still began, not when it was detected
    assert ts_of(f.end_time) == T0 + 990 + 240  # four minutes later
    assert f.airborne and f.fix_count == 1200
    assert f.distance_km == pytest.approx(9.0, abs=0.05) and f.max_alt == 2300.0 and f.min_alt == 1400.0
    assert f.takeoff_lat == pytest.approx(LAT0, abs=1e-4) and f.takeoff_lon == pytest.approx(LON0, abs=1e-4)
    assert f.straight_km == pytest.approx(9.0, abs=0.05)
    assert f.preview and f.preview[0][:2] == pytest.approx([LON0, LAT0], abs=1e-4)
    assert rig.counters.get("flights.opened") == 1 and rig.counters.get("flights.closed") == 1


def test_every_second_is_stored_exactly_once(rig):
    fly(rig, T0)
    rig.finish()
    assert [x.ts for x in rig.fixes()] == list(range(T0 + 31, T0 + 1231))


def test_duplicate_positions_are_not_stored_twice(rig):
    for t in range(T0, T0 + 200):
        b = beacon(t, LAT0, LON0 + 10.0 * t / M_PER_DEG_LON, 2000, 36.0)
        rig.feed(b)
        rig.feed(b)  # the same packet via a second receiver
    rig.tracker.flush(force=True)
    assert rig.count(Fix) == len({x.ts for x in rig.fixes()}) and rig.counters.get("drop.out_of_order") == 200


def test_min_fix_interval_limits_the_rate(tmp_path):
    rig = Rig(tmp_path, min_fix_interval=5.0)
    fly(rig, T0, air_s=300, still_s=300)
    rig.finish()
    stamps = [x.ts for x in rig.fixes()]
    assert all(b - a >= 5 for a, b in zip(stamps, stamps[1:], strict=False)) and 80 < len(stamps) < 130


def test_speed_is_derived_from_the_track_when_the_sender_does_not_say(rig):
    fly(rig, T0, report_speed=False)
    rig.finish()
    (f,) = rig.flights()
    assert f.close_reason == "landed" and f.airborne
    cruise = [x.speed / 10 for x in rig.fixes() if T0 + 200 < x.ts < T0 + 900]
    assert all(35.0 < v < 37.0 for v in cruise)


def test_flight_date_is_the_local_date_and_offset_follows_dst(rig):
    late = int(datetime(2026, 7, 15, 22, 30, tzinfo=UTC).timestamp())  # 00:30 on the 16th in Switzerland (CEST)
    fly(rig, late, address="AAAAAA")
    winter = int(datetime(2026, 1, 15, 22, 30, tzinfo=UTC).timestamp())  # 23:30 on the 15th (CET)
    fly(rig, winter, address="BBBBBB")
    rig.finish()
    summer_flight, winter_flight = rig.flights()
    assert (summer_flight.date, summer_flight.utc_offset_s) == (date(2026, 7, 16), 7200)
    assert (winter_flight.date, winter_flight.utc_offset_s) == (date(2026, 1, 15), 3600)


def test_slow_movement_never_opens_a_flight(rig):
    for t in range(T0, T0 + 900):
        rig.feed(beacon(t, LAT0, LON0 + 3.0 * (t - T0) / M_PER_DEG_LON, 1500, 14.9))  # walking uphill, fast
    assert rig.flights() == []


def test_one_fast_fix_is_not_a_take_off(rig):
    for i, speed in enumerate([0, 0, 40, 0, 0, 40, 0, 0]):
        rig.feed(beacon(T0 + i, speed=speed))
    assert rig.flights() == []


def test_a_walker_who_speeds_up_takes_off_after_two_fast_fixes(rig):
    for i, speed in enumerate([5, 8, 12, 16, 18, 25, 30]):
        rig.feed(beacon(T0 + i, LAT0, LON0 + 5.0 * i / M_PER_DEG_LON, 2000, speed))
    rig.tracker.flush(force=True)
    (f,) = rig.flights()
    assert f.status == "active" and ts_of(f.takeoff_time) == T0 + 3  # the first fix at 15 km/h or more (16)
    assert ts_of(f.start_time) == T0  # and the flight opened with the second one (18 km/h), keeping the buffer


# ---------------------------------------------------------------- the region rule


def germany(t, lon, speed=36.0):
    return beacon(t, 48.5, lon, 2000, speed)


def test_a_flight_that_starts_outside_every_region_is_not_recorded(rig):
    for t in range(T0, T0 + 600):
        rig.feed(germany(t, 11.0 + 10.0 * (t - T0) / M_PER_DEG_LON))
    assert rig.flights() == [] and rig.counters.get("drop.outside_region") >= 1
    assert rig.tracker.live()["aircraft"] == []  # heard in the margin of the feed, not shown


def test_flying_into_a_region_does_not_start_a_flight_there(rig):
    fly(rig, T0, lon=5.70, alt=3000, air_s=1500, still_s=0, ground_s=60)  # crosses 5.85 degrees east after 1140 s
    state = rig.tracker.states["FLR112880"]
    assert state.last.lon > 5.85 and state.in_region and state.last.speed == 36.0  # it is flying inside Switzerland now
    assert state.flying_outside and rig.counters.get("drop.outside_region") == 1
    rig.finish()
    assert rig.flights() == [] and rig.count(Fix) == 0


def test_after_landing_outside_the_next_take_off_inside_counts(rig):
    t = fly(rig, T0, lon=5.70, alt=3000, air_s=300, still_s=300, ground_s=60)  # lands at 5.74, outside
    assert rig.tracker.states["FLR112880"].flying_outside is False  # landed: the unrecorded flight is over
    fly(rig, t + 600, lon=LON0, ground_s=60, air_s=300, still_s=300)  # a new flight, in Switzerland
    rig.finish()
    (f,) = rig.flights()
    assert f.takeoff_lon == pytest.approx(LON0, abs=1e-3)


def test_a_flight_that_started_inside_may_leave_the_region(rig):
    fly(rig, T0, lon=10.45, alt=2500, air_s=900, ground_s=60, still_s=300)  # crosses 10.55 degrees east
    rig.finish()
    (f,) = rig.flights()
    assert f.close_reason == "landed" and f.max_lon > 10.55 and f.fix_count > 1100


# ---------------------------------------------------------------- gaps, resuming, restarts


def silence_then(rig, last_ts, minutes):
    """The device is silent for ``minutes``; the server's clock keeps running (sweep)."""
    rig.tracker.sweep(datetime.fromtimestamp(last_ts + minutes * 60, UTC))


def test_silence_closes_the_flight_as_a_gap(rig):
    end = fly(rig, T0, still_s=0)
    silence_then(rig, end - 1, 19)
    assert rig.flights()[0].status == "active"
    silence_then(rig, end - 1, 21)
    rig.finalizer.drain()
    (f,) = rig.flights()
    assert (f.status, f.close_reason) == ("closed", "gap") and ts_of(f.landing_time) == ts_of(f.end_time)


def test_the_next_position_after_a_long_silence_closes_the_old_flight_first(rig):
    end = fly(rig, T0, still_s=0)
    for i in range(10):  # nothing swept; the pilot comes back 25 minutes later, on the ground
        rig.feed(beacon(end + 1500 + i, LAT0, LON0 + 0.2, 1000, 0.0, address="112880"), received=end + 1500 + i)
    assert rig.flights()[0].close_reason == "gap"


def test_back_in_the_air_within_90_minutes_is_the_same_flight(rig):
    end = fly(rig, T0, still_s=0)  # ends in the air, 1400 m
    silence_then(rig, end - 1, 25)
    lon = LON0 + 9000 / M_PER_DEG_LON
    for i in range(120):  # airborne again 40 minutes after the last position
        rig.feed(beacon(end + 2400 + i, LAT0, lon + 10.0 * i / M_PER_DEG_LON, 1500, 36.0))
    rig.finish()
    (f,) = rig.flights()
    assert rig.counters.get("flights.resumed") == 1
    assert ts_of(f.end_time) >= end + 2400 + 100 and f.fix_count > 1000


def test_back_after_more_than_90_minutes_is_a_new_flight(rig):
    end = fly(rig, T0, still_s=0)
    silence_then(rig, end - 1, 25)
    fly(rig, end + 95 * 60, ground_s=60, air_s=300, still_s=300, lon=LON0 + 0.1)
    rig.finish()
    assert len(rig.flights()) == 2 and rig.counters.get("flights.resumed") == 0


@pytest.mark.parametrize(("agl", "resumes"), [(500.0, True), (20.0, False)])
def test_a_pilot_lost_near_the_ground_has_landed_not_left_the_coverage(tmp_path, agl, resumes):
    rig = Rig(tmp_path, terrain=FlatTerrain(fn=lambda lat, lon: 1400.0 - agl))  # the last fix is at 1400 m
    end = fly(rig, T0, still_s=0)
    silence_then(rig, end - 1, 25)
    lon = LON0 + 9000 / M_PER_DEG_LON
    for i in range(120):
        rig.feed(beacon(end + 2400 + i, LAT0, lon + 10.0 * i / M_PER_DEG_LON, 1500, 36.0))
    rig.finish()
    assert (len(rig.flights()) == 1) is resumes


def test_a_restart_closes_open_flights_and_they_resume(rig):
    end = fly(rig, T0, still_s=0, air_s=600)
    closed = rig.restart()
    (f,) = rig.flights()
    assert closed == [f.id] and (f.status, f.close_reason) == ("closed", "gap")
    lon = LON0 + 6000 / M_PER_DEG_LON
    for i in range(60):  # the pilot is still up there a minute later
        rig.feed(beacon(end + 60 + i, LAT0, lon + 10.0 * i / M_PER_DEG_LON, 1500, 36.0))
    rig.finish()
    (f,) = rig.flights()
    assert rig.counters.get("flights.resumed") == 1 and ts_of(f.end_time) >= end + 100


def test_after_a_restart_a_much_later_take_off_is_a_new_flight(rig):
    end = fly(rig, T0, still_s=0, air_s=600)
    rig.restart()
    fly(rig, end + 3 * 3600, ground_s=60, air_s=300, still_s=300)
    rig.finish()
    assert len(rig.flights()) == 2


# ---------------------------------------------------------------- when has a pilot landed?


def hover(rig, start, minutes, alt, climb=0.0):
    for t in range(start, start + minutes * 60):
        rig.feed(beacon(t, LAT0 + 0.2, LON0, alt, 1.0, climb=climb))


def airborne_at(rig, start, lat=LAT0 + 0.2):
    for i in range(30):
        rig.feed(beacon(start + i, lat, LON0 + 10.0 * i / M_PER_DEG_LON, 1400, 36.0))
    return start + 30


def test_standing_still_high_above_known_ground_is_hovering_not_landing(tmp_path):
    rig = Rig(tmp_path, terrain=FlatTerrain(1000.0))
    t = airborne_at(rig, T0)
    hover(rig, t, 15, alt=1500.0)  # 500 m above the ground, stationary: strong wind
    assert rig.tracker.states["FLR112880"].flight is not None
    hover(rig, t + 900, 5, alt=1010.0)  # now it is on the ground
    assert rig.tracker.states["FLR112880"].flight is None
    rig.finish()
    assert rig.flights()[0].close_reason == "landed"


@pytest.mark.parametrize(("climb", "landed"), [(0.1, True), (-0.4, True), (1.2, False), (-2.0, False)])
def test_without_terrain_a_steady_vario_means_still_airborne(rig, climb, landed):
    t = airborne_at(rig, T0)
    hover(rig, t, 6, alt=1400.0, climb=climb)
    assert (rig.tracker.states["FLR112880"].flight is None) is landed


def test_the_stationary_period_restarts_when_the_pilot_moves_away(rig):
    t = airborne_at(rig, T0)
    for minute in range(10):  # stands still 3 minutes, shuffles 400 m, again: never 4 minutes within 150 m
        for s in range(180):
            rig.feed(beacon(t + minute * 180 + s, LAT0 + 0.2 + 0.0036 * minute, LON0, 1400, 1.0))
    assert rig.tracker.states["FLR112880"].flight is not None


# ---------------------------------------------------------------- sanity filters


def test_future_and_stale_time_stamps_are_dropped(rig):
    b = beacon(T0)
    rig.feed(b, received=T0 - 121)  # the stamp is 121 s ahead of the clock
    rig.feed(b, received=T0 + 31 * 60)  # and a very old one
    assert (rig.counters.get("drop.future"), rig.counters.get("drop.stale")) == (1, 1)
    assert rig.tracker.states == {}
    rig.feed(b, received=T0 - 119)
    rig.feed(beacon(T0 + 1), received=T0 + 1 + 29 * 60)
    assert rig.counters.get("drop.future") == 1 and rig.counters.get("drop.stale") == 1


def test_a_position_older_than_the_last_one_is_dropped(rig):
    rig.feed(beacon(T0 + 10))
    rig.feed(beacon(T0 + 5))
    rig.feed(beacon(T0 + 10))
    assert rig.counters.get("drop.out_of_order") == 2


def test_a_jump_of_2_km_at_over_500_kmh_is_a_glitch_but_the_new_place_is_accepted_after_three(rig):
    t = airborne_at(rig, T0)
    far = LON0 + 50_000 / M_PER_DEG_LON  # 50 km away, one second later: 180,000 km/h
    results = []
    for i in range(5):
        rig.feed(beacon(t + i, LAT0 + 0.2, far + 10.0 * i / M_PER_DEG_LON, 1400, 36.0))
        results.append(rig.counters.get("drop.glitch"))
    assert results == [1, 2, 3, 3, 3]  # three rejected, then it is taken as real
    assert rig.tracker.states["FLR112880"].last.lon == pytest.approx(far + 40.0 / M_PER_DEG_LON, abs=1e-5)
    rig.finish()
    (f,) = rig.flights()
    assert f.distance_km < 1.0  # the hop itself is not travelled distance


def test_a_small_jump_is_not_a_glitch(rig):
    t = airborne_at(rig, T0)
    rig.feed(beacon(t, LAT0 + 0.2, LON0 + 1500 / M_PER_DEG_LON, 1400, 36.0))  # 1.5 km in a second: odd, but under 2 km
    assert rig.counters.get("drop.glitch") == 0


def test_one_implausibly_fast_position_is_just_discarded(rig):
    t = airborne_at(rig, T0)
    rig.feed(beacon(t, LAT0 + 0.2, LON0 + 300 / M_PER_DEG_LON, 1400, 200.0))
    for i in range(1, 40):
        rig.feed(beacon(t + i, LAT0 + 0.2, LON0 + (300 + 10.0 * i) / M_PER_DEG_LON, 1400, 36.0))
    assert rig.counters.get("drop.spike") == 1 and not rig.tracker.states["FLR112880"].misclassified
    rig.finish()
    assert len(rig.flights()) == 1


def test_a_device_that_keeps_flying_too_fast_is_a_mis_configured_one(rig):
    t = airborne_at(rig, T0)
    rig.tracker.flush(force=True)
    assert rig.count(Flight) == 1 and rig.count(Fix) > 0
    for i in range(2):  # two of the last ten positions at 180 km/h
        rig.feed(beacon(t + i, LAT0 + 0.2, LON0 + 400 / M_PER_DEG_LON, 1400, 180.0))
    assert rig.tracker.states["FLR112880"].misclassified
    assert (rig.count(Flight), rig.count(Fix), rig.count(Device)) == (0, 0, 0)  # the open flight is gone
    rig.feed(beacon(t + 10, LAT0 + 0.2, LON0, 1400, 30.0))  # ignored from then on
    assert rig.counters.get("drop.misconfigured") == 2 and rig.count(Flight) == 0
    assert rig.tracker.live()["aircraft"] == [] and rig.flights() == []


def test_the_overspeed_window_is_ten_fixes(rig):
    t = airborne_at(rig, T0)
    rig.feed(beacon(t, LAT0 + 0.2, LON0, 1400, 180.0))
    for i in range(1, 11):  # ten normal fixes push the spike out of the window
        rig.feed(beacon(t + i, LAT0 + 0.2, LON0 + 10.0 * i / M_PER_DEG_LON, 1400, 36.0))
    rig.feed(beacon(t + 11, LAT0 + 0.2, LON0 + 110 / M_PER_DEG_LON, 1400, 180.0))
    assert not rig.tracker.states["FLR112880"].misclassified and rig.counters.get("drop.spike") == 2


def test_other_aircraft_types_have_their_own_speed_limit(rig):
    for i in range(30):  # a "hang glider" at 150 km/h is fine, a paraglider is not
        rig.feed(beacon(T0 + i, LAT0, LON0 + 40.0 * i / M_PER_DEG_LON, 2000, 150.0, aircraft_type=6, address="AAAAAA"))
    assert not rig.tracker.states["FLRAAAAAA"].misclassified
    rig.feed(beacon(T0 + 40, speed=200.0, aircraft_type=6, address="AAAAAA"))
    rig.feed(beacon(T0 + 41, speed=200.0, aircraft_type=6, address="AAAAAA"))
    assert rig.tracker.states["FLRAAAAAA"].misclassified


# ---------------------------------------------------------------- terrain


def test_terrain_height_is_filled_in_and_decides_whether_it_was_a_flight(tmp_path):
    rig = Rig(tmp_path, terrain=FlatTerrain(fn=lambda lat, lon: 1000.0))
    fly(rig, T0, alt=2300.0)
    rig.finish()
    (f,) = rig.flights()
    assert f.ground_filled and f.airborne and f.max_agl == pytest.approx(1300.0, abs=0.5)
    assert all(x.ground == 10_000 for x in rig.fixes())


def test_driving_to_the_launch_is_not_a_flight(tmp_path):
    rig = Rig(tmp_path, terrain=FlatTerrain(fn=lambda lat, lon: 795.0))
    for t in range(T0, T0 + 900):  # 15 minutes at 54 km/h on a road, 5 m above the ground
        rig.feed(beacon(t, LAT0, LON0 + 15.0 * (t - T0) / M_PER_DEG_LON, 800.0, 54.0))
    for t in range(T0 + 900, T0 + 1300):
        rig.feed(beacon(t, LAT0, LON0 + 13500 / M_PER_DEG_LON, 800.0, 0.0))
    rig.finish()
    (f,) = rig.flights()
    assert f.close_reason == "landed" and f.airborne is False and f.max_agl == pytest.approx(5.0, abs=0.2)


def test_without_terrain_the_flight_is_judged_by_altitude_range_and_speed(rig):
    fly(rig, T0)
    rig.finish()
    f = rig.flights()[0]
    assert f.ground_filled is False and f.max_agl is None and f.airborne is True


def test_missing_terrain_is_filled_in_later(tmp_path):
    heights = {"available": False}
    terrain = FlatTerrain(fn=lambda lat, lon: 1000.0 if heights["available"] else None)
    rig = Rig(tmp_path, terrain=terrain)
    fly(rig, T0)
    rig.finish()
    assert rig.flights()[0].ground_filled is False and all(x.ground is None for x in rig.fixes())
    heights["available"] = True
    rig.finalizer.finalize(rig.flights()[0].id)
    assert rig.flights()[0].ground_filled is True and all(x.ground is not None for x in rig.fixes())
