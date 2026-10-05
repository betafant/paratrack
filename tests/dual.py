"""One pilot seen over FLARM and FANET, as text lines through the whole chain."""

from __future__ import annotations

from datetime import UTC, datetime

from prack.ogn.builder import build_position
from prack.ogn.constants import SOURCE_CODES

from .rig import LAT0, LON0, M_PER_DEG_LAT, M_PER_DEG_LON, T0, Rig

FLARM, FANET = SOURCE_CODES["FLARM"], SOURCE_CODES["FANET"]
CRUISE_MS = 10.0  # 36 km/h


def at(t: int) -> datetime:
    return datetime.fromtimestamp(t, UTC)


def position(t: int, started: int = T0 + 60) -> tuple[float, float, float, float]:
    """(lat, lon, alt, speed km/h) of the pilot at time t: standing 60 s, then 10 m/s east for ever."""
    if t < started:
        return LAT0, LON0, 2300.0, 0.0
    dt = t - started
    return LAT0, LON0 + CRUISE_MS * dt / M_PER_DEG_LON, 2300.0 - dt, 36.0


def flarm(rig: Rig, t: int, address: str = "112880") -> None:
    lat, lon, alt, speed = position(t)
    rig.line(build_position(at(t), lat, lon, alt, address=address, speed_kmh=speed, course=90, climb_ms=-1.0), t)


def fanet(rig: Rig, t: int, address: str = "112880", offset_m: float = 11.0) -> None:
    lat, lon, alt, speed = position(t)
    line = build_position(
        at(t), lat + offset_m / M_PER_DEG_LAT, lon, alt, address=address, prefix="FNT", tocall="OGNFNT",
        speed_kmh=speed, course=90, climb_ms=-1.0, signal_db=None, errors=None, freq_khz=None, gps=None, turn_dps=None,
    )  # fmt: skip
    rig.line(line, t)
