from __future__ import annotations

import math
import random

import pytest

from prack.geo import distance_m
from prack.tracking.flightstats import FixPoint, compute_stats, simplify_track, to_datetime, total_ascent

T0 = 1_784_000_000
LAT, LON = 46.6, 8.2
M_PER_DEG_LAT = 111_195.0


def fix(i: int, north_m: float = 0.0, alt: float = 2000.0, **kw) -> FixPoint:
    return FixPoint(T0 + i, LAT + north_m / M_PER_DEG_LAT, LON, alt, **kw)


def test_distance_straight_line_and_extent():
    fixes = [fix(i, north_m=10.0 * i, alt=2000 - i, speed=36.0) for i in range(101)]  # 1 km north at 36 km/h
    s = compute_stats(fixes, takeoff_speed=15)
    assert s["fix_count"] == 101
    assert s["distance_km"] == pytest.approx(1.0, abs=0.01)
    assert s["straight_km"] == pytest.approx(1.0, abs=0.01)
    assert s["max_from_start_km"] == pytest.approx(1.0, abs=0.01)
    assert (s["start_time"], s["end_time"]) == (to_datetime(T0), to_datetime(T0 + 100))
    assert (s["max_alt"], s["min_alt"]) == (2000.0, 1900.0)
    assert s["max_speed"] == 36.0


def test_circling_has_distance_but_no_straight_line():
    fixes = []
    for i in range(0, 361, 5):  # one circle of 100 m radius
        a = math.radians(i)
        fixes.append(
            FixPoint(T0 + i, LAT + 100 * math.sin(a) / M_PER_DEG_LAT, LON + 100 * (math.cos(a) - 1) / 75_900.0, 2000.0)
        )
    s = compute_stats(fixes, takeoff_speed=15)
    assert s["distance_km"] == pytest.approx(2 * math.pi * 0.1, abs=0.02)
    assert s["straight_km"] < 0.01 and s["max_from_start_km"] == pytest.approx(0.2, abs=0.02)


def test_glitch_hops_do_not_count_towards_distance():
    fixes = [fix(i, north_m=10.0 * i) for i in range(10)] + [fix(10, north_m=50_000.0)] + [fix(11, north_m=110.0)]
    s = compute_stats(fixes, takeoff_speed=15)
    assert s["distance_km"] < 0.2  # the 50 km excursion and the jump back are skipped


def test_altitude_gain_counts_climbs_not_noise():
    climb = [fix(i, alt=1000 + 2.0 * i) for i in range(60)]  # +2 m/s for a minute: 118 m
    noisy = [fix(60 + i, alt=1118 + (3.0 if i % 2 else -3.0)) for i in range(60)]  # +-3 m jitter in level flight
    s = compute_stats(climb + noisy, takeoff_speed=15)
    assert s["alt_gain"] == pytest.approx(118, abs=6)


def test_altitude_gain_of_random_sensor_noise_stays_small():
    rng = random.Random(3)
    noise = [fix(i, alt=2000 + rng.gauss(0, 3.0)) for i in range(7200)]  # two hours of level flight, 3 m of noise
    assert compute_stats(noise, takeoff_speed=15)["alt_gain"] < 150  # summing every rise would give ~2800 m
    sparse = [fix(4 * i, alt=2000 + rng.gauss(0, 3.0)) for i in range(1800)]  # the same flight seen at 4 s (FANET)
    assert compute_stats(sparse, takeoff_speed=15)["alt_gain"] < 250


def test_real_climbs_survive_noise_at_both_sampling_rates():
    rng = random.Random(5)
    alts, a = [], 1500.0
    for _ in range(3):  # three thermals: +300 m at 2 m/s, then 300 m of gliding down
        for _ in range(150):
            a += 2.0
            alts.append(a + rng.gauss(0, 3))
        for _ in range(300):
            a -= 1.0
            alts.append(a + rng.gauss(0, 3))
    for step in (1, 4):
        fixes = [fix(i * step, alt=alt) for i, alt in enumerate(alts[::step])]
        assert compute_stats(fixes, takeoff_speed=15)["alt_gain"] == pytest.approx(900, rel=0.08)


def test_total_ascent_hysteresis():
    assert total_ascent([]) == 0.0 and total_ascent([100.0]) == 0.0
    assert total_ascent([0, 10, 20, 10, 0, 10, 20]) == 40  # two climbs of 20 m with a 20 m descent between
    assert total_ascent([0, 4, 0, 4, 0, 4]) == 0  # below the threshold
    assert total_ascent([100, 50, 0, 20]) == 20 and total_ascent([0, 30]) == 30


def test_best_climb_and_sink_over_twenty_seconds():
    fixes = [fix(i, alt=1000.0) for i in range(40)]
    fixes += [fix(40 + i, alt=1000.0 + 4.0 * i) for i in range(21)]  # +4 m/s for 20 s
    fixes += [fix(61 + i, alt=1080.0 - 3.0 * i) for i in range(30)]  # then -3 m/s
    s = compute_stats(fixes, takeoff_speed=15)
    assert s["max_climb"] == pytest.approx(4.0, abs=0.05)
    assert s["max_sink"] == pytest.approx(-3.0, abs=0.05)


