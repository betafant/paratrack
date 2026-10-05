"""Database access: SQLAlchemy engine with SQLite tuned for one writer and many readers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, Insert, create_engine, event, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base, Meta
from .views import create_views


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(url: str) -> Engine:
    """Create an engine. SQLite gets WAL, ``synchronous=NORMAL``, a busy timeout and foreign keys."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, pool_pre_ping=True)

    in_memory = parsed.database in (None, "", ":memory:")
    if not in_memory:
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool if in_memory else None,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        if not in_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


class Database:
    """An engine plus a session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def init(self) -> None:
        """Create the tables that do not exist yet, and (re)create the analysis views."""
        Base.metadata.create_all(self.engine)
        create_views(self.engine)

    def insert_ignore(self, model: type[Base]) -> Insert:
        """``INSERT ... ON CONFLICT DO NOTHING`` for the given model (SQLite and PostgreSQL)."""
        if self.engine.dialect.name == "postgresql":
            return postgresql.insert(model).on_conflict_do_nothing()
        return sqlite.insert(model).on_conflict_do_nothing()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session that commits on success and rolls back on error."""
        with self._sessions() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def get_meta(self, key: str) -> str | None:
        with self.session() as session:
            row = session.get(Meta, key)
            return row.value if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.session() as session:
            session.merge(Meta(key=key, value=value))

    def size_bytes(self) -> int | None:
        """Size of the database on disk (SQLite file plus write-ahead log), or PostgreSQL's own figure."""
        try:
            if self.engine.dialect.name == "postgresql":
                with self.engine.connect() as conn:
                    return int(conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
            name = make_url(self.url).database
            if not name or name == ":memory:":
                return None
            return sum(p.stat().st_size for p in (Path(name), Path(name + "-wal")) if p.exists())
        except Exception:  # noqa: BLE001 - a status figure, never worth failing for
            return None

    def dispose(self) -> None:
        self.engine.dispose()
