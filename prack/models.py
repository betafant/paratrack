"""SQLAlchemy models. All timestamps are UTC.

``fixes`` is the big table (one row per second and flight), so it is compact: integer-scaled columns, a
composite primary key and ``WITHOUT ROWID`` on SQLite (about 62 bytes per fix instead of about 190). Do not
query it for analysis; use the views ``fixes_v`` and ``flights_v`` (see ``prack/views.py``), which show real units.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """Stored as a naive UTC timestamp, returned as an aware UTC ``datetime``."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UtcDateTime()}


class Ddb(Base):
    """Copy of the OGN device database, keyed by the 24 bit device address (6 hex digits)."""

    __tablename__ = "ddb"

    address: Mapped[str] = mapped_column(String(6), primary_key=True)
    model: Mapped[str | None] = mapped_column(String(64))
    registration: Mapped[str | None] = mapped_column(String(32))
    competition_id: Mapped[str | None] = mapped_column(String(16))
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    identified: Mapped[bool] = mapped_column(Boolean, default=True)
    ddb_aircraft_type: Mapped[int | None] = mapped_column(Integer)


class Meta(Base):
    """Small key/value store (e.g. when the device database was last refreshed)."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))


class Device(Base):
    """One physical device. ``callsign`` is the identity of its preferred protocol, e.g. ``FLR112880``.

    Registration and competition number are stored only when the owner agreed to be identified.
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callsign: Mapped[str] = mapped_column(String(16), unique=True)
    address: Mapped[str] = mapped_column(String(8), index=True)
    address_type: Mapped[int | None] = mapped_column(SmallInteger)
    aircraft_type: Mapped[int] = mapped_column(SmallInteger, default=0)
    source: Mapped[str | None] = mapped_column(String(32))
    registration: Mapped[str | None] = mapped_column(String(32))
    competition_id: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(64))
    pilot_name: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime | None]
    last_seen: Mapped[datetime | None]


class Flight(Base):
    __tablename__ = "flights"
    __table_args__ = (Index("ix_flights_date_region", "date", "region"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    region: Mapped[str] = mapped_column(String(32))
    date: Mapped[date] = mapped_column(Date)  # local date (region time zone) of the start
    status: Mapped[str] = mapped_column(String(8), default="active")  # active | closed
    close_reason: Mapped[str | None] = mapped_column(String(8))  # landed | gap
    airborne: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str | None] = mapped_column(String(32))
    utc_offset_s: Mapped[int] = mapped_column(Integer, default=0)  # local time = UTC + this, at the start
    start_time: Mapped[datetime]
    end_time: Mapped[datetime]
    takeoff_time: Mapped[datetime | None]
    landing_time: Mapped[datetime | None]
    fix_count: Mapped[int] = mapped_column(Integer, default=0)
    takeoff_lat: Mapped[float | None] = mapped_column(Float)
    takeoff_lon: Mapped[float | None] = mapped_column(Float)
    takeoff_alt: Mapped[float | None] = mapped_column(Float)
    landing_lat: Mapped[float | None] = mapped_column(Float)
    landing_lon: Mapped[float | None] = mapped_column(Float)
    landing_alt: Mapped[float | None] = mapped_column(Float)
    min_lat: Mapped[float | None] = mapped_column(Float)
    max_lat: Mapped[float | None] = mapped_column(Float)
    min_lon: Mapped[float | None] = mapped_column(Float)
    max_lon: Mapped[float | None] = mapped_column(Float)
    max_alt: Mapped[float | None] = mapped_column(Float)
    min_alt: Mapped[float | None] = mapped_column(Float)
    max_agl: Mapped[float | None] = mapped_column(Float)
    alt_gain: Mapped[float | None] = mapped_column(Float)
    max_climb: Mapped[float | None] = mapped_column(Float)
    max_sink: Mapped[float | None] = mapped_column(Float)
    max_speed: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)
    straight_km: Mapped[float | None] = mapped_column(Float)
    max_from_start_km: Mapped[float | None] = mapped_column(Float)
    ground_filled: Mapped[bool] = mapped_column(Boolean, default=False)
    preview: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))  # [[lon, lat, alt], ...]: overview only
    created_at: Mapped[datetime | None]
    updated_at: Mapped[datetime | None]


class Fix(Base):
    """One track point. Scaled integers, see ``prack/units.py``; use ``fixes_v`` to read it."""

    __tablename__ = "fixes"
    __table_args__ = {"sqlite_with_rowid": False}

    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # epoch seconds
    lat: Mapped[int] = mapped_column(Integer)  # degrees * 1e6
    lon: Mapped[int] = mapped_column(Integer)
    alt: Mapped[int] = mapped_column(Integer)  # GPS altitude MSL, m * 10
    ground: Mapped[int | None] = mapped_column(Integer)  # terrain height below, m * 10
    speed: Mapped[int | None] = mapped_column(Integer)  # km/h * 10
    track: Mapped[int | None] = mapped_column(SmallInteger)  # course, degrees
    climb: Mapped[int | None] = mapped_column(SmallInteger)  # m/s * 100
    turn: Mapped[int | None] = mapped_column(SmallInteger)  # deg/s * 10
    src: Mapped[int] = mapped_column(SmallInteger)  # protocol of this fix, see SOURCE_CODES
    receiver: Mapped[str | None] = mapped_column(String(24))
    signal: Mapped[int | None] = mapped_column(SmallInteger)  # dB * 10
    errors: Mapped[int | None] = mapped_column(SmallInteger)  # corrected bit errors
    freq_offset: Mapped[int | None] = mapped_column(SmallInteger)  # kHz * 10
    gps: Mapped[str | None] = mapped_column(String(8))  # e.g. "2x3"
