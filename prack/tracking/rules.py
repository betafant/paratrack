"""Thresholds of the tracking rules, in one place."""

from __future__ import annotations

# --- time sanity
FUTURE_S = 2 * 60  # a time stamp this far ahead of the reception time is wrong
STALE_S = 30 * 60  # and this old is not live data

# --- glitches and plausibility
GLITCH_MIN_DISTANCE_M = 2000.0  # a jump of more than this ...
GLITCH_MIN_SPEED_KMH = 500.0  # ... that implies more than this is a bad position
GLITCH_MAX_IN_A_ROW = 3  # after this many, the new place is accepted as real
# Highest ground speed a device of this OGN aircraft type can plausibly have (strong tail wind included).
MAX_TYPE_SPEED_KMH = {7: 130.0, 6: 180.0}
SPEED_WINDOW = 10  # fixes looked at ...
SPEED_LIMIT_COUNT = 2  # ... too fast ones in them make the device "mis-configured"

# --- take-off, landing, gaps
TAKEOFF_SPEED_KMH = {7: 15.0, 6: 18.0, 1: 35.0}
DEFAULT_TAKEOFF_SPEED_KMH = 30.0
TAKEOFF_FIXES = 2  # consecutive fixes at or above the take-off speed open a flight
PRE_TAKEOFF_S = 60  # fixes kept before take-off, so the launch run is part of the flight
STATIONARY_SPEED_KMH = 5.0
STATIONARY_RADIUS_M = 150.0
LANDING_S = 4 * 60  # stationary this long: landed
LANDED_MAX_AGL_M = 80.0  # higher than this above known ground is hovering, not landed
LANDED_MAX_VARIO_MS = 0.5  # without terrain: a steady vario means still airborne
GAP_S = 20 * 60  # silence this long closes a flight as "gap"
RESUME_S = 90 * 60  # airborne again within this after a gap: the same flight (coverage holes)
RESUME_MIN_AGL_M = 50.0  # lost below this above the ground: it landed, it did not leave coverage

# --- one aircraft, several protocols
ALIAS_MAX_DISTANCE_M = 3000.0
ALIAS_MAX_AGE_S = 10 * 60
SOURCE_HOLD_S = 30  # a lower-ranked protocol is ignored while a better one was heard this recently

# --- live view
LIVE_WINDOW_S = 10 * 60
TRAIL_POINTS = 360
FLIGHT_UPDATE_S = 10.0  # how often open flights' summaries are written


def takeoff_speed(aircraft_type: int) -> float:
    return TAKEOFF_SPEED_KMH.get(aircraft_type, DEFAULT_TAKEOFF_SPEED_KMH)
