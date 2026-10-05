"""Parser for OGN-flavoured APRS lines.

Aircraft beacon (FLARM)::

    FLR112880>OGFLR,qAS,Pizol:/183037h4702.30N/00926.07E'090/019/A=005300 !W00! id1E112880 -380fpm +0.0rot

``parse_line`` never raises. It returns a :class:`Beacon` (aircraft position), a :class:`Status`
(a status message that carries a pilot name, FANET style) or a :class:`Skipped` that says why the
line was not used, so that nothing disappears silently.

Units are converted on the way in: feet -> m, knots -> km/h, ft/min -> m/s, rot -> deg/s.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .constants import (
    ADSB_TOCALLS,
    NO_ID_SOURCES,
    RECEIVER_TOCALLS,
    SOURCE_DEFAULT_TYPES,
    WINGMAN_TOCALL,
    normalize_tocall,
    source_for,
)

KNOTS_TO_KMH = 1.852
FEET_TO_M = 0.3048
FPM_TO_MS = 0.00508
ROT_TO_DPS = 3.0  # 1 rot = half a turn per minute = 3 deg/s

HEADER_RE = re.compile(r"^(?P<callsign>[^>\s]{1,12})>(?P<tocall>[^,:]+)(?:,(?P<path>[^:]*))?:(?P<payload>.*)$")

POSITION_RE = re.compile(
    r"^(?:[/@](?P<time>\d{6})(?P<tfmt>[hz])|[!=])"
    r"(?P<lat>\d{4}\.\d{2})(?P<ns>[NS])(?P<symtab>.)"
    r"(?P<lon>\d{5}\.\d{2})(?P<ew>[EW])(?P<sym>.)"
    r"(?:(?P<course>\d{3})/(?P<speed>\d{3}))?"
    r"(?:/A=(?P<alt>-?\d{5,6}))?"
    r"(?P<rest>.*)$"
)

STATUS_RE = re.compile(r"^>(?:(?P<time>\d{6})(?P<tfmt>[hz]))?(?P<text>.*)$")
NAME_RE = re.compile(r'Name="([^"]*)"')

_PRECISION_RE = re.compile(r"!W(\d)(\d)!")
_ID_RE = re.compile(r"id([0-9A-Za-z-]+)")
_CLIMB_RE = re.compile(r"([+-]?\d+)fpm")
_TURN_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)rot")
_SIGNAL_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)dB")
_ERRORS_RE = re.compile(r"(\d+)e")
_FREQ_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)kHz")
_GPS_RE = re.compile(r"gps(\d+x\d+)")
_Q_CONSTRUCT_RE = re.compile(r"^q[AO][A-Za-z]$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

HEX = frozenset("0123456789ABCDEF")


class Skip(enum.Enum):
    """Why a line produced neither a position nor a pilot name."""

    COMMENT = "comment"  # server comment, keepalive, blank line
    MALFORMED = "malformed"  # not an APRS packet we can read
    RECEIVER = "receiver"  # ground station / server beacon
    WEATHER = "weather"  # weather station (symbol _)
    NO_ALTITUDE = "no_altitude"  # position without altitude
    NO_ID = "no_id"  # position from a source that should send an id, but did not
    BAD_TIME = "bad_time"
    BAD_POSITION = "bad_position"
    STATUS_NO_NAME = "status_no_name"  # status message without a pilot name


@dataclass(frozen=True, slots=True)
class Skipped:
    reason: Skip
    source: str | None = None  # data source label, when the header could be read


_COMMENT = Skipped(Skip.COMMENT)


@dataclass(slots=True)
class Status:
    """A status message that carries a pilot name (FANET: ``Name="Mia"``)."""

    callsign: str
    tocall: str
    source: str
    address: str
    timestamp: datetime | None
    name: str


@dataclass(slots=True)
class Beacon:
    """One aircraft position with all telemetry, in SI-ish units."""

    callsign: str
    tocall: str
    source: str
    receiver: str | None
    timestamp: datetime  # UTC, timezone aware
    lat: float
    lon: float
    alt_m: float  # GPS altitude above mean sea level
    address: str  # 24 bit device address as 6 hex digits
    address_type: int
    aircraft_type: int  # effective type (ADS-B "paraglider" is forced to 0)
    reported_type: int  # type as encoded in the id field (0 when there is none)
    stealth: bool = False
    no_tracking: bool = False
    relayed: bool = False
    track_deg: float | None = None
    speed_kmh: float | None = None  # None when the sender reports 000/000 ("no data")
    climb_ms: float | None = None
    turn_dps: float | None = None
    signal_db: float | None = None
    errors: int | None = None
    freq_khz: float | None = None
    gps: str | None = None

    @property
    def epoch(self) -> int:
        """Timestamp as integer seconds since 1970 (UTC)."""
        return int(self.timestamp.timestamp())

    @property
    def ident(self) -> str:
        """Unique device identity such as ``FLR112880``.

        Usually the APRS callsign. Some senders use one callsign for every device (SkyBase uses
        ``SKYBASE``) or a user-chosen one (Wingman), and some ids differ from the callsign address;
        then a three letter prefix is combined with the address from the id field.
        """
        cs = self.callsign.upper()
        if len(cs) == 9 and cs[:3].isalpha():
            prefix = cs[:3]
        else:
            prefix = re.sub(r"[^A-Z0-9]", "", self.source.upper())[:3].ljust(3, "X")
        return f"{prefix}{self.address}"


def decode_time(value: str, fmt: str, reference: datetime) -> datetime | None:
    """Combine an APRS time stamp with the reception time ``reference`` (timezone aware UTC).

    ``h``: HHMMSS UTC; the day nearest to the reception time is used, which handles midnight.
    ``z``: DDHHMM UTC; the month nearest to the reception time is used.
    Returns None for impossible values.
    """
    try:
        if fmt == "h":
            h, m, s = int(value[0:2]), int(value[2:4]), int(value[4:6])
            candidate = reference.replace(hour=h, minute=m, second=s, microsecond=0)
            return min(
                (candidate + timedelta(days=d) for d in (-1, 0, 1)),
                key=lambda c: abs((c - reference).total_seconds()),
            )
        day, h, m = int(value[0:2]), int(value[2:4]), int(value[4:6])
        best: datetime | None = None
        for offset in (-1, 0, 1):
            year, month = reference.year, reference.month + offset
            if month < 1:
                year, month = year - 1, month + 12
            elif month > 12:
                year, month = year + 1, month - 12
            try:
                candidate = reference.replace(
                    year=year, month=month, day=day, hour=h, minute=m, second=0, microsecond=0
                )
            except ValueError:  # e.g. day 31 in a 30 day month
                continue
            if best is None or abs(candidate - reference) < abs(best - reference):
                best = candidate
        return best
    except ValueError:
        return None


def coord(value: str, deg_digits: int, hemisphere: str, extra: int | None) -> float:
    """``4702.30`` (DDMM.mm), 2, ``N`` and the extra digit of ``!Wxy!`` -> decimal degrees."""
    minutes = float(value[deg_digits:]) + (extra / 1000.0 if extra is not None else 0.0)
    if minutes >= 60.0:
        raise ValueError(f"minutes out of range: {value}")
    result = int(value[:deg_digits]) + minutes / 60.0
    return -result if hemisphere in ("S", "W") else result


def callsign_address(callsign: str) -> str:
    """``FLR112880`` -> ``112880``; anything else is cut to 8 characters."""
    tail = callsign[3:].upper()
    return tail if len(tail) == 6 and set(tail) <= HEX else callsign.upper()[:8]


def decode_id(value: str, callsign: str, tocall: str = "") -> tuple[str, int, int, bool, bool]:
    """Decode the ``id`` field into (address, address type, aircraft type, stealth, no-tracking).

    * 8 hex digits (FLARM, FANET, OGN tracker, ...): flags byte ``STttttaa`` + 24 bit address.
    * 10 hex digits (Naviter): 40 bits; stealth 39, no-track 38, type 34-37, address type 28-33,
      address 0-23.
    * Wingman: flags byte followed by the user's own text id. With a known ``tocall`` this is only
      assumed for ``OGNWMN``; without one, a hex flags byte followed by a non-hex character counts.
    * 6 hex digits (AirMate): plain address, no type.
    * Everything else (LiveTrack24, SPOT, Spider, SkyLines, ...): no type information.
    """
    v = value.upper()
    if len(v) == 8 and set(v) <= HEX:
        flags = int(v[:2], 16)
        return v[2:], flags & 0x03, (flags >> 2) & 0x0F, bool(flags & 0x80), bool(flags & 0x40)
    if len(v) == 10 and set(v) <= HEX:
        n = int(v, 16)
        return v[4:], (n >> 28) & 0x3F, (n >> 34) & 0x0F, bool((n >> 39) & 1), bool((n >> 38) & 1)
    address = callsign_address(callsign)
    if len(v) > 8 and set(v[:2]) <= HEX:
        wingman = normalize_tocall(tocall) == WINGMAN_TOCALL if tocall else v[2] not in HEX
        if wingman:
            flags = int(v[:2], 16)
            return address, flags & 0x03, (flags >> 2) & 0x0F, bool(flags & 0x80), bool(flags & 0x40)
    if len(v) == 6 and set(v) <= HEX:
        return v, 0, 0, False, False
    return address, 0, 0, False, False


def clean_text(value: str, limit: int = 40) -> str | None:
    """Make free text from the radio network safe to store and show: no control characters."""
    text = " ".join(_CONTROL_RE.sub(" ", value).split())[:limit].strip()
    return text or None


def _scan_tokens(rest: str) -> dict:
    """Decode the space separated comment tokens of a position beacon; unknown tokens are ignored."""
    fields: dict = {}
    for token in rest.split():
        if token == "relayed":
            fields["relayed"] = True
        elif token[0] == "!":
            if m := _PRECISION_RE.fullmatch(token):
                fields["precision"] = (int(m[1]), int(m[2]))
        elif token.startswith("id"):
            if m := _ID_RE.fullmatch(token):
                fields["id"] = m[1]
        elif token.startswith("gps"):
            if m := _GPS_RE.fullmatch(token):
                fields["gps"] = m[1]
        elif token.endswith("fpm"):
            if m := _CLIMB_RE.fullmatch(token):
                fields["climb"] = int(m[1])
        elif token.endswith("rot"):
            if m := _TURN_RE.fullmatch(token):
                fields["turn"] = float(m[1])
        elif token.endswith("dB"):
            if m := _SIGNAL_RE.fullmatch(token):
                fields["signal"] = float(m[1])
        elif token.endswith("kHz"):
            if m := _FREQ_RE.fullmatch(token):
                fields["freq"] = float(m[1])
        elif token.endswith("e") and (m := _ERRORS_RE.fullmatch(token)):
            fields["errors"] = int(m[1])
    return fields


def _scan_path(path: str) -> tuple[bool, bool, str | None]:
    """-> (came via a server connection, relayed, receiver) for ``OGN2FD00F*,qAS,LZHL``."""
    parts = path.split(",") if path else []
    server = any(p.startswith("TCPIP") or p == "qAC" for p in parts)
    relayed = any(p.endswith("*") and not p.startswith("TCPIP") for p in parts)
    receiver = None
    for i, part in enumerate(parts):
        if _Q_CONSTRUCT_RE.match(part) and i + 1 < len(parts):
            receiver = parts[i + 1].rstrip("*")
            break
    return server, relayed, receiver


def parse_line(line: str, reference: datetime) -> Beacon | Status | Skipped:
    """Parse one APRS line. ``reference`` is the reception time (UTC), used to complete time stamps."""
    line = line.strip()
    if not line or line[0] == "#":
        return _COMMENT
    header = HEADER_RE.match(line)
    if header is None:
        return Skipped(Skip.MALFORMED)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    callsign = header["callsign"]
    tocall = normalize_tocall(header["tocall"])
    source = source_for(tocall)
    payload = header["payload"]
    if tocall in RECEIVER_TOCALLS:
        return Skipped(Skip.RECEIVER, source)
    server_path, relayed, receiver = _scan_path(header["path"] or "")

    if payload.startswith(">"):
        if server_path:
            return Skipped(Skip.RECEIVER, source)
        return _parse_status(payload, callsign, tocall, source, reference)

    m = POSITION_RE.match(payload)
    if m is None:
        return Skipped(Skip.MALFORMED, source)
    if m["sym"] == "_":
        return Skipped(Skip.WEATHER, source)
    fields = _scan_tokens(m["rest"])
    # Ground stations and the servers' own beacons arrive over a server connection and carry no id.
    # (Some real aircraft sources, e.g. SkyBase and VarioVoice, use such a path but do send an id.)
    if server_path and "id" not in fields:
        return Skipped(Skip.RECEIVER, source)
    if m["alt"] is None:
        return Skipped(Skip.NO_ALTITUDE, source)

    if "id" in fields:
        address, address_type, reported_type, stealth, no_tracking = decode_id(fields["id"], callsign, tocall)
    elif tocall in NO_ID_SOURCES:
        address, address_type, reported_type, stealth, no_tracking = callsign_address(callsign), 0, 0, False, False
    else:
        return Skipped(Skip.NO_ID, source)
    aircraft_type = reported_type or SOURCE_DEFAULT_TYPES.get(tocall, 0)
    if tocall in ADSB_TOCALLS and aircraft_type in (6, 7):
        aircraft_type = 0  # ADS-B emitter category "ultralight / hang glider / paraglider": microlights

    if m["time"]:
        timestamp = decode_time(m["time"], m["tfmt"], reference)
        if timestamp is None:
            return Skipped(Skip.BAD_TIME, source)
    else:
        timestamp = reference.replace(microsecond=0)

    lat_extra, lon_extra = fields.get("precision", (None, None))
    try:
        lat = coord(m["lat"], 2, m["ns"], lat_extra)
        lon = coord(m["lon"], 3, m["ew"], lon_extra)
    except ValueError:
        return Skipped(Skip.BAD_POSITION, source)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return Skipped(Skip.BAD_POSITION, source)

    beacon = Beacon(
        callsign=callsign,
        tocall=tocall,
        source=source,
        receiver=receiver,
        timestamp=timestamp,
        lat=lat,
        lon=lon,
        alt_m=int(m["alt"]) * FEET_TO_M,
        address=address,
        address_type=address_type,
        aircraft_type=aircraft_type,
        reported_type=reported_type,
        stealth=stealth,
        no_tracking=no_tracking,
        relayed=relayed or fields.get("relayed", False) or receiver == "relayed",
    )
    if m["course"] is not None:
        course, knots = int(m["course"]), int(m["speed"])
        if course or knots:  # 000/000 means "no data" in OGN-flavoured APRS
            beacon.track_deg = float(course % 360)
            beacon.speed_kmh = knots * KNOTS_TO_KMH
    if "climb" in fields:
        beacon.climb_ms = fields["climb"] * FPM_TO_MS
    if "turn" in fields:
        beacon.turn_dps = fields["turn"] * ROT_TO_DPS
    if "signal" in fields:
        beacon.signal_db = fields["signal"]
    if "errors" in fields:
        beacon.errors = fields["errors"]
    if "freq" in fields:
        beacon.freq_khz = fields["freq"]
    if "gps" in fields:
        beacon.gps = fields["gps"]
    return beacon


def _parse_status(payload: str, callsign: str, tocall: str, source: str, reference: datetime) -> Status | Skipped:
    m = STATUS_RE.match(payload)
    if m is None:
        return Skipped(Skip.MALFORMED, source)
    name_match = NAME_RE.search(m["text"])
    name = clean_text(name_match[1]) if name_match else None
    if name is None:
        return Skipped(Skip.STATUS_NO_NAME, source)
    timestamp = decode_time(m["time"], m["tfmt"], reference) if m["time"] else None
    return Status(callsign, tocall, source, callsign_address(callsign), timestamp, name)
