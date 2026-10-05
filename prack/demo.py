"""Demo mode: the real tracker, database and API, fed by simulated paragliders instead of the OGN feed.

``prack demo`` works offline. It always uses its own database (``<data dir>/demo.db``, recreated at start), never the
real one. A few past days are recorded first so the calendar has something to show; then simulated pilots keep
taking off, flying and landing in real time.
"""

from __future__ import annotations

import logging
import threading
import time as clock
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import func, select

from .db import utcnow
from .models import Flight
from .ogn.client import LinkStatus
from .ogn.simulator import Ground, Simulator
from .runtime import Runtime
from .terrain import TerrainService

log = logging.getLogger(__name__)

PREROLL_S = 1200  # the live feed starts this far in the past and catches up at once, so the map is not empty
WAVE_EVERY_S = 1500  # a new group of pilots takes off this often
HISTORY_PROTOCOLS = [("flarm",), ("fanet",), ("flarm",), ("ogn",)]  # single protocols: fast to record


def demo_database_path(data_dir: Path) -> Path:
    return data_dir / "demo.db"


def remove_demo_database(data_dir: Path) -> None:
    path = demo_database_path(data_dir)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def terrain_works(terrain: TerrainService, lat: float, lon: float, timeout: float = 6.0) -> bool:
    """Is a terrain tile reachable (or cached)? Gives up after ``timeout`` so an offline demo starts quickly."""
    result: list[float | None] = []
    thread = threading.Thread(target=lambda: result.append(terrain.height(lat, lon)), daemon=True)
    thread.start()
    thread.join(timeout)
    return bool(result) and result[0] is not None


class Demo:
    def __init__(
        self,
        runtime: Runtime,
        *,
        speed: float = 1.0,
        pilots: int = 12,
        history_days: int = 2,
        say: Callable[[str], None] = print,
    ) -> None:
        if not 1.0 <= speed <= 60.0:
            raise ValueError("speed must be between 1 and 60")
        self.runtime = runtime
        self.speed = speed
        self.pilots = pilots
        self.history_days = history_days
        self.say = say
        self.region = runtime.regions[0]
        self.ground: Ground | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        runtime.link = LinkStatus(state="demo", server="simulated pilots")

    # ------------------------------------------------------------------ preparation

    def prepare(self) -> None:
        """Choose how altitudes are made, then record the past days."""
        lat, lon = self.region.center[1], self.region.center[0]
        if terrain_works(self.runtime.terrain, lat, lon):
            self.ground = self.runtime.terrain.height  # flights lie on the real terrain, as the 3D view shows it
            self.say("Terrain tiles reachable: simulated flights follow the real mountains.")
        else:
            self.runtime.terrain.enabled = False
            self.say("No terrain tiles (offline?): simulated flights use invented altitudes.")
        self.runtime.prepare()
        self.seed_history()

    def _feed(self, sim: Simulator, seconds: int, *, flush_every: int = 20) -> None:
        tracker = self.runtime.tracker
        for k in range(seconds):
            now = sim.at(k)
            for line in sim.lines(now):
                self.runtime.consume(now, line)
            if k % flush_every == 0:
                tracker.flush()
            if k % 60 == 0:
                tracker.sweep(now)

    def seed_history(self) -> None:
        """Record the last few days with the real tracker; days that already have flights are left alone."""
        today = datetime.now(self.region.tz).date()
        for back in range(self.history_days, 0, -1):
            day = today - timedelta(days=back)
            with self.runtime.db.session() as session:
                if session.scalar(select(func.count()).select_from(Flight).where(Flight.date == day)):
                    continue
            self.say(f"Recording simulated flights for {day} ...")
            start = datetime.combine(day, time(10, 30), tzinfo=self.region.tz).astimezone(UTC)
            sim = Simulator.demo(
                start, self.region, pilots=max(6, self.pilots - 2), seed=1000 + back, ground=self.ground,
                protocols=HISTORY_PROTOCOLS, noise=False, stagger=600, address_base=0xE00000 + 0x1000 * back,
            )  # fmt: skip
            self._feed(sim, sim.duration_s() + 2)
            self.runtime.tracker.close_all()
            self.runtime.finalizer.drain()

    # ------------------------------------------------------------------ live traffic

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="demo-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)

    def _new_wave(self, number: int, start: datetime) -> Simulator:
        return Simulator.demo(
            start, self.region, pilots=self.pilots, seed=number, ground=self.ground,
            address_base=0xD00000 + 0x1000 * number,
        )  # fmt: skip

    def _run(self) -> None:
        """Feed simulated seconds: first catch up from the past to now as fast as possible, then follow a clock that
        runs ``speed`` simulated seconds per real second."""
        start = utcnow().replace(microsecond=0) - timedelta(seconds=PREROLL_S)
        sims = [self._new_wave(0, start)]
        next_wave, waves = start + timedelta(seconds=WAVE_EVERY_S), 1
        sim_now = start
        anchor: tuple[float, datetime] | None = None  # (monotonic time, simulated time) once caught up
        while not self._stop.is_set():
            if anchor is None:
                target = utcnow()
            else:
                target = anchor[1] + timedelta(seconds=(clock.monotonic() - anchor[0]) * self.speed)
            try:
                while sim_now < target and not self._stop.is_set():
                    if sim_now >= next_wave:
                        sims.append(self._new_wave(waves, sim_now))
                        waves, next_wave = waves + 1, next_wave + timedelta(seconds=WAVE_EVERY_S)
                    for sim in sims:
                        for line in sim.lines(sim_now):
                            self.runtime.consume(sim_now, line)
                    if sim_now.second % 10 == 0:
                        self.runtime.tracker.sweep(sim_now)
                        sims = [x for x in sims if (sim_now - x.start).total_seconds() < x.duration_s() + 120]
                    sim_now += timedelta(seconds=1)
            except Exception:  # noqa: BLE001 - a demo must keep going
                log.exception("Demo feed error")
            if anchor is None and sim_now >= target:
                anchor = (clock.monotonic(), sim_now)
                log.info("Demo feed caught up with the clock")
            self._stop.wait(0.2)
