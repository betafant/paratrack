"""Turns the stream of OGN beacons into flights.

Per device (keyed by its 24 bit address) a small state machine runs:

* **not flying**: positions only go into a 60 s buffer (so the launch run is not lost)
* **take-off**: two consecutive positions at or above the take-off speed open a flight, but only inside a region
* **flying**: every position is stored, at most one per ``min_fix_interval``
* **landed**: stationary for four minutes closes the flight; its landing time is when standing still began
* **gap**: twenty minutes of silence close the flight; if the device flies again within 90 minutes it is the same
  flight (coverage holes in the Alps)

One pilot on several protocols (FLARM, FANET, OGN tracker, ADS-L) is one aircraft: the best protocol heard in the
last 30 s wins, the others only fill its gaps. Closed flights go to the finalizer for terrain height and statistics.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update

from ..config import Settings
from ..db import Database, utcnow
from ..geo import distance_m
from ..models import Device, Fix, Flight
from ..ogn.constants import source_code, source_priority
from ..ogn.ddb import DdbInfo, DeviceDatabase
from ..ogn.filters import DropReason
from ..ogn.parser import Beacon, Status
from ..regions import Region, region_for
from ..stats import Counters
from ..terrain import TerrainService
from ..units import ALT, CLIMB, FREQ, INT32, LATLON, SIGNAL, SPEED, TURN, scale
from . import rules as R
from .maintenance import delete_flight, drop_device_if_unused, merge_flights

log = logging.getLogger(__name__)

NAME_CACHE_SIZE = 4096
MAX_PENDING = 300_000  # fixes kept in memory while the database is unavailable (about an hour of a busy day)


@dataclass(slots=True)
class Point:
    """An accepted position."""

    ts: int  # epoch seconds
    lat: float
    lon: float
    alt: float  # GPS altitude MSL, m
    speed: float | None  # km/h: as reported, else derived from the track
    track: float | None
    climb: float | None
    turn: float | None
    src: int
    receiver: str | None
    signal: float | None
    errors: int | None
    freq: float | None
    gps: str | None
    ground: float | None = None


def fix_row(flight_id: int, p: Point) -> dict:
    """A ``fixes`` row: scaled integers, see ``prack/units.py``."""
    return {
        "flight_id": flight_id,
        "ts": p.ts,
        "lat": scale(p.lat, LATLON, INT32),
        "lon": scale(p.lon, LATLON, INT32),
        "alt": scale(p.alt, ALT, INT32),
        "ground": scale(p.ground, ALT, INT32),
        "speed": scale(p.speed, SPEED, INT32),
        "track": None if p.track is None else int(round(p.track)) % 360,
        "climb": scale(p.climb, CLIMB),
        "turn": scale(p.turn, TURN),
        "src": p.src,
        "receiver": p.receiver[:24] if p.receiver else None,
        "signal": scale(p.signal, SIGNAL),
        "errors": None if p.errors is None else max(0, min(32767, p.errors)),
        "freq_offset": scale(p.freq, FREQ),
        "gps": p.gps[:8] if p.gps else None,
    }


@dataclass(slots=True)
class FlightAcc:
    """Running summary of an open flight, written to the flights table every few seconds."""

    flight_id: int
    region: str
    start_ts: int
    end_ts: int
    takeoff_ts: int | None = None
    fix_count: int = 0
    max_alt: float = -1e9
    min_alt: float = 1e9
    max_speed: float = 0.0
    distance_m: float = 0.0
    takeoff: tuple[float, float, float] | None = None
    last_pos: tuple[float, float] | None = None
    last_agl: float | None = None
    bbox: list[float] = field(default_factory=lambda: [90.0, -90.0, 180.0, -180.0])  # min/max lat, min/max lon
    dirty: bool = True

    def add(self, p: Point) -> None:
        self.fix_count += 1
        self.end_ts = max(self.end_ts, p.ts)
        self.max_alt, self.min_alt = max(self.max_alt, p.alt), min(self.min_alt, p.alt)
        if p.speed is not None and p.speed < 450.0:
            self.max_speed = max(self.max_speed, p.speed)
        if self.takeoff is None:
            self.takeoff = (p.lat, p.lon, p.alt)
        if self.last_pos is not None:
            self.distance_m += distance_m(self.last_pos[0], self.last_pos[1], p.lat, p.lon)
        self.last_pos = (p.lat, p.lon)
        self.last_agl = None if p.ground is None else p.alt - p.ground
        b = self.bbox
        self.bbox = [min(b[0], p.lat), max(b[1], p.lat), min(b[2], p.lon), max(b[3], p.lon)]
        self.dirty = True

    def values(self) -> dict:
        v: dict = {
            "end_time": datetime.fromtimestamp(self.end_ts, UTC),
            "fix_count": self.fix_count,
            "updated_at": utcnow(),
        }
        if self.fix_count:
            v.update(
                max_alt=self.max_alt,
                min_alt=self.min_alt,
                max_speed=round(self.max_speed, 1),
                distance_km=round(self.distance_m / 1000, 3),
                min_lat=self.bbox[0],
                max_lat=self.bbox[1],
                min_lon=self.bbox[2],
                max_lon=self.bbox[3],
            )
        if self.takeoff:
            v.update(takeoff_lat=self.takeoff[0], takeoff_lon=self.takeoff[1], takeoff_alt=self.takeoff[2])
        if self.last_pos:
            v.update(landing_lat=self.last_pos[0], landing_lon=self.last_pos[1])
        return v


@dataclass
class DeviceState:
    ident: str  # callsign of the preferred protocol, e.g. FLR112880; the key in Tracker.states
    device_id: int
    address: str
    aircraft_type: int
    source: str  # preferred protocol seen so far
    registration: str | None = None
    competition_id: str | None = None
    model: str | None = None
    pilot_name: str | None = None
    last: Point | None = None
    ground: float | None = None
    flight: FlightAcc | None = None
    flying_outside: bool = False  # airborne since a take-off outside every region: not recorded
    in_region: bool = False
    last_stored: int | None = None
    pre_buffer: deque[Point] = field(default_factory=deque)
    recent: deque[tuple[int, float, float]] = field(default_factory=lambda: deque(maxlen=40))
    moving: int = 0
    stationary_since: int | None = None
    stationary_anchor: tuple[float, float] | None = None
    rejected: int = 0
    fast: deque[bool] = field(default_factory=lambda: deque(maxlen=R.SPEED_WINDOW))
    misclassified: bool = False
    source_seen: dict[str, int] = field(default_factory=dict)
    prev_flight_id: int | None = None  # last flight closed as "gap": it may resume
    prev_end: int | None = None
    prev_pos: tuple[float, float] | None = None
    prev_resumable: bool = True
    trail: deque[tuple] = field(default_factory=lambda: deque(maxlen=R.TRAIL_POINTS))
    seq: int = 0
    live: bool = False

    def display_name(self) -> str:
        if self.pilot_name:
            return self.pilot_name
        if self.competition_id and self.registration:
            return f"{self.competition_id} {self.registration}"
        return self.registration or self.competition_id or self.ident


class Tracker:
    """All methods that change state take ``lock``; ``process_*`` run on one consumer thread, ``flush`` and
    ``sweep`` on a writer thread, ``live`` on web request threads."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        regions: list[Region],
        *,
        ddb: DeviceDatabase | None = None,
        terrain: TerrainService | None = None,
        counters: Counters | None = None,
        on_flight_closed: Callable[[int], None] | None = None,
    ) -> None:
        self.db = db
        self.regions = regions
        self.ddb = ddb
        self.terrain = terrain
        self.counters = counters or Counters()
        self.on_flight_closed = on_flight_closed
        self.min_interval = settings.min_fix_interval
        self.lock = threading.RLock()
        self.states: dict[str, DeviceState] = {}
        self.by_address: dict[str, str] = {}
        self.names: OrderedDict[str, str] = OrderedDict()  # FANET names heard before the device was tracked
        self.pending: list[tuple[int, Point]] = []
        self.seq = 0
        self.removed: deque[tuple[int, str]] = deque(maxlen=5000)
        self._last_summary = 0.0

    # ------------------------------------------------------------------ input

    def _drop(self, reason: DropReason) -> None:
        self.counters.inc(f"drop.{reason.value}")

    def process_status(self, status: Status) -> None:
        """A pilot name (FANET). Kept for devices that are or become tracked; never stored for others."""
        with self.lock:
            self.names[status.address] = status.name
            self.names.move_to_end(status.address)
            while len(self.names) > NAME_CACHE_SIZE:
                self.names.popitem(last=False)
            ident = self.by_address.get(status.address)
            state = self.states.get(ident) if ident else None
            if state is None or state.pilot_name == status.name:
                return
            state.pilot_name = status.name
            self._touch(state)
            self._store_pilot_name(state)

    def process_beacon(self, b: Beacon, received_at: datetime) -> None:
        """A position that passed the stateless filters (type, privacy)."""
        ts = b.epoch
        received = int(received_at.timestamp())
        if ts - received > R.FUTURE_S:
            return self._drop(DropReason.FUTURE)
        if received - ts > R.STALE_S:
            return self._drop(DropReason.STALE)
        info = self.ddb.lookup(b.address) if self.ddb is not None else None
        with self.lock:
            ident = b.ident
            state = self.states.get(ident)
            peer = self._peer(b, ts)
            if peer is not None and peer is not state:
                state = self._unify(state, peer, b, info)
            if state is None:
                state = self._create_state(b, info)
            if state.misclassified:
                return self._drop(DropReason.MISCONFIGURED)
            self._handle(state, b, ts)

    # ------------------------------------------------------------------ one aircraft, several protocols

    def _peer(self, b: Beacon, ts: int) -> DeviceState | None:
        """The state of the same address under another callsign, if it was heard close by a moment ago."""
        ident = self.by_address.get(b.address)
        if ident is None or ident == b.ident:
            return None
        other = self.states.get(ident)
        if other is None:
            return None
        if other.last is not None:
            when, lat, lon = other.last.ts, other.last.lat, other.last.lon
        elif other.prev_end is not None and other.prev_pos is not None:  # restored after a restart
            when, (lat, lon) = other.prev_end, other.prev_pos
        else:
            return None
        if abs(ts - when) > R.ALIAS_MAX_AGE_S or distance_m(lat, lon, b.lat, b.lon) > R.ALIAS_MAX_DISTANCE_M:
            return None
        return other

    def _unify(self, state: DeviceState | None, peer: DeviceState, b: Beacon, info: DdbInfo | None) -> DeviceState:
        """Route a beacon of an aircraft known under two callsigns to one state with the best identity."""
        self.counters.inc("identity.routed")
        if state is None:
            if source_priority(b.source) < source_priority(peer.source):
                self._adopt_identity(peer, b, info)
            return peer
        primary, secondary = (state, peer) if self._preferred(state, peer) else (peer, state)
        self._absorb(primary, secondary)
        return primary

    @staticmethod
    def _preferred(a: DeviceState, b: DeviceState) -> bool:
        pa, pb = source_priority(a.source), source_priority(b.source)
        if pa != pb:
            return pa < pb
        start_a = a.flight.start_ts if a.flight else 1 << 62
        start_b = b.flight.start_ts if b.flight else 1 << 62
        return start_a <= start_b

    def _adopt_identity(self, state: DeviceState, b: Beacon, info: DdbInfo | None) -> None:
        """The better protocol showed up: from now on the aircraft is stored and shown under its callsign."""
        old, new = state.ident, b.ident
        log.info("%s is the same aircraft as %s, continuing as %s", new, old, new)
        self.flush(force=True)  # fixes still pending belong to the flight, not to a callsign
        state.device_id = self._rename_device(state, new, b, info)
        state.ident, state.source = new, b.source
        if state.flight is not None:
            with self.db.session() as session:
                session.execute(
                    update(Flight)
                    .where(Flight.id == state.flight.flight_id)
                    .values(device_id=state.device_id, source=state.source)
                )
        self.states.pop(old, None)
        self.states[new] = state
        self.by_address[state.address] = new
        self._removed(old)
        self._touch(state)
        self.counters.inc("identity.switched")

    def _absorb(self, primary: DeviceState, secondary: DeviceState) -> None:
        """Two states turned out to be one aircraft: keep one state and one flight."""
        log.info("%s and %s are the same aircraft, keeping %s", primary.ident, secondary.ident, primary.ident)
        self.flush(force=True)
        if secondary.pilot_name and not primary.pilot_name:
            primary.pilot_name = secondary.pilot_name
            self._store_pilot_name(primary)
        for source, seen in secondary.source_seen.items():
            primary.source_seen[source] = max(seen, primary.source_seen.get(source, seen))
        sec_flight = secondary.flight
        if sec_flight is not None:
            if primary.flight is None:
                primary.flight, primary.last_stored = sec_flight, secondary.last_stored
                with self.db.session() as session:
                    session.execute(
                        update(Flight)
                        .where(Flight.id == sec_flight.flight_id)
                        .values(device_id=primary.device_id, source=primary.source)
                    )
            else:
                added = merge_flights(self.db, primary.flight.flight_id, sec_flight.flight_id)
                acc = primary.flight
                acc.start_ts, acc.end_ts = min(acc.start_ts, sec_flight.start_ts), max(acc.end_ts, sec_flight.end_ts)
                acc.fix_count += added
                acc.dirty = True
        if secondary.device_id != primary.device_id:
            with self.db.session() as session:
                session.execute(
                    update(Flight).where(Flight.device_id == secondary.device_id).values(device_id=primary.device_id)
                )
                drop_device_if_unused(session, secondary.device_id)
        self.states.pop(secondary.ident, None)
        if secondary.live:
            self._removed(secondary.ident)
        self.by_address[primary.address] = primary.ident
        self._touch(primary)
        self.counters.inc("identity.merged")

    # ------------------------------------------------------------------ device rows

    @staticmethod
    def _identity(info: DdbInfo | None) -> tuple[str | None, str | None, str | None]:
        """Registration and competition number only for owners who agreed to be identified."""
        if info is None:
            return None, None, None
        return info.shown_registration, info.shown_competition_id, info.model

    def _fill_device(self, device: Device, b: Beacon, info: DdbInfo | None) -> None:
        device.address_type, device.aircraft_type, device.source = b.address_type, b.aircraft_type, b.source
        device.last_seen = utcnow()
        if info is not None:
            device.registration, device.competition_id, device.model = self._identity(info)

    def _ensure_device(self, b: Beacon, info: DdbInfo | None) -> tuple[int, str | None]:
        with self.db.session() as session:
            device = session.scalar(select(Device).where(Device.callsign == b.ident))
            if device is None:
                device = Device(callsign=b.ident, address=b.address, first_seen=utcnow())
                session.add(device)
            self._fill_device(device, b, info)
            session.flush()
            return device.id, device.pilot_name

    def _rename_device(self, state: DeviceState, new_ident: str, b: Beacon, info: DdbInfo | None) -> int:
        """Give the device row the callsign of the better protocol. Returns the device id to use from now on."""
        with self.db.session() as session:
            current = session.get(Device, state.device_id)
            existing = session.scalar(select(Device).where(Device.callsign == new_ident, Device.id != state.device_id))
            if existing is None:
                if current is None:  # the row vanished (repair job): start a new one
                    current = Device(callsign=new_ident, address=state.address, first_seen=utcnow())
                    session.add(current)
                current.callsign = new_ident
                target = current
            else:  # earlier flights exist under the better callsign: move everything there
                if current is not None:
                    session.execute(update(Flight).where(Flight.device_id == current.id).values(device_id=existing.id))
                    existing.pilot_name = existing.pilot_name or current.pilot_name
                    session.delete(current)
                target = existing
            if state.pilot_name:
                target.pilot_name = state.pilot_name
            self._fill_device(target, b, info)
            session.flush()
            return target.id

    def _store_pilot_name(self, state: DeviceState) -> None:
        with self.db.session() as session:
            session.execute(update(Device).where(Device.id == state.device_id).values(pilot_name=state.pilot_name))

    def _create_state(self, b: Beacon, info: DdbInfo | None) -> DeviceState:
        device_id, stored_name = self._ensure_device(b, info)
        registration, competition_id, model = self._identity(info)
        state = DeviceState(
            ident=b.ident,
            device_id=device_id,
            address=b.address,
            aircraft_type=b.aircraft_type,
            source=b.source,
            registration=registration,
            competition_id=competition_id,
            model=model,
            pilot_name=stored_name or self.names.get(b.address),
        )
        if state.pilot_name and not stored_name:
            self._store_pilot_name(state)
        self.states[b.ident] = state
        self.by_address[b.address] = b.ident
        return state

    # ------------------------------------------------------------------ state machine

    def _derive_speed(self, state: DeviceState, ts: int, lat: float, lon: float) -> float | None:
        """Ground speed from the track, over about 5 s: positions are quantised to 1.85 m, so one second of
        a standing aircraft would otherwise look like a walk."""
        base = None
        for t, la, lo in reversed(state.recent):
            if ts - t > 30:
                break
            base = (t, la, lo)
            if ts - t >= 5:
                break
        if base is None or ts - base[0] < 1:
            return None
        return distance_m(base[1], base[2], lat, lon) / (ts - base[0]) * 3.6

    def _handle(self, state: DeviceState, b: Beacon, ts: int) -> None:
        # One device on several protocols: use the best one heard in the last 30 s; the others fill gaps only.
        state.source_seen[b.source] = max(ts, state.source_seen.get(b.source, ts))
        best = min(source_priority(s) for s, seen in state.source_seen.items() if abs(ts - seen) <= R.SOURCE_HOLD_S)
        if source_priority(b.source) > best:
            return self._drop(DropReason.LOWER_SOURCE)

        last = state.last
        if last is not None:
            if ts <= last.ts:
                return self._drop(DropReason.OUT_OF_ORDER)
            d = distance_m(last.lat, last.lon, b.lat, b.lon)
            if d > R.GLITCH_MIN_DISTANCE_M and d / (ts - last.ts) * 3.6 > R.GLITCH_MIN_SPEED_KMH:
                if state.rejected < R.GLITCH_MAX_IN_A_ROW:
                    state.rejected += 1
                    return self._drop(DropReason.GLITCH)
                state.recent.clear()  # it really moved there: start measuring from the new place
                state.fast.clear()
        state.rejected = 0

        speed = b.speed_kmh if b.speed_kmh is not None else self._derive_speed(state, ts, b.lat, b.lon)
        limit = R.MAX_TYPE_SPEED_KMH.get(b.aircraft_type)
        if limit is not None and speed is not None:
            too_fast = speed > limit
            state.fast.append(too_fast)
            if too_fast:
                if sum(state.fast) >= R.SPEED_LIMIT_COUNT:
                    return self._disqualify(state, b, speed)
                return self._drop(DropReason.SPIKE)

        ground = self.terrain.height_cached(b.lat, b.lon) if self.terrain is not None else None
        p = Point(
            ts,
            b.lat,
            b.lon,
            b.alt_m,
            speed,
            b.track_deg,
            b.climb_ms,
            b.turn_dps,
            source_code(b.source),
            b.receiver,
            b.signal_db,
            b.errors,
            b.freq_khz,
            b.gps,
            ground,
        )
        previous_ts = last.ts if last is not None else None
        was_visible = self._visible(state)
        state.last, state.ground = p, ground
        state.aircraft_type = b.aircraft_type
        state.recent.append((ts, b.lat, b.lon))
        state.in_region = region_for(self.regions, b.lat, b.lon) is not None
        state.live = True
        if was_visible and not self._visible(state):
            self._removed(state.ident)  # left every region without a flight: gone from the live view
        self._touch(state)
        state.trail.append((state.seq, ts, b.lat, b.lon, b.alt_m, speed, b.climb_ms, b.track_deg, ground))

        if state.flight is not None and ts - state.flight.end_ts > R.GAP_S:
            self._close(state, "gap", state.flight.end_ts)
        if state.flying_outside and previous_ts is not None and ts - previous_ts > R.GAP_S:
            state.flying_outside = False

        if state.flight is None and not state.flying_outside:
            self._maybe_takeoff(state, p)
        else:
            if state.flight is not None:
                self._store(state, p)
            self._check_landing(state, p)

    def _disqualify(self, state: DeviceState, b: Beacon, speed: float) -> None:
        """Faster than its declared type can fly, again and again: a mis-configured device."""
        log.info(
            "%s says it is aircraft type %d but flies %.0f km/h: ignored from now on",
            state.ident,
            b.aircraft_type,
            speed,
        )
        self._drop(DropReason.MISCONFIGURED)
        state.misclassified = True
        if state.flight is not None:
            self._discard_flight(state.flight.flight_id)
            state.flight = None
        state.prev_flight_id = None
        if state.live:
            state.live = False
            self._removed(state.ident)

    def _discard_flight(self, flight_id: int) -> None:
        self.pending = [(fid, p) for fid, p in self.pending if fid != flight_id]
        with self.db.session() as session:
            delete_flight(session, flight_id)
        self.counters.inc("flights.discarded")

    def _maybe_takeoff(self, state: DeviceState, p: Point) -> None:
        buf = state.pre_buffer
        buf.append(p)
        while buf and p.ts - buf[0].ts > R.PRE_TAKEOFF_S:
            buf.popleft()
        if (p.speed or 0.0) >= R.takeoff_speed(state.aircraft_type):
            state.moving += 1
        else:
            state.moving = 0
            return
        if state.moving < R.TAKEOFF_FIXES:
            return
        if (
            state.prev_flight_id is not None
            and state.prev_resumable
            and state.prev_end is not None
            and p.ts - state.prev_end <= R.RESUME_S
        ):
            return self._resume(state, p)
        region = region_for(self.regions, p.lat, p.lon)
        if region is None:  # a flight may only start inside a region; this one is already under way
            state.flying_outside = True
            state.pre_buffer.clear()
            state.stationary_since = None
            self._drop(DropReason.OUTSIDE_REGION)
            return
        self._open(state, region, p)

    def _open(self, state: DeviceState, region: Region, p: Point) -> None:
        buffered = list(state.pre_buffer)
        start = buffered[0].ts if buffered else p.ts
        threshold = R.takeoff_speed(state.aircraft_type)
        takeoff = next((q.ts for q in buffered if (q.speed or 0.0) >= threshold), p.ts)  # as the finalizer defines it
        started = datetime.fromtimestamp(start, UTC)
        local = started.astimezone(region.tz)
        now = utcnow()
        with self.db.session() as session:
            flight = Flight(
                device_id=state.device_id,
                region=region.id,
                date=local.date(),
                status="active",
                source=state.source,
                utc_offset_s=int(local.utcoffset().total_seconds()),
                start_time=started,
                end_time=started,
                takeoff_time=datetime.fromtimestamp(takeoff, UTC),
                created_at=now,
                updated_at=now,
            )
            session.add(flight)
            session.flush()
            flight_id = flight.id
        state.flight = FlightAcc(flight_id, region.id, start, start, takeoff)
        state.last_stored = None
        state.stationary_since = None
        state.pre_buffer.clear()
        self.counters.inc("flights.opened")
        for q in buffered:
            self._store(state, q)

    def _resume(self, state: DeviceState, p: Point) -> None:
        flight_id = state.prev_flight_id
        self.flush(force=True)
        with self.db.session() as session:
            flight = session.get(Flight, flight_id)
            if flight is None:
                state.prev_flight_id = None
                return
            flight.status, flight.close_reason, flight.landing_time, flight.updated_at = "active", None, None, utcnow()
            acc = FlightAcc(
                flight.id,
                flight.region,
                int(flight.start_time.timestamp()),
                int(flight.end_time.timestamp()),
                int(flight.takeoff_time.timestamp()) if flight.takeoff_time else None,
                fix_count=flight.fix_count or 0,
                max_alt=flight.max_alt if flight.max_alt is not None else -1e9,
                min_alt=flight.min_alt if flight.min_alt is not None else 1e9,
                max_speed=flight.max_speed or 0.0,
                distance_m=(flight.distance_km or 0.0) * 1000,
                takeoff=(flight.takeoff_lat, flight.takeoff_lon, flight.takeoff_alt or 0.0)
                if flight.takeoff_lat is not None
                else None,
                last_pos=(flight.landing_lat, flight.landing_lon) if flight.landing_lat is not None else None,
            )
            if flight.min_lat is not None:
                acc.bbox = [flight.min_lat, flight.max_lat, flight.min_lon, flight.max_lon]
        after_gap = [q for q in state.pre_buffer if q.ts > acc.end_ts]
        state.flight, state.prev_flight_id, state.last_stored = acc, None, None
        state.stationary_since = None
        state.pre_buffer.clear()
        self.counters.inc("flights.resumed")
        for q in after_gap or [p]:
            self._store(state, q)

    def _store(self, state: DeviceState, p: Point) -> None:
        acc = state.flight
        if acc is None:
            return
        if state.last_stored is not None and p.ts - state.last_stored < self.min_interval:
            acc.end_ts = max(acc.end_ts, p.ts)
            return
        state.last_stored = p.ts
        acc.add(p)
        self.pending.append((acc.flight_id, p))

    def _check_landing(self, state: DeviceState, p: Point) -> None:
        if p.speed is None:
            return
        if p.speed >= R.STATIONARY_SPEED_KMH:
            state.stationary_since = None
            return
        anchor = state.stationary_anchor
        if (
            state.stationary_since is None
            or anchor is None
            or distance_m(anchor[0], anchor[1], p.lat, p.lon) > R.STATIONARY_RADIUS_M
        ):
            state.stationary_since, state.stationary_anchor = p.ts, (p.lat, p.lon)
            return
        if p.ts - state.stationary_since < R.LANDING_S:
            return
        if p.ground is not None and p.alt - p.ground > R.LANDED_MAX_AGL_M:
            return  # hovering in strong wind, not landed
        if p.ground is None and p.climb is not None and abs(p.climb) > R.LANDED_MAX_VARIO_MS:
            return
        landed_at = state.stationary_since
        if state.flight is not None:
            self._close(state, "landed", landed_at)
        else:
            state.flying_outside = False  # an unrecorded flight outside the regions ends
            state.moving, state.stationary_since = 0, None

    def _close(self, state: DeviceState, reason: str, landing_ts: int) -> None:
        acc = state.flight
        if acc is None:
            return
        self.flush(force=True)
        values = acc.values()
        values.update(status="closed", close_reason=reason, landing_time=datetime.fromtimestamp(landing_ts, UTC))
        with self.db.session() as session:
            session.execute(update(Flight).where(Flight.id == acc.flight_id).values(**values))
            session.execute(update(Device).where(Device.id == state.device_id).values(last_seen=utcnow()))
        if reason == "gap":  # it may come back (coverage hole), unless it was near the ground when it vanished
            state.prev_flight_id, state.prev_end, state.prev_pos = acc.flight_id, acc.end_ts, acc.last_pos
            state.prev_resumable = acc.last_agl is None or acc.last_agl >= R.RESUME_MIN_AGL_M
        else:
            state.prev_flight_id = None
        state.flight = None
        state.moving, state.stationary_since = 0, None
        state.pre_buffer.clear()
        self.counters.inc("flights.closed")
        self._touch(state)
        if self.on_flight_closed is not None:
            self.on_flight_closed(acc.flight_id)

    # ------------------------------------------------------------------ persistence and housekeeping

    def flush(self, force: bool = False) -> None:
        """Write buffered fixes, and every few seconds the summaries of open flights.

        When the database is unavailable the fixes stay queued (up to ``MAX_PENDING``) and the error is raised.
        """
        with self.lock:
            taken, self.pending = self.pending, []
            summaries: list[tuple[FlightAcc, dict]] = []
            now = utcnow().timestamp()
            if force or now - self._last_summary >= R.FLIGHT_UPDATE_S:
                self._last_summary = now
                for state in self.states.values():
                    acc = state.flight
                    if acc is not None and acc.dirty:
                        summaries.append((acc, acc.values()))
                        acc.dirty = False
            rows = [fix_row(fid, p) for fid, p in taken]
        if not rows and not summaries:
            return
        try:
            with self.db.session() as session:
                if rows:
                    session.execute(self.db.insert_ignore(Fix), rows)
                for acc, values in summaries:
                    session.execute(update(Flight).where(Flight.id == acc.flight_id).values(**values))
        except Exception:
            with self.lock:
                queued = taken + self.pending
                if len(queued) > MAX_PENDING:
                    self.counters.inc("fixes.lost", len(queued) - MAX_PENDING)
                self.pending = queued[-MAX_PENDING:]
                for acc, _ in summaries:
                    acc.dirty = True
            raise
        self.counters.inc("fixes.stored", len(rows))

    def sweep(self, now: datetime | None = None) -> None:
        """Close flights that went silent, retire aircraft from the live view, forget old states."""
        now_ts = int((now or utcnow()).timestamp())
        with self.lock:
            for ident, state in list(self.states.items()):
                if state.flight is not None and now_ts - state.flight.end_ts > R.GAP_S:
                    self._close(state, "gap", state.flight.end_ts)
                last_ts = state.last.ts if state.last else None
                if state.live and (last_ts is None or now_ts - last_ts > R.LIVE_WINDOW_S):
                    state.live = False
                    self._removed(ident)
                if state.flying_outside and last_ts is not None and now_ts - last_ts > R.GAP_S:
                    state.flying_outside = False
                idle_since = last_ts or state.prev_end
                if state.flight is None and (idle_since is None or now_ts - idle_since > R.RESUME_S + 3600):
                    del self.states[ident]
                    if self.by_address.get(state.address) == ident:
                        del self.by_address[state.address]

    def restore(self) -> list[int]:
        """Close flights left open by a previous run (as "gap"); they resume if the pilot is still flying."""
        closed: list[int] = []
        with self.lock, self.db.session() as session:
            rows = session.execute(
                select(Flight, Device).join(Device, Flight.device_id == Device.id).where(Flight.status == "active")
            ).all()
            for flight, device in rows:
                last = session.execute(
                    select(Fix.alt, Fix.ground).where(Fix.flight_id == flight.id).order_by(Fix.ts.desc()).limit(1)
                ).first()
                agl = None if last is None or last.ground is None else (last.alt - last.ground) / ALT
                flight.status, flight.close_reason, flight.landing_time = "closed", "gap", flight.end_time
                closed.append(flight.id)
                self.states[device.callsign] = DeviceState(
                    ident=device.callsign,
                    device_id=device.id,
                    address=device.address,
                    aircraft_type=device.aircraft_type,
                    source=device.source or "",
                    registration=device.registration,
                    competition_id=device.competition_id,
                    model=device.model,
                    pilot_name=device.pilot_name,
                    prev_flight_id=flight.id,
                    prev_end=int(flight.end_time.timestamp()),
                    prev_pos=(flight.landing_lat, flight.landing_lon) if flight.landing_lat is not None else None,
                    prev_resumable=agl is None or agl >= R.RESUME_MIN_AGL_M,
                )
                self.by_address[device.address] = device.callsign
        if self.on_flight_closed is not None:
            for flight_id in closed:
                self.on_flight_closed(flight_id)
        return closed

    def close_all(self) -> None:
        """Close every open flight (replay and tests); a normal shutdown leaves them open for the next start."""
        with self.lock:
            for state in list(self.states.values()):
                if state.flight is not None:
                    self._close(state, "gap", state.flight.end_ts)
        self.flush(force=True)

    # ------------------------------------------------------------------ live view

    def _touch(self, state: DeviceState) -> None:
        self.seq += 1
        state.seq = self.seq

    def _removed(self, ident: str) -> None:
        self.seq += 1
        self.removed.append((self.seq, ident))

    @staticmethod
    def _pt(p: tuple) -> list:
        """A trail entry as the browser wants it: ``[t, lon, lat, alt, speed, vario, heading, ground]``."""
        _, ts, lat, lon, alt, speed, climb, track, ground = p
        return [
            ts,
            round(lon, 6),
            round(lat, 6),
            round(alt),
            None if speed is None else round(speed, 1),
            None if climb is None else round(climb, 1),
            None if track is None else round(track),
            None if ground is None else round(ground),
        ]

    def _visible(self, state: DeviceState) -> bool:
        """Aircraft outside every region (heard in the margin of the feed) are not shown, unless recorded."""
        return state.last is not None and (state.flight is not None or state.in_region)

    def live(self, since: int | None = None) -> dict:
        """Aircraft that changed after sequence number ``since``, and the ids that went away.

        ``pts`` has every position received since then (full resolution, for the browser's live tracks). Without
        ``since`` the message is a full snapshot: every live aircraft, with its recent trail as ``trail``
        (``[t, lon, lat, alt]``, every third point) instead of ``pts``.
        """
        full = since is None
        since = since or 0
        pending: list[tuple[dict, list[tuple]]] = []
        with self.lock:  # copy under the lock, format outside it: the tracker must not wait for the browser
            for s in self.states.values():
                p = s.last
                if not (s.live and p is not None and s.seq > since and self._visible(s)):
                    continue
                entry = {
                    "id": s.ident,
                    "address": s.address,
                    "name": s.display_name(),
                    "pilot": s.pilot_name,
                    "reg": s.registration,
                    "cn": s.competition_id,
                    "src": s.source,
                    "flight_id": s.flight.flight_id if s.flight else None,
                    "flying": s.flight is not None,
                    "takeoff": (s.flight.takeoff_ts or s.flight.start_ts) if s.flight else None,
                    "t": p.ts,
                    "lat": round(p.lat, 6),
                    "lon": round(p.lon, 6),
                    "alt": round(p.alt),
                    "gnd": None if s.ground is None else round(s.ground),
                    "spd": None if p.speed is None else round(p.speed, 1),
                    "vs": None if p.climb is None else round(p.climb, 1),
                    "hdg": None if p.track is None else round(p.track),
                }
                if full:
                    raw = list(s.trail)[::-3][::-1]  # every third point, newest included
                else:
                    raw = []
                    for q in reversed(s.trail):
                        if q[0] <= since:
                            break
                        raw.append(q)
                    raw.reverse()
                pending.append((entry, raw))
            removed = [] if full else [ident for seq, ident in self.removed if seq > since]
            seq = self.seq
        for entry, raw in pending:
            if full:
                entry["trail"] = [[q[1], round(q[3], 6), round(q[2], 6), round(q[4])] for q in raw]
            else:
                entry["pts"] = [self._pt(q) for q in raw]
        return {
            "seq": seq,
            "now": int(utcnow().timestamp()),
            "full": full,
            "aircraft": [entry for entry, _ in pending],
            "removed": removed,
        }

    def summary(self) -> dict:
        """Numbers for ``/api/status``."""
        with self.lock:
            return {
                "devices": len(self.states),
                "live": sum(1 for s in self.states.values() if s.live and self._visible(s)),
                "flying": sum(1 for s in self.states.values() if s.flight is not None),
                "pending_fixes": len(self.pending),
            }
