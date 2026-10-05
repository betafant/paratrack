"""Insert flights straight into the database, for tests of repairs and the finalizer."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from prack.db import Database
from prack.models import Device, Fix, Flight
from prack.ogn.constants import SOURCE_CODES
from prack.tracking.tracker import Point, fix_row

from .rig import LAT0, LON0, M_PER_DEG_LON, T0

PREFIX = {"FLARM": "FLR", "FANET": "FNT", "OGN tracker": "OGN", "OGN tracker (ADS-L)": "OGN", "ADS-B": "ICA"}


def make_flight(
    db: Database,
    *,
    address: str = "112880",
    source: str = "FLARM",
    aircraft_type: int = 7,
    start: int = T0,
    seconds: int = 300,
    step: int = 1,
    speed: float = 36.0,
    status: str = "closed",
    close_reason: str | None = "landed",
    lat: float = LAT0,
    lon: float = LON0,
    skip: tuple[int, int] | None = None,
    spikes: int = 0,
    spike_speed: float = 200.0,
    finalized: bool = False,
) -> int:
    """A straight eastbound flight of ``seconds`` with a fix every ``step`` s; ``skip`` = (from, to) offsets without
    data; the first ``spikes`` fixes get ``spike_speed``."""
    callsign = f"{PREFIX.get(source, 'XXX')}{address}"
    t_start = datetime.fromtimestamp(start, UTC)
    t_end = datetime.fromtimestamp(start + seconds, UTC)
    with db.session() as s:
        device = s.scalar(select(Device).where(Device.callsign == callsign))
        if device is None:
            device = Device(callsign=callsign, address=address, aircraft_type=aircraft_type, source=source)
            s.add(device)
            s.flush()
        flight = Flight(
            device_id=device.id,
            region="ch",
            date=date(2026, 7, 15),
            status=status,
            close_reason=close_reason,
            source=source,
            utc_offset_s=7200,
            start_time=t_start,
            end_time=t_end,
            takeoff_time=t_start,
            landing_time=t_end,
            preview=[[lon, lat, 2000]] if finalized else None,
            fix_count=0,
        )
        s.add(flight)
        s.flush()
        rows = []
        for i, offset in enumerate(range(0, seconds + 1, step)):
            if skip and skip[0] <= offset < skip[1]:
                continue
            fast = i < spikes
            p = Point(
                start + offset,
                lat,
                lon + 10.0 * offset / M_PER_DEG_LON,
                2000.0 - offset * 0.5,
                spike_speed if fast else speed,
                90.0,
                -0.5,
                None,
                SOURCE_CODES.get(source, 99),
                "Rx",
                None,
                None,
                None,
                None,
            )
            rows.append(fix_row(flight.id, p))
        s.execute(db.insert_ignore(Fix), rows)
        flight.fix_count = len(rows)
        return flight.id
