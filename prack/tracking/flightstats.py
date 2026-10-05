"""Flight statistics and the simplified preview path, computed from a flight's fixes in real units."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from ..geo import distance_m

MAX_PLAUSIBLE_SPEED_KMH = 450.0  # faster hops are position glitches and do not count towards distance
VARIO_WINDOW_S = 20.0  # best climb and sink are averaged over this long
ASCENT_THRESHOLD_M = 5.0
AIRBORNE_MIN_AGL_M = 50.0
AIRBORNE_MIN_DURATION_S = 60
NO_TERRAIN_MIN_RANGE_M = 100.0
NO_TERRAIN_MIN_SPEED_KMH = 20.0


@dataclass(slots=True)
class FixPoint:
    ts: int  # epoch seconds
    lat: float
    lon: float
    alt: float  # GPS altitude MSL, m
    ground: float | None = None  # terrain height, m
    speed: float | None = None  # km/h
    climb: float | None = None  # m/s


def to_datetime(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def _smooth(values: list[float], window: int = 5) -> list[float]:
    if len(values) < window:
        return list(values)
    half = window // 2
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _smoothing_window(fixes: list[FixPoint]) -> int:
    """About ten seconds of data: 9 fixes at 1 Hz (FLARM), 5 at the 4 s of FANET."""
    steps = sorted(b.ts - a.ts for a, b in zip(fixes, fixes[1:], strict=False) if b.ts > a.ts)
    median = steps[len(steps) // 2] if steps else 1
    return max(5, min(9, round(10 / median)))


def total_ascent(alts: list[float], threshold: float = ASCENT_THRESHOLD_M) -> float:
    """Sum of all climbs, where a climb counts only once it rose ``threshold`` metres and ended after falling as
    far. Summing every positive step instead would add up sensor noise (thousands of metres over a long flight)."""
    if not alts:
        return 0.0
    gain = 0.0
    low = high = alts[0]
    climbing = False
    for a in alts[1:]:
        if climbing:
            if a > high:
                high = a
            elif high - a >= threshold:
                gain += high - low
                climbing, low = False, a
        elif a < low:
            low = a
        elif a - low >= threshold:
            climbing, high = True, a
    return gain + (high - low if climbing else 0.0)


def compute_stats(fixes: list[FixPoint], takeoff_speed: float, landing_ts: int | None = None) -> dict:
    """The ``flights`` columns derived from a chronologically sorted list of fixes.

    ``airborne``: the pilot got at least 50 m above the ground and the flight lasted a minute. Without terrain
    data: a minute and either more than 100 m of altitude range or more than 20 km/h. Walking, driving and GPS
    noise must not count as flights.
    """
    if not fixes:
        return {"fix_count": 0}
    first, last = fixes[0], fixes[-1]
    alts = [f.alt for f in fixes]
    takeoff = next((f for f in fixes if (f.speed or 0.0) >= takeoff_speed), first)
    landing = last
    if landing_ts is not None:
        landing = next((f for f in fixes if f.ts >= landing_ts), last)

    distance = 0.0
    max_from_start = 0.0
    for prev, cur in zip(fixes, fixes[1:], strict=False):
        d = distance_m(prev.lat, prev.lon, cur.lat, cur.lon)
        dt = cur.ts - prev.ts
        if dt > 0 and d / dt * 3.6 > MAX_PLAUSIBLE_SPEED_KMH:
            continue
        distance += d
        max_from_start = max(max_from_start, distance_m(takeoff.lat, takeoff.lon, cur.lat, cur.lon))

    alt_gain = total_ascent(_smooth(alts, _smoothing_window(fixes)))

    max_climb = max_sink = 0.0
    j = 0
    for i in range(len(fixes)):
        while j < i and fixes[i].ts - fixes[j].ts > VARIO_WINDOW_S:
            j += 1
        dt = fixes[i].ts - fixes[j].ts
        if dt >= VARIO_WINDOW_S * 0.5:
            rate = (fixes[i].alt - fixes[j].alt) / dt
            max_climb, max_sink = max(max_climb, rate), min(max_sink, rate)

    speeds = [f.speed for f in fixes if f.speed is not None and f.speed < MAX_PLAUSIBLE_SPEED_KMH]
    max_speed = round(max(speeds), 1) if speeds else None
    agls = [f.alt - f.ground for f in fixes if f.ground is not None]
    max_agl = round(max(agls), 1) if agls else None

    duration = last.ts - first.ts
    if max_agl is not None:
        airborne = max_agl >= AIRBORNE_MIN_AGL_M and duration >= AIRBORNE_MIN_DURATION_S
    else:
        airborne = duration >= AIRBORNE_MIN_DURATION_S and (
            max(alts) - min(alts) > NO_TERRAIN_MIN_RANGE_M or (max_speed or 0.0) > NO_TERRAIN_MIN_SPEED_KMH
        )

    return {
        "fix_count": len(fixes),
        "start_time": to_datetime(first.ts),
        "end_time": to_datetime(last.ts),
        "takeoff_time": to_datetime(takeoff.ts),
        "landing_time": to_datetime(landing.ts),
        "takeoff_lat": takeoff.lat,
        "takeoff_lon": takeoff.lon,
        "takeoff_alt": round(takeoff.alt, 1),
        "landing_lat": landing.lat,
        "landing_lon": landing.lon,
        "landing_alt": round(landing.alt, 1),
        "min_lat": min(f.lat for f in fixes),
        "max_lat": max(f.lat for f in fixes),
        "min_lon": min(f.lon for f in fixes),
        "max_lon": max(f.lon for f in fixes),
        "max_alt": round(max(alts), 1),
        "min_alt": round(min(alts), 1),
        "max_agl": max_agl,
        "alt_gain": round(alt_gain, 1),
        "max_climb": round(max_climb, 2),
        "max_sink": round(max_sink, 2),
        "max_speed": max_speed,
        "distance_km": round(distance / 1000.0, 3),
        "straight_km": round(distance_m(takeoff.lat, takeoff.lon, landing.lat, landing.lon) / 1000.0, 3),
        "max_from_start_km": round(max_from_start / 1000.0, 3),
        "airborne": airborne,
    }


def simplify_track(
    points: list[tuple[float, float, float]], tolerance_m: float = 12.0, max_points: int = 500
) -> list[list[float]]:
    """Ramer-Douglas-Peucker simplification of ``(lon, lat, alt)`` points for the day overview.

    Best-first: the point that deviates most from the current polyline is added next, until every point is within
    ``tolerance_m`` or ``max_points`` are used. One pass, so the cost is bounded however winding the track is.
    Coordinates are rounded to about a metre.
    """
    n = len(points)

    def rounded(idx: list[int]) -> list[list[float]]:
        return [[round(points[i][0], 5), round(points[i][1], 5), round(points[i][2])] for i in idx]

    if n <= 2:
        return rounded(list(range(n)))
    kx = 111_320.0 * math.cos(math.radians(sum(p[1] for p in points) / n))
    ky = 110_540.0
    xs = [p[0] * kx for p in points]
    ys = [p[1] * ky for p in points]

    def farthest(a: int, b: int) -> tuple[float, int]:
        """Largest distance of the points between a and b from the chord a-b, and which point it is."""
        ax, ay = xs[a], ys[a]
        dx, dy = xs[b] - ax, ys[b] - ay
        norm = math.hypot(dx, dy)
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            d = math.hypot(xs[i] - ax, ys[i] - ay) if norm == 0.0 else abs(dy * (xs[i] - ax) - dx * (ys[i] - ay)) / norm
            if d > best:
                best, best_i = d, i
        return best, best_i

    keep = [0, n - 1]
    heap: list[tuple[float, int, int, int]] = []
    if n > 2:
        d, i = farthest(0, n - 1)
        heap.append((-d, 0, n - 1, i))
    while heap and len(keep) < max_points:
        neg_d, a, b, i = heapq.heappop(heap)
        if -neg_d <= tolerance_m:
            break
        keep.append(i)
        for lo, hi in ((a, i), (i, b)):
            if hi - lo > 1:
                d, j = farthest(lo, hi)
                heapq.heappush(heap, (-d, lo, hi, j))
    return rounded(sorted(keep))
