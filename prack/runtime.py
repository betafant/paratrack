"""Puts the pieces together and runs them on threads.

OGN feed -> reader thread -> bounded queue -> consumer thread: parse, filters, tracker (one writer of state)
                                              writer thread: batched fix inserts every second, sweeps
                                              finalizer thread: terrain height, statistics, preview
                                              DDB thread: device database, refreshed daily
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.engine import make_url

from . import __version__
from .config import Settings
from .db import Database, utcnow
from .ogn.client import AprsClient, LinkStatus
from .ogn.ddb import DeviceDatabase
from .ogn.filters import FilterPolicy
from .ogn.ingest import Ingest
from .ogn.pipeline import LineClassifier, Result
from .ogn.survey import Survey
from .regions import Region, aprs_filter, load_regions
from .stats import Counters
from .terrain import TerrainService
from .tracking.finalizer import Finalizer
from .tracking.maintenance import merge_duplicate_flights, purge_implausible
from .tracking.tracker import Tracker

log = logging.getLogger(__name__)

REPAIR_WINDOW = timedelta(days=7)  # start-up repairs look at this much history; ``prack repair --all`` at everything
FLUSH_EVERY_S = 1.0
SWEEP_EVERY_S = 10.0


class Runtime:
    def __init__(
        self,
        settings: Settings,
        regions: list[Region] | None = None,
        *,
        feed: bool = True,
        db: Database | None = None,
        terrain_transport: httpx.BaseTransport | None = None,
        ddb_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.regions = regions or load_regions(settings.regions, settings.regions_dir)
        self.db = db or Database(settings.db_url)
        self.counters = Counters()
        self.survey = Survey()
        self.terrain = TerrainService(
            settings.terrain_url, settings.terrain_zoom, settings.data_dir / "dem",
            enabled=settings.terrain_enabled, transport=terrain_transport,
        )  # fmt: skip
        self.ddb = DeviceDatabase(self.db, settings.ddb_url, transport=ddb_transport) if settings.ddb_enabled else None
        self.finalizer = Finalizer(self.db, self.terrain, self.counters)
        self.tracker = Tracker(
            self.db, settings, self.regions, ddb=self.ddb, terrain=self.terrain, counters=self.counters,
            on_flight_closed=self.finalizer.enqueue,
        )  # fmt: skip
        self.policy = FilterPolicy.from_settings(settings)
        self.classifier = LineClassifier(self.policy, self.ddb, self.counters, self.survey)
        self.feed = feed
        self.filter_expr = aprs_filter(self.regions, settings.ogn_filter_margin_km)
        self.client = AprsClient(
            settings.ogn_host, settings.ogn_port, settings.resolved_callsign(), self.filter_expr, lambda line: None
        )
        self.ingest = Ingest(self.client, self.consume, self.counters)
        self.link = self.client.status if feed else LinkStatus(state="off")
        self.started_at = utcnow()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ the data path

    def consume(self, received_at: datetime, line: str) -> None:
        """One line from the feed (or a simulator): parse, filter, track."""
        self.route(self.classifier.classify(line, received_at), received_at)

    def route(self, result: Result, received_at: datetime) -> None:
        if result.kind == "beacon" and result.beacon is not None:
            self.tracker.process_beacon(result.beacon, received_at)
        elif result.kind == "status" and result.status is not None:
            self.tracker.process_status(result.status)

    # ------------------------------------------------------------------ life cycle

    def prepare(self) -> dict[str, int]:
        """Create the schema, repair recent data, close flights a previous run left open. No threads yet."""
        self.db.init()
        since = utcnow() - REPAIR_WINDOW
        purged = purge_implausible(self.db, since=since)
        merged = merge_duplicate_flights(self.db, since=since, on_merged=self.finalizer.enqueue)
        closed = self.tracker.restore()
        unfinished = self.finalizer.enqueue_unfinished()
        return {"purged": purged, "merged": merged, "reopened": len(closed), "unfinished": unfinished}

    def start(self) -> None:
        repairs = self.prepare()
        if any(repairs.values()):
            log.info("Start-up: %s", ", ".join(f"{k} {v}" for k, v in repairs.items() if v))
        self._spawn("finalizer", self.finalizer.run, self._stop)
        self._spawn("writer", self._write_loop)
        if self.ddb is not None:
            self.ddb.start_background(self._stop)
        if self.feed:
            self.ingest.start()

    def _spawn(self, name: str, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _write_loop(self) -> None:
        next_sweep = 0.0
        while not self._stop.wait(FLUSH_EVERY_S):
            try:
                self.tracker.flush()
                if time.monotonic() >= next_sweep:
                    next_sweep = time.monotonic() + SWEEP_EVERY_S
                    self.tracker.sweep()
            except Exception:  # noqa: BLE001 - keep writing; the fixes stay queued until the database is back
                self.counters.inc("writer.errors")
                log.exception("Writing to the database failed")

    def stop(self) -> None:
        """Stop reading, write everything out. Open flights stay open: the next start closes them as gaps and
        lets them resume."""
        if self.feed:
            self.ingest.stop()
        self._stop.set()
        for thread in self._threads:
            thread.join(15)
        try:
            self.tracker.flush(force=True)
        except Exception:  # noqa: BLE001
            log.exception("Final write failed")

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """Everything ``/api/status`` shows."""
        link = self.link.as_dict()
        link.update(filter=self.filter_expr, host=self.settings.ogn_host, port=self.settings.ogn_port)
        return {
            "version": __version__,
            "started_at": self.started_at.isoformat(),
            "link": link,
            "counters": self.counters.snapshot(),
            "drops": {k[5:]: v for k, v in self.counters.snapshot("drop.").items()},
            "sources": self.survey.snapshot(),
            "tracker": self.tracker.summary(),
            "ddb": self.ddb.stats() if self.ddb is not None else None,
            "queue": {"size": self.ingest.queue.qsize(), "dropped": self.counters.get("ingest.queue_dropped")},
            "finalizer": {"queued": self.finalizer.queue.qsize()},
            "database": {
                "url": make_url(self.db.url).render_as_string(hide_password=True),
                "size_bytes": self.db.size_bytes(),
            },
        }
