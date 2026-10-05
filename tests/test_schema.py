"""The compact schema, its views in real units, and the guarantees the tracker relies on.

Runs on SQLite; also on PostgreSQL when PRACK_TEST_PG_URL points at an empty scratch database.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from prack.db import Database
from prack.models import Base, Device, Fix, Flight
from prack.ogn.constants import SOURCE_CODES
from prack.tracking.tracker import Point, fix_row
from prack.views import VIEWS

PG_URL = os.environ.get("PRACK_TEST_PG_URL")
T0 = int(datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC).timestamp())


@pytest.fixture(
    params=["sqlite", pytest.param("postgresql", marks=pytest.mark.skipif(not PG_URL, reason="no PRACK_TEST_PG_URL"))]
)
def db(request, tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'schema.db'}" if request.param == "sqlite" else PG_URL)
    if request.param == "postgresql":
        with database.engine.begin() as conn:
            for view in VIEWS:
                conn.execute(text(f"DROP VIEW IF EXISTS {view}"))
        Base.metadata.drop_all(database.engine)
    database.init()
    yield database
    database.dispose()


def add_flight(db: Database, *, callsign="FLR112880", pilot="Mia", registration="HB-3123", cn="X1") -> int:
    with db.session() as s:
        device = Device(
            callsign=callsign,
            address=callsign[3:],
            aircraft_type=7,
            source="FLARM",
            pilot_name=pilot,
            registration=registration,
            competition_id=cn,
            model="Gin Boomerang 12",
        )
        s.add(device)
        s.flush()
        flight = Flight(
            device_id=device.id,
            region="ch",
            date=date(2026, 7, 15),
            status="closed",
            close_reason="landed",
            airborne=True,
            source="FLARM",
            utc_offset_s=7200,
            start_time=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
            end_time=datetime(2026, 7, 15, 10, 45, tzinfo=UTC),
            takeoff_time=datetime(2026, 7, 15, 10, 5, tzinfo=UTC),
            landing_time=datetime(2026, 7, 15, 10, 35, tzinfo=UTC),
            fix_count=3,
            max_alt=2400.5,
            distance_km=12.345,
        )
        s.add(flight)
        s.flush()
        return flight.id


POINT = Point(
    T0,
    46.645312,
    7.651098,
    2330.4,
    35.2,
    90.0,
    -1.93,
    3.0,
    SOURCE_CODES["FLARM"],
    "Pizol",
    12.5,
    2,
    1.2,
    "2x3",
    ground=1810.3,
)


def test_a_fix_comes_back_in_real_units_through_the_view(db):
    fid = add_flight(db)
    with db.session() as s:
        s.execute(db.insert_ignore(Fix), [fix_row(fid, POINT)])
        row = s.execute(text("SELECT * FROM fixes_v")).mappings().one()
    assert (row["lat"], row["lon"]) == pytest.approx((46.645312, 7.651098), abs=1e-6)
    assert (row["alt_m"], row["ground_m"], row["agl_m"]) == pytest.approx((2330.4, 1810.3, 520.1), abs=0.05)
    assert (row["speed_kmh"], row["vario_ms"], row["turn_dps"]) == pytest.approx((35.2, -1.93, 3.0), abs=0.005)
    assert (row["heading"], row["source"], row["receiver"], row["gps"]) == (90, "FLARM", "Pizol", "2x3")
    assert (row["signal_db"], row["bit_errors"], row["freq_offset_khz"]) == pytest.approx((12.5, 2, 1.2), abs=0.05)
    assert str(row["utc"])[:19].replace("T", " ") == "2026-07-15 10:00:00"
    assert str(row["local_time"])[:19].replace("T", " ") == "2026-07-15 12:00:00"  # UTC + the flight's offset
    assert (row["callsign"], row["pilot_name"], row["registration"], row["competition_id"]) == (
        "FLR112880",
        "Mia",
        "HB-3123",
        "X1",
    )
    assert (row["flight_id"], str(row["flight_date"]), row["region"], bool(row["airborne"])) == (
        fid,
        "2026-07-15",
        "ch",
        True,
    )


def test_missing_values_stay_missing(db):
    fid = add_flight(db)
    bare = Point(T0, 46.6, 7.6, 2000.0, None, None, None, None, 99, None, None, None, None, None)
    with db.session() as s:
        s.execute(db.insert_ignore(Fix), [fix_row(fid, bare)])
        row = s.execute(text("SELECT * FROM fixes_v")).mappings().one()
    for column in ("ground_m", "agl_m", "speed_kmh", "heading", "vario_ms", "turn_dps", "receiver", "signal_db", "gps"):
        assert row[column] is None, column
    assert row["source"] == "other"


def test_every_known_protocol_has_a_name_in_the_view(db):
    fid = add_flight(db)
    rows = [
        fix_row(fid, Point(T0 + code, 46.6, 7.6, 2000.0, 1.0, None, None, None, code, None, None, None, None, None))
        for code in SOURCE_CODES.values()
    ]
    with db.session() as s:
        s.execute(db.insert_ignore(Fix), rows)
        names = dict(s.execute(text("SELECT ts, source FROM fixes_v")).all())
    assert {names[T0 + code] for code in SOURCE_CODES.values()} == set(SOURCE_CODES)


def test_flights_view_has_local_times_and_minutes(db):
    add_flight(db)
    with db.session() as s:
        row = s.execute(text("SELECT * FROM flights_v")).mappings().one()
    assert str(row["start_local"])[11:16] == "12:00" and str(row["end_local"])[11:16] == "12:45"
    assert str(row["takeoff_local"])[11:16] == "12:05" and str(row["landing_local"])[11:16] == "12:35"
    assert (row["duration_min"], row["airtime_min"]) == pytest.approx((45.0, 30.0))
    assert (str(row["local_date"]), row["callsign"], row["pilot_name"], row["max_alt_m"], row["distance_km"]) == (
        "2026-07-15",
        "FLR112880",
        "Mia",
        2400.5,
        12.345,
    )


def test_the_first_copy_of_a_fix_wins(db):
    fid = add_flight(db)
    other = Point(T0, 47.0, 8.0, 3000.0, 1.0, None, None, None, 1, "Elsewhere", None, None, None, None)
    with db.session() as s:
        s.execute(db.insert_ignore(Fix), [fix_row(fid, POINT)])
        s.execute(db.insert_ignore(Fix), [fix_row(fid, other), fix_row(fid, POINT)])  # ON CONFLICT DO NOTHING
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Fix)) == 1
        assert s.scalar(select(Fix.receiver)) == "Pizol"


def test_hostile_values_are_clamped_not_fatal(db):
    fid = add_flight(db)
    wild = Point(T0, 46.6, 7.6, 2000.0, 1e9, 359.9, 1e9, -1e9, 0, "R" * 100, 1e9, 10**9, 1e9, "x" * 40)
    with db.session() as s:
        s.execute(db.insert_ignore(Fix), [fix_row(fid, wild)])
        row = s.scalars(select(Fix)).one()
    assert (row.climb, row.turn, row.signal, row.freq_offset) == (32767, -32768, 32767, 32767)
    assert (row.errors, len(row.receiver), len(row.gps), row.track) == (32767, 24, 8, 0)


def test_callsigns_are_unique_and_flights_need_a_device(db):
    add_flight(db)
    with pytest.raises(IntegrityError), db.session() as s:
        s.add(Device(callsign="FLR112880", address="112880"))
    with pytest.raises(IntegrityError), db.session() as s:
        s.add(
            Flight(
                device_id=999,
                region="ch",
                date=date(2026, 7, 15),
                start_time=datetime.now(UTC),
                end_time=datetime.now(UTC),
            )
        )


def test_deleting_a_flight_deletes_its_fixes(db):
    fid = add_flight(db)
    with db.session() as s:
        s.execute(db.insert_ignore(Fix), [fix_row(fid, POINT)])
    with db.session() as s:
        s.execute(Flight.__table__.delete().where(Flight.id == fid))
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Fix)) == 0


def test_creating_the_schema_twice_is_harmless(db):
    add_flight(db)
    db.init()
    db.init()
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Flight)) == 1


def test_utc_timestamps_round_trip_as_aware_datetimes(db):
    fid = add_flight(db)
    with db.session() as s:
        flight = s.get(Flight, fid)
        assert flight.start_time == datetime(2026, 7, 15, 10, 0, tzinfo=UTC) and flight.start_time.tzinfo is not None


@pytest.mark.skipif(PG_URL is not None, reason="file size check is SQLite specific")
def test_a_fix_takes_about_sixty_bytes(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'size.db'}")
    database.init()
    fid = add_flight(database)
    rows = []
    for i in range(60_000):  # 100 minutes of 1 Hz FLARM
        p = Point(
            T0 + i,
            46.645312 + i * 1e-5,
            7.651098 + i * 2e-5,
            2330.4 - i * 0.01,
            35.2 + (i % 7) * 0.3,
            float(i % 360),
            -1.9 + (i % 5) * 0.1,
            3.0,
            0,
            "Pizol",
            12.5 + (i % 9),
            0,
            1.2,
            "2x3",
            ground=1810.3 + (i % 50),
        )
        rows.append(fix_row(fid, p))
    with database.session() as s:
        s.execute(database.insert_ignore(Fix), rows)
    with database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM"))
        bytes_per_fix = (
            conn.execute(text("PRAGMA page_count")).scalar() * conn.execute(text("PRAGMA page_size")).scalar()
        ) / 60_000
    assert 40 < bytes_per_fix < 80  # about 62 bytes against about 190 with REAL and DATETIME columns
    with database.engine.connect() as conn:
        assert "WITHOUT ROWID" in conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'fixes'")).scalar()
    database.dispose()
