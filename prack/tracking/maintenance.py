"""Repairs on stored data: merging a pilot's flights that were recorded twice, purging impossible ones.

The tracker avoids both problems while running; these functions fix what older versions, restarts or changed
settings left behind. They run at start-up over the last week; ``prack repair --all`` runs them over everything.
"""

from __future__ import annotations

import bisect
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..db import Database, utcnow
from ..geo import distance_m
from ..models import Device, Fix, Flight
from ..ogn.constants import source_priority
from .rules import ALIAS_MAX_DISTANCE_M, MAX_TYPE_SPEED_KMH, SOURCE_HOLD_S, SPEED_LIMIT_COUNT

log = logging.getLogger(__name__)

OVERLAP_SLACK_S = 60  # flights this close in time can still be one flight seen over two protocols
NEAR_WINDOW_S = 180  # positions are compared this close to the middle of the overlap


def drop_device_if_unused(session: Session, device_id: int) -> None:
    session.execute(
        delete(Device).where(
            Device.id == device_id,
            ~select(Flight.id).where(Flight.device_id == device_id).exists(),
        )
    )


def delete_flight(session: Session, flight_id: int) -> None:
    """Delete a flight with its fixes, and its device when nothing else refers to it."""
    device_id = session.scalar(select(Flight.device_id).where(Flight.id == flight_id))
    session.execute(delete(Fix).where(Fix.flight_id == flight_id))
    session.execute(delete(Flight).where(Flight.id == flight_id))
    if device_id is not None:
        drop_device_if_unused(session, device_id)


def merge_flights(db: Database, keep_id: int, drop_id: int) -> int:
    """Fold flight ``drop_id`` into ``keep_id`` and return the number of copied fixes.

    Fixes of the dropped flight are copied only where the kept flight has had no data for 30 seconds, the same rule
    the tracker applies live: a lower-ranked protocol only fills gaps, otherwise the track zig-zags between two
    devices. Statistics of the kept flight are recomputed by the finalizer afterwards (the caller enqueues it).
    """
    with db.session() as session:
        keep, drop = session.get(Flight, keep_id), session.get(Flight, drop_id)
        if keep is None or drop is None or keep_id == drop_id:
            return 0
        keep_ts = sorted(session.scalars(select(Fix.ts).where(Fix.flight_id == keep_id)))
        copies = []
        for fix in session.scalars(select(Fix).where(Fix.flight_id == drop_id)):
            i = bisect.bisect_left(keep_ts, fix.ts)
            neighbours = [abs(fix.ts - keep_ts[j]) for j in (i - 1, i) if 0 <= j < len(keep_ts)]
            if not neighbours or min(neighbours) > SOURCE_HOLD_S:
                copies.append({c.name: getattr(fix, c.name) for c in Fix.__table__.columns} | {"flight_id": keep_id})
        if copies:
            session.execute(db.insert_ignore(Fix), copies)
        keep.start_time = min(keep.start_time, drop.start_time)
        keep.end_time = max(keep.end_time, drop.end_time)
        takeoffs = [t for t in (keep.takeoff_time, drop.takeoff_time) if t is not None]
        keep.takeoff_time = min(takeoffs) if takeoffs else None
        keep.fix_count = (keep.fix_count or 0) + len(copies)
        keep.updated_at = utcnow()
        device_id = drop.device_id
        session.execute(delete(Fix).where(Fix.flight_id == drop_id))
        session.execute(delete(Flight).where(Flight.id == drop_id))
        drop_device_if_unused(session, device_id)
        return len(copies)


@dataclass(slots=True)
class _Span:
    id: int
    start: int
    end: int
    source: str | None


def _nearest_fix(session: Session, flight_id: int, ts: int) -> tuple[float, float, int] | None:
    row = session.execute(
        select(Fix.lat, Fix.lon, Fix.ts).where(Fix.flight_id == flight_id).order_by(func.abs(Fix.ts - ts)).limit(1)
    ).first()
    return None if row is None else (row.lat / 1e6, row.lon / 1e6, row.ts)


