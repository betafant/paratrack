"""Scaled-integer encoding of a fix, so the ``fixes`` table stays small (about 62 bytes per row).

SQLite stores small integers in 1 to 4 bytes; REAL and DATETIME columns would take 8 and ~25 bytes.
The views ``fixes_v`` and ``flights_v`` turn everything back into real units for analysis.
"""

from __future__ import annotations

LATLON = 1_000_000  # degrees * 1e6: 0.11 m
ALT = 10  # metres * 10 (altitude and terrain height)
SPEED = 10  # km/h * 10
CLIMB = 100  # m/s * 100
TURN = 10  # deg/s * 10
SIGNAL = 10  # dB * 10
FREQ = 10  # kHz * 10

INT32 = (-(2**31), 2**31 - 1)
INT16 = (-(2**15), 2**15 - 1)


def scale(value: float | None, factor: int, bounds: tuple[int, int] = INT16) -> int | None:
    """``value * factor`` as an integer clamped to the column range (a hostile value must not break a batch)."""
    if value is None:
        return None
    return max(bounds[0], min(bounds[1], round(value * factor)))


def unscale(value: int | None, factor: int) -> float | None:
    return None if value is None else value / factor
