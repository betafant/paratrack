"""OGN device database (DDB): registrations, competition numbers and owners' privacy choices.

Owners can opt out of tracking (``tracked = N``) or of being identified (``identified = N``). prack honours
both: untracked devices are never stored, and registration / competition number are shown only for
identified devices.

The DDB is downloaded at start (when missing or older than a day), cached in the database so restarts
need no download, and refreshed daily by a background thread.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, insert, select

from .. import __version__
from ..db import Database, utcnow
from ..models import Ddb
from .parser import clean_text

log = logging.getLogger(__name__)

META_UPDATED = "ddb_updated_at"
REFRESH_EVERY = timedelta(hours=24)
RETRY_FIRST = 15 * 60.0
RETRY_MAX = 6 * 3600.0
INSERT_CHUNK = 5000


class DdbError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DdbInfo:
    address: str
    registration: str | None
    competition_id: str | None
    model: str | None
    tracked: bool
    identified: bool
    ddb_aircraft_type: int | None = None

    @property
    def shown_registration(self) -> str | None:
        """Registration to display: only when the owner agreed to be identified."""
        return self.registration if self.identified else None

    @property
    def shown_competition_id(self) -> str | None:
        return self.competition_id if self.identified else None


def _yes(value: object, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().upper() in ("Y", "YES", "1", "TRUE")


def _merge(a: DdbInfo, b: DdbInfo) -> DdbInfo:
    """Two entries for one address (e.g. a FLARM and an ICAO device): the stricter privacy choice wins."""
    return DdbInfo(
        address=a.address,
        registration=a.registration or b.registration,
        competition_id=a.competition_id or b.competition_id,
        model=a.model or b.model,
        tracked=a.tracked and b.tracked,
        identified=a.identified and b.identified,
        ddb_aircraft_type=a.ddb_aircraft_type if a.ddb_aircraft_type is not None else b.ddb_aircraft_type,
    )


def parse_ddb(payload: object) -> dict[str, DdbInfo]:
    """Turn the DDB JSON (``{"devices": [...]}``) into entries by address. Bad rows are skipped."""
    devices = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(devices, list):
        raise DdbError("unexpected DDB response: no 'devices' list")
    entries: dict[str, DdbInfo] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        address = str(device.get("device_id", "")).strip().upper()
        if len(address) != 6 or not all(c in "0123456789ABCDEF" for c in address):
            continue
        try:
            ddb_type: int | None = int(device.get("aircraft_type") or 0)
        except (TypeError, ValueError):
            ddb_type = None
        info = DdbInfo(
            address=address,
            registration=clean_text(str(device.get("registration") or ""), 32),
            competition_id=clean_text(str(device.get("cn") or ""), 16),
            model=clean_text(str(device.get("aircraft_model") or ""), 64),
            tracked=_yes(device.get("tracked")),
            identified=_yes(device.get("identified")),
            ddb_aircraft_type=ddb_type,
        )
        entries[address] = _merge(entries[address], info) if address in entries else info
    return entries


class DeviceDatabase:
    """In-memory lookup by device address, backed by the ``ddb`` table (when a database is given)."""

    def __init__(
        self,
        db: Database | None,
        url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        max_age: timedelta = REFRESH_EVERY,
    ) -> None:
        self.db = db
        self.url = url
        self.max_age = max_age
        self._transport = transport
        self._entries: dict[str, DdbInfo] = {}
        self._lock = threading.Lock()
        self.updated_at: datetime | None = None
        self.last_error: str | None = None

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, address: str) -> DdbInfo | None:
        return self._entries.get(address.upper())

    def stats(self) -> dict:
        entries = self._entries
        return {
            "devices": len(entries),
            "untracked": sum(1 for e in entries.values() if not e.tracked),
            "unidentified": sum(1 for e in entries.values() if not e.identified),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_error": self.last_error,
        }

    def load_from_db(self) -> int:
        """Read the cached copy. Returns the number of devices."""
        if self.db is None:
            return 0
        with self.db.session() as session:
            rows = session.execute(select(Ddb)).scalars().all()
        entries = {
            r.address: DdbInfo(
                r.address, r.registration, r.competition_id, r.model, r.tracked, r.identified, r.ddb_aircraft_type
            )
            for r in rows
        }
        stamp = self.db.get_meta(META_UPDATED)
        with self._lock:
            self._entries = entries
            self.updated_at = datetime.fromisoformat(stamp) if stamp else None
        log.info("Loaded %d device database entries from the database", len(entries))
        return len(entries)

    def is_stale(self) -> bool:
        return not self._entries or self.updated_at is None or utcnow() - self.updated_at > self.max_age

    def refresh(self) -> int:
        """Download the DDB, replace the cached copy and the in-memory table. Returns the device count."""
        try:
            with httpx.Client(
                timeout=60,
                follow_redirects=True,
                transport=self._transport,
                headers={"User-Agent": f"prack/{__version__}"},
            ) as client:
                response = client.get(self.url)
                response.raise_for_status()
                entries = parse_ddb(response.json())
            if not entries:
                raise DdbError("the device database response contained no devices")
        except (httpx.HTTPError, ValueError, DdbError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        now = utcnow().replace(microsecond=0)
        if self.db is not None:
            self._store(entries, now)
        with self._lock:
            self._entries = entries
            self.updated_at = now
            self.last_error = None
        untracked = sum(1 for e in entries.values() if not e.tracked)
        log.info("Device database refreshed: %d devices, %d opted out of tracking", len(entries), untracked)
        if untracked == 0 and len(entries) > 1000:
            log.warning(
                "No device in the database has opted out of tracking. If %s filters them out, owners' "
                "opt-outs cannot be honoured (set PRACK_DDB_URL to the full download).",
                self.url,
            )
        return len(entries)

    def _store(self, entries: dict[str, DdbInfo], when: datetime) -> None:
        assert self.db is not None
        rows = [
            {
                "address": e.address,
                "model": e.model,
                "registration": e.registration,
                "competition_id": e.competition_id,
                "tracked": e.tracked,
                "identified": e.identified,
                "ddb_aircraft_type": e.ddb_aircraft_type,
            }
            for e in entries.values()
        ]
        with self.db.session() as session:
            session.execute(delete(Ddb))
            for i in range(0, len(rows), INSERT_CHUNK):
                session.execute(insert(Ddb), rows[i : i + INSERT_CHUNK])
        self.db.set_meta(META_UPDATED, when.isoformat())

    def start_background(self, stop: threading.Event) -> threading.Thread:
        """Load the cache, then keep it fresh: refresh when stale, retry with back-off after failures."""
        thread = threading.Thread(target=self._run, args=(stop,), name="ddb-refresh", daemon=True)
        thread.start()
        return thread

    def _run(self, stop: threading.Event) -> None:
        try:
            self.load_from_db()
        except Exception:  # noqa: BLE001 - the cache is optional
            log.exception("Could not read the cached device database")
        delay = RETRY_FIRST
        while not stop.is_set():
            wait = 3600.0
            if self.is_stale():
                try:
                    self.refresh()
                    delay = RETRY_FIRST
                except Exception as exc:  # noqa: BLE001 - keep the thread alive, try again later
                    log.warning("Device database refresh failed: %s (retry in %.0f min)", exc, delay / 60)
                    wait, delay = delay, min(delay * 2, RETRY_MAX)
            if stop.wait(wait):
                break