def _close_together(session: Session, a: _Span, b: _Span) -> bool:
    """Were the two flights in the same place at the same time (3 km, around the middle of their overlap)?"""
    middle = (b.start + min(a.end, b.end)) // 2 if b.start <= min(a.end, b.end) else b.start
    positions = []
    for span in (a, b):
        fix = _nearest_fix(session, span.id, middle)
        if fix is None or abs(fix[2] - middle) > NEAR_WINDOW_S:
            return False
        positions.append(fix)
    return distance_m(positions[0][0], positions[0][1], positions[1][0], positions[1][1]) <= ALIAS_MAX_DISTANCE_M


def merge_duplicate_flights(
    db: Database, *, since: datetime | None = None, on_merged: Callable[[int], None] | None = None
) -> int:
    """Merge flights of one device address that overlap in time and place (one pilot, several protocols).

    The flight of the preferred protocol (FLARM > FANET > OGN tracker > ADS-L) is kept. ``on_merged`` is called
    with the id of each kept flight whose statistics need recomputing. Returns the number of flights removed.
    """
    query = (
        select(Flight.id, Flight.start_time, Flight.end_time, Flight.source, Device.address)
        .join(Device, Device.id == Flight.device_id)
        .order_by(Device.address, Flight.start_time)
    )
    if since is not None:
        query = query.where(Flight.end_time >= since)
    groups: dict[str, list[_Span]] = defaultdict(list)
    with db.session() as session:
        for fid, start, end, source, address in session.execute(query):
            groups[address].append(_Span(fid, int(start.timestamp()), int(end.timestamp()), source))

    removed = 0
    touched: set[int] = set()
    for spans in groups.values():
        gone: set[int] = set()
        for i, a in enumerate(spans):
            if a.id in gone:
                continue
            for b in spans[i + 1 :]:
                if b.start > a.end + OVERLAP_SLACK_S:
                    break  # sorted by start: nothing later can overlap a
                if b.id in gone:
                    continue
                with db.session() as session:
                    if not _close_together(session, a, b):
                        continue
                keep, drop = (a, b) if source_priority(a.source or "") <= source_priority(b.source or "") else (b, a)
                merge_flights(db, keep.id, drop.id)
                keep.start, keep.end = min(a.start, b.start), max(a.end, b.end)
                gone.add(drop.id)
                touched.add(keep.id)
                removed += 1
                if drop is a:
                    break  # a is gone; its successor in the outer loop carries on
    if removed:
        log.info("Merged %d duplicate flights (one pilot recorded over two protocols)", removed)
    if on_merged is not None:
        for flight_id in touched:
            on_merged(flight_id)
    return removed


def purge_implausible(db: Database, *, since: datetime | None = None) -> int:
    """Delete flights that cannot be what their device type says: "paragliders" that keep flying faster than a
    paraglider can, and anything recorded over ADS-B. Returns the number of deleted flights."""
    doomed: set[int] = set()
    with db.session() as session:
        for aircraft_type, limit in MAX_TYPE_SPEED_KMH.items():
            query = (
                select(Fix.flight_id)
                .join(Flight, Flight.id == Fix.flight_id)
                .join(Device, Device.id == Flight.device_id)
                .where(Device.aircraft_type == aircraft_type, Fix.speed > int(limit * 10))
                .group_by(Fix.flight_id)
                .having(func.count() >= SPEED_LIMIT_COUNT)
            )
            if since is not None:
                query = query.where(Flight.end_time >= since)
            doomed.update(session.scalars(query))
        adsb = select(Flight.id).where(Flight.source == "ADS-B")
        if since is not None:
            adsb = adsb.where(Flight.end_time >= since)
        doomed.update(session.scalars(adsb))
    for flight_id in doomed:
        with db.session() as session:
            delete_flight(session, flight_id)
    if doomed:
        log.info("Deleted %d stored flights with an implausible aircraft type", len(doomed))
    return len(doomed)


def reassign_flights(session: Session, from_device: int, to_device: int) -> None:
    session.execute(update(Flight).where(Flight.device_id == from_device).values(device_id=to_device))
