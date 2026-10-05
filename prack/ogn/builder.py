"""Build synthetic OGN APRS lines, the way the OGN servers send them.

Used by the tests, the fake OGN server and the demo simulator, so the same parser that reads the real
feed is exercised by the synthetic one.
"""

from __future__ import annotations

from datetime import datetime

from .parser import FEET_TO_M, FPM_TO_MS, KNOTS_TO_KMH, ROT_TO_DPS


def _coord(value: float, deg_digits: int, positive: str, negative: str) -> tuple[str, int]:
    """Decimal degrees -> (``DDMM.mmN``, extra digit for ``!Wxy!``)."""
    hemisphere = positive if value >= 0 else negative
    thousandths = round(abs(value) * 60000)  # 1/1000 arc minute is about 1.85 m
    degrees, rest = divmod(thousandths, 60000)
    hundredths, extra = divmod(rest, 10)
    return f"{degrees:0{deg_digits}d}{hundredths // 100:02d}.{hundredths % 100:02d}{hemisphere}", extra


def flags_byte(aircraft_type: int, address_type: int = 2, *, stealth: bool = False, no_tracking: bool = False) -> int:
    return (
        (0x80 if stealth else 0) | (0x40 if no_tracking else 0) | ((aircraft_type & 0x0F) << 2) | (address_type & 0x03)
    )


def build_position(
    ts: datetime,
    lat: float,
    lon: float,
    alt_m: float,
    *,
    address: str = "112880",
    prefix: str = "FLR",
    callsign: str | None = None,
    tocall: str = "OGFLR",
    path: str | None = None,
    receiver: str = "TestRx",
    speed_kmh: float | None = 0.0,
    course: int | None = 0,
    climb_ms: float | None = 0.0,
    turn_dps: float | None = None,
    aircraft_type: int = 7,
    address_type: int = 2,
    stealth: bool = False,
    no_tracking: bool = False,
    include_id: bool = True,
    signal_db: float | None = 12.5,
    errors: int | None = 0,
    freq_khz: float | None = 1.2,
    gps: str | None = "2x3",
    symbol_table: str = "/",
    symbol: str = "'",
) -> str:
    """One aircraft position beacon. ``None`` leaves a field out, like real senders do."""
    lat_s, lat_extra = _coord(lat, 2, "N", "S")
    lon_s, lon_extra = _coord(lon, 3, "E", "W")
    csp = ""
    if speed_kmh is not None and course is not None:
        csp = f"{course % 360:03d}/{round(speed_kmh / KNOTS_TO_KMH):03d}"
    feet = round(alt_m / FEET_TO_M)
    alt = f"/A={'-' if feet < 0 else ''}{abs(feet):0{5 if feet < 0 else 6}d}"
    tokens = [f"!W{lat_extra}{lon_extra}!"]
    if include_id:
        flags = flags_byte(aircraft_type, address_type, stealth=stealth, no_tracking=no_tracking)
        tokens.append(f"id{flags:02X}{address}")
    if climb_ms is not None:
        tokens.append(f"{round(climb_ms / FPM_TO_MS):+04d}fpm")
    if turn_dps is not None:
        tokens.append(f"{turn_dps / ROT_TO_DPS:+.1f}rot")
    if signal_db is not None:
        tokens.append(f"{signal_db:.1f}dB")
    if errors is not None:
        tokens.append(f"{errors}e")
    if freq_khz is not None:
        tokens.append(f"{freq_khz:+.1f}kHz")
    if gps is not None:
        tokens.append(f"gps{gps}")
    call = callsign or f"{prefix}{address}"
    route = path if path is not None else f"qAS,{receiver}"
    return f"{call}>{tocall},{route}:/{ts:%H%M%S}h{lat_s}{symbol_table}{lon_s}{symbol}{csp}{alt} " + " ".join(tokens)


def build_status(
    ts: datetime,
    name: str,
    *,
    address: str = "112880",
    prefix: str = "FNT",
    tocall: str = "OGNFNT",
    receiver: str = "TestRx",
) -> str:
    """A FANET style status message that carries the pilot name."""
    return f'{prefix}{address}>{tocall},qAS,{receiver}:>{ts:%H%M%S}h Name="{name}" 26.0dB -12.1kHz'
