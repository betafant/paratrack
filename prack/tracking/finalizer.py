"""Finishes closed flights in the background: terrain height under every fix, statistics, preview path."""

from __future__ import annotations

import logging
import queue
import threading

from sqlalchemy import bindparam, select, update

from ..db import Database, utcnow
from ..models import Device, Fix, Flight
from ..stats import Counters
from ..terrain import TerrainService
from ..units import ALT, CLIMB, LATLON, SPEED
from .flightstats import FixPoint, compute_stats, simplify_track
from .maintenance import delete_flight
from .rules import takeoff_speed

log = logging.getLogger(__name__)

UNFINISHED_LIMIT = 2000
SET_GROUND = (
    update(Fix)
    .where(Fix.flight_id == bindparam("fid"), Fix.ts == bindparam("t"))
    .values(ground=bindparam("g"))
    .execution_options(synchronize_session=False)
)


def _real(value: int | None, factor: int) -> float | None:
    return None if value is None else value / factor


class Finalizer:
    """Counters: ``finalizer.done``, ``finalizer.skipped`` (the flight resumed meanwhile), ``finalizer.errors``."""

    def __init__(self, db: Database, terrain: TerrainService | None, counters: Counters | None = None) -> None:
        self.db = db
        self.terrain = terrain
        self.counters = counters or Counters()
        self.queue: queue.Queue[int] = queue.Queue()
        self._queued: set[int] = set()
        self._lock = threading.Lock()

    def enqueue(self, flight_id: int) -> None:
        """Queue a flight; one that is already waiting is not queued twice."""
        with self._lock:
            if flight_id in self._queued:
                return
            self._queued.add(flight_id)
        self.queue.put(flight_id)

    def enqueue_unfinished(self) -> int:
        """Closed flights that were never finished (the program stopped before the finalizer got to them)."""
        with self.db.session() as session:
            ids = list(
                session.scalars(
                    select(Flight.id)
                    .where(Flight.status == "closed", Flight.preview.is_(None))
                    .order_by(Flight.id.desc())
                    .limit(UNFINISHED_LIMIT)
                )
            )
        for flight_id in ids:
            self.enqueue(flight_id)
        return len(ids)

    def run(self, stop: threading.Event) -> None:
        """Worker loop; after ``stop`` is set it still finishes what is queued."""
        while not stop.is_set() or not self.queue.empty():
            try:
                flight_id = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._safely(flight_id)

    def drain(self) -> int:
        """Finish everything that is queued, in the calling thread (replay, tests). Returns how many."""
        done = 0
        while True:
            try:
                flight_id = self.queue.get_nowait()
            except queue.Empty:
                return done
            self._safely(flight_id)
            done += 1

    def _safely(self, flight_id: int) -> None:
        with self._lock:
            self._queued.discard(flight_id)  # from now on a new request is a new request
        try:
            self.finalize(flight_id)
        except Exception:  # noqa: BLE001 - one bad flight must not stop the others
            self.counters.inc("finalizer.errors")
            log.exception("Finalizing flight %s failed", flight_id)

    def fill_ground(self, flight_id: int) -> int:
        """Look up the terrain height of fixes that have none. Returns how many are still missing (-1: no terrain)."""
        if self.terrain is None:
            return -1
        with self.db.session() as session:
            rows = session.execute(
                select(Fix.ts, Fix.lat, Fix.lon).where(Fix.flight_id == flight_id, Fix.ground.is_(None))
            ).all()
        if not rows:
            return 0
        heights = self.terrain.heights([(r.lat / LATLON, r.lon / LATLON) for r in rows])
        updates = [
            {"fid": flight_id, "t": r.ts, "g": round(h * ALT)}
            for r, h in zip(rows, heights, strict=True)
            if h is not None
        ]
        if updates:
            with self.db.session() as session:
                session.connection().execute(SET_GROUND, updates)
        return len(rows) - len(updates)

    def finalize(self, flight_id: int) -> None:
        with self.db.session() as session:
            flight = session.get(Flight, flight_id)
            if flight is None or flight.status != "closed":
                return
            aircraft_type = session.scalar(select(Device.aircraft_type).where(Device.id == flight.device_id)) or 0
            landing_ts = (
                int(flight.landing_time.timestamp())
                if flight.close_reason == "landed" and flight.landing_time
                else None
            )
        missing = self.fill_ground(flight_id)
        with self.db.session() as session:
            rows = session.execute(
                select(Fix.ts, Fix.lat, Fix.lon, Fix.alt, Fix.ground, Fix.speed, Fix.climb)
                .where(Fix.flight_id == flight_id)
                .order_by(Fix.ts)
            ).all()
            if not rows:  # nothing was stored: not a flight
                if session.scalar(select(Flight.status).where(Flight.id == flight_id)) == "closed":
                    delete_flight(session, flight_id)
                return
            points = [
                FixPoint(r.ts, r.lat / LATLON, r.lon / LATLON, r.alt / ALT, _real(r.ground, ALT), _real(r.speed, SPEED),
                         _real(r.climb, CLIMB))
                for r in rows
            ]  # fmt: skip
            values = compute_stats(points, takeoff_speed(aircraft_type), landing_ts)
            values.update(
                preview=simplify_track([(p.lon, p.lat, p.alt) for p in points]),
                ground_filled=missing == 0,
                updated_at=utcnow(),
            )
            # Only while the flight is still closed: it may have resumed (and be growing) in the meantime.
            result = session.execute(
                update(Flight).where(Flight.id == flight_id, Flight.status == "closed").values(**values)
            )
            self.counters.inc("finalizer.done" if result.rowcount else "finalizer.skipped")
