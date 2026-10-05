"""SQLAlchemy models.

Milestone 1 holds the device database cache and a small key/value table; the flight, device and fix
tables are added with the tracker.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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