def test_takeoff_is_the_first_fast_fix_and_landing_the_given_time():
    fixes = [fix(i, speed=2.0) for i in range(30)] + [fix(30 + i, north_m=10.0 * i, speed=30.0) for i in range(60)]
    s = compute_stats(fixes, takeoff_speed=15, landing_ts=T0 + 70)
    assert s["takeoff_time"] == to_datetime(T0 + 30)
    assert s["landing_time"] == to_datetime(T0 + 70)
    assert s["straight_km"] == pytest.approx(0.4, abs=0.01)  # from the take-off fix to the landing fix


@pytest.mark.parametrize(
    ("max_agl", "seconds", "expected"),
    [(120.0, 300, True), (49.0, 300, False), (50.0, 300, True), (500.0, 59, False), (500.0, 60, True)],
)
def test_airborne_with_terrain(max_agl, seconds, expected):
    fixes = [fix(i, alt=1000.0 + max_agl * (i == seconds // 2), ground=1000.0) for i in range(seconds + 1)]
    assert compute_stats(fixes, takeoff_speed=15)["airborne"] is expected


def test_walking_and_driving_are_not_flights_when_terrain_is_known():
    drive = [fix(i, north_m=15.0 * i, alt=800.0, ground=795.0, speed=54.0) for i in range(600)]  # 10 min at 54 km/h
    s = compute_stats(drive, takeoff_speed=15)
    assert s["airborne"] is False and s["max_agl"] == 5.0


@pytest.mark.parametrize(
    ("alt_range", "speed", "seconds", "expected"),
    [(150.0, 5.0, 300, True), (50.0, 5.0, 300, False), (50.0, 25.0, 300, True), (500.0, 40.0, 30, False)],
)
def test_airborne_without_terrain(alt_range, speed, seconds, expected):
    fixes = [fix(i, alt=1000.0 + alt_range * (i == seconds // 2), speed=speed) for i in range(seconds + 1)]
    s = compute_stats(fixes, takeoff_speed=15)
    assert s["max_agl"] is None and s["airborne"] is expected


def test_empty_and_single_fix():
    assert compute_stats([], 15) == {"fix_count": 0}
    s = compute_stats([fix(0)], 15)
    assert s["fix_count"] == 1 and s["distance_km"] == 0.0 and s["airborne"] is False


# ---------------------------------------------------------------- preview


def track(n: int) -> list[tuple[float, float, float]]:
    """A wiggly route of n points, about 20 km long."""
    return [(8.0 + i * 1e-4, 46.5 + 0.005 * math.sin(i / 30.0), 1500.0 + 300 * math.sin(i / 200.0)) for i in range(n)]


def test_simplify_keeps_the_end_points_and_the_shape():
    pts = track(7200)
    out = simplify_track(pts)
    assert 10 < len(out) <= 500
    assert out[0] == [round(pts[0][0], 5), round(pts[0][1], 5), round(pts[0][2])]
    assert out[-1][:2] == [round(pts[-1][0], 5), round(pts[-1][1], 5)]
    # every original point is within the tolerance of the simplified polyline (metres, flat-earth)
    kx, ky = 111_320.0 * math.cos(math.radians(46.5)), 110_540.0
    poly = [(p[0] * kx, p[1] * ky) for p in out]

    def dist(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
        return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)

    worst = max(
        min(dist((p[0] * kx, p[1] * ky), poly[i], poly[i + 1]) for i in range(len(poly) - 1)) for p in pts[::50]
    )
    assert worst < 12.5  # every point within the tolerance of the simplified line


def test_simplify_limits_the_number_of_points_and_survives_tiny_inputs():
    zigzag = [(8.0 + i * 1e-5, 46.5 + (0.0003 if i % 2 else 0.0), 1000.0) for i in range(5000)]
    assert len(simplify_track(zigzag, tolerance_m=1.0, max_points=500)) <= 500
    assert simplify_track([]) == [] and len(simplify_track([(8.0, 46.5, 1000.0)])) == 1
    assert len(simplify_track([(8.0, 46.5, 1000.0)] * 50)) == 2  # all the same point: just the ends


def test_distance_helper_sanity():
    assert distance_m(46.0, 8.0, 46.0, 8.0) == 0.0
    assert distance_m(0.0, 0.0, 0.0, 1.0) == pytest.approx(111_195, rel=1e-3)


def test_simplify_is_fast_enough_for_a_two_hour_flight():
    import time

    thermalling = [
        (8.2 + 60 * math.cos(i * 0.17) / 75_900 + i * 2e-6, 46.6 + 60 * math.sin(i * 0.17) / 111_195 + i * 1e-6, 2000.0)
        for i in range(7200)
    ]
    started = time.perf_counter()
    out = simplify_track(thermalling)
    assert len(out) <= 500 and time.perf_counter() - started < 1.0
    zigzag = [(8.0 + i * 1e-5, 46.5 + (0.0003 if i % 2 else 0.0), 1000.0) for i in range(7200)]
    started = time.perf_counter()
    assert len(simplify_track(zigzag, tolerance_m=1.0)) <= 500
    assert time.perf_counter() - started < 3.0  # the worst case is bounded
