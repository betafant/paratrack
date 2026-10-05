"""A test rig: tracker, finalizer and a scratch database, fed with synthetic beacons at controlled times."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, text

from prack.config import Settings
from prack.db import Database
from prack.models import Base, Device, Fix, Flight
from prack.ogn.constants import SOURCE_CODES
from prack.ogn.filters import FilterPolicy
from prack.ogn.parser import Beacon
from prack.ogn.pipeline import LineClassifier
from prack.ogn.survey import Survey
from prack.regions import load_regions
from prack.stats import Counters
from prack.tracking.finalizer import Finalizer
from prack.tracking.tracker import Tracker
from prack.views import VIEWS

T0 = int(datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC).timestamp())  # 12:00 local time in Switzerland
LAT0, LON0 = 46.6453, 7.6511  # near Niesen, inside the Swiss box
M_PER_DEG_LON = 111_195.0 * 0.6866  # at 46.6 degrees north
M_PER_DEG_LAT = 111_195.0
PREFIX = {"FLARM": "FLR", "FANET": "FNT", "OGN tracker": "OGN", "OGN tracker (ADS-L)": "OGN"}
assert set(PREFIX) <= set(SOURCE_CODES)


PG_URL = os.environ.get("PRACK_TEST_PG_URL")  # set it to run the whole tracker suite on PostgreSQL as well


def scratch_database(tmp_path: Path, name: str = "rig.db") -> Database:
    """SQLite in the test's directory, or an emptied PostgreSQL database when PRACK_TEST_PG_URL is set."""
    if not PG_URL:
        database = Database(f"sqlite:///{tmp_path / name}")
    else:
        database = Database(PG_URL)
        with database.engine.begin() as conn:
            for view in VIEWS:
                conn.execute(text(f"DROP VIEW IF EXISTS {view}"))
        Base.metadata.drop_all(database.engine)
    database.init()
    return database


def beacon(
    ts: int,
    lat: float = LAT0,
    lon: float = LON0,
    alt: float = 2000.0,
    speed: float | None = 0.0,
    *,
    address: str = "112880",
    source: str = "FLARM",
    aircraft_type: int = 7,
    climb: float | None = 0.0,
    track: float | None = 90.0,
    receiver: str = "Rx",
    stealth: bool = False,
    no_tracking: bool = False,
) -> Beacon:
    prefix = PREFIX.get(source, "XXX")
    return Beacon(
        callsign=f"{prefix}{address}", tocall="OGFLR", source=source, receiver=receiver,
        timestamp=datetime.fromtimestamp(ts, UTC), lat=lat, lon=lon, alt_m=alt, address=address, address_type=2,
        aircraft_type=aircraft_type, reported_type=aircraft_type, stealth=stealth, no_tracking=no_tracking,
        track_deg=track, speed_kmh=speed, climb_ms=climb, turn_dps=None, signal_db=12.5, errors=0, freq_khz=1.0,
        gps="2x3",
    )  # fmt: skip


class FlatTerrain:
    """Terrain stand-in: ``ground`` metres everywhere, or ``fn(lat, lon)``."""

    def __init__(self, ground: float = 1000.0, fn=None) -> None:
        self.fn = fn or (lambda lat, lon: ground)

    def height_cached(self, lat: float, lon: float) -> float | None:
        return self.fn(lat, lon)

    def heights(self, points: list[tuple[float, float]]) -> list[float | None]:
        return [self.fn(lat, lon) for lat, lon in points]


@dataclass
class Rig:
    tmp_path: Path
    terrain: object | None = None
    ddb: object | None = None
    region_ids: tuple[str, ...] = ("ch",)
    min_fix_interval: float = 1.0
    db: Database = field(init=False)
    counters: Counters = field(init=False)
    closed: list[int] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.db = scratch_database(self.tmp_path)
        self.counters = Counters()
        self.settings = Settings(data_dir=self.tmp_path, min_fix_interval=self.min_fix_interval)
        self.tracker = self.make_tracker()
        self.finalizer = Finalizer(self.db, self.terrain, self.counters)
        self.survey = Survey()
        self.classifier = LineClassifier(FilterPolicy(), self.ddb, self.counters, self.survey)

    def make_tracker(self) -> Tracker:
        return Tracker(
            self.db,
            self.settings,
            load_regions(list(self.region_ids)),
            ddb=self.ddb,
            terrain=self.terrain,
            counters=self.counters,
            on_flight_closed=self.on_closed,
        )

    def on_closed(self, flight_id: int) -> None:
        self.closed.append(flight_id)
        self.finalizer.enqueue(flight_id)

    def restart(self) -> list[int]:
        """A new process: a fresh tracker on the same database."""
        self.tracker.flush(force=True)
        self.tracker = self.make_tracker()
        return self.tracker.restore()

    def feed(self, b: Beacon, received: int | None = None) -> None:
        self.tracker.process_beacon(b, datetime.fromtimestamp(received if received is not None else b.epoch, UTC))

    def line(self, text: str, received: int | None = None) -> None:
        """An APRS line through the whole chain: parse, stateless filters, tracker."""
        when = datetime.fromtimestamp(received if received is not None else T0, UTC)
        result = self.classifier.classify(text, when)
        if result.kind == "beacon":
            self.tracker.process_beacon(result.beacon, when)
        elif result.kind == "status":
            self.tracker.process_status(result.status)

    def finish(self) -> None:
        """Flush, close everything open, and run the finalizer."""
        self.tracker.close_all()
        self.finalizer.drain()

    # ---- looking at the result
    def flights(self) -> list[Flight]:
        with self.db.session() as s:
            return list(s.scalars(select(Flight).order_by(Flight.id)))

    def devices(self) -> list[Device]:
        with self.db.session() as s:
            return list(s.scalars(select(Device).order_by(Device.id)))

    def fixes(self, flight_id: int | None = None) -> list[Fix]:
        with self.db.session() as s:
            q = select(Fix).order_by(Fix.flight_id, Fix.ts)
            if flight_id is not None:
                q = q.where(Fix.flight_id == flight_id)
            return list(s.scalars(q))

    def count(self, table) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(table))


def fly(
    rig: Rig,
    start: int,
    *,
    address: str = "112880",
    source: str = "FLARM",
    ground_s: int = 90,
    air_s: int = 900,
    still_s: int = 400,
    step: int = 1,
    report_speed: bool = True,
    lat: float = LAT0,
    lon: float = LON0,
    alt: float = 2300.0,
    **kw,
) -> int:
    """Stand still, launch, fly east at 36 km/h while sinking 1 m/s, stand still in the landing field.

    Returns the time stamp after the last fix. ``kw`` goes to ``beacon`` (aircraft_type, receiver, ...).
    """
    t = start
    end_ground, end_air = start + ground_s, start + ground_s + air_s
    while t < end_ground + air_s + still_s:
        if t < end_ground:
            b = beacon(t, lat, lon, alt, 0.0 if report_speed else None, address=address, source=source, **kw)
        elif t < end_air:
            dt = t - end_ground
            b = beacon(t, lat, lon + 10.0 * dt / M_PER_DEG_LON, alt - 1.0 * dt, 36.0 if report_speed else None,
                       address=address, source=source, climb=-1.0, **kw)  # fmt: skip
        else:
            b = beacon(t, lat, lon + 10.0 * air_s / M_PER_DEG_LON, alt - air_s, 0.0 if report_speed else None,
                       address=address, source=source, **kw)  # fmt: skip
        rig.feed(b)
        t += step
    return t
