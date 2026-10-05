"""What is arriving on the feed: sources, aircraft types and what happened to each class.

``diagnose`` prints it, ``replay`` prints it, and the web app serves the same numbers on ``/api/status``,
so a source that silently vanishes (a new id format, a wrongly classified type) shows up immediately.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field

from .constants import type_name
from .parser import Beacon, Skip, Skipped

MAX_DEVICES_PER_GROUP = 5000  # bounds memory on a busy feed; the position counter stays exact
SAMPLES_PER_REASON = 5
SAMPLE_REASONS = frozenset({Skip.MALFORMED, Skip.NO_ID, Skip.BAD_TIME, Skip.BAD_POSITION})
KEPT_LIMIT = 2000
OK = "ok"  # outcome of a beacon that passed the filters


@dataclass(slots=True)
class _Group:
    positions: int = 0
    devices: set[str] = field(default_factory=set)
    outcomes: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class KeptDevice:
    """Last position of a device that passed the filters."""

    ident: str
    source: str
    address: str
    lat: float
    lon: float
    alt_m: float
    speed_kmh: float | None
    epoch: int


class Survey:
    """Counts beacons per (source, aircraft type) with the outcome of the filters. Thread safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: dict[tuple[str, int, int], _Group] = {}
        self._skips: Counter[tuple[str, str]] = Counter()
        self._samples: dict[str, list[str]] = {}
        self._kept: dict[str, KeptDevice] = {}

    def record(self, beacon: Beacon, outcome: str) -> None:
        """``outcome`` is ``ok`` or the value of a ``DropReason``."""
        key = (beacon.source, beacon.aircraft_type, beacon.reported_type)
        with self._lock:
            group = self._groups.get(key)
            if group is None:
                group = self._groups[key] = _Group()
            group.positions += 1
            group.outcomes[outcome] += 1
            if len(group.devices) < MAX_DEVICES_PER_GROUP:
                group.devices.add(beacon.address)
            if outcome == OK and (beacon.address in self._kept or len(self._kept) < KEPT_LIMIT):
                self._kept[beacon.address] = KeptDevice(
                    beacon.ident,
                    beacon.source,
                    beacon.address,
                    beacon.lat,
                    beacon.lon,
                    beacon.alt_m,
                    beacon.speed_kmh,
                    beacon.epoch,
                )

    def skip(self, skipped: Skipped, line: str) -> None:
        if skipped.reason is Skip.COMMENT:
            return
        with self._lock:
            self._skips[(skipped.source or "?", skipped.reason.value)] += 1
            if skipped.reason in SAMPLE_REASONS:
                samples = self._samples.setdefault(skipped.reason.value, [])
                if len(samples) < SAMPLES_PER_REASON:
                    samples.append(line[:200])

    def kept_devices(self) -> list[KeptDevice]:
        with self._lock:
            return sorted(self._kept.values(), key=lambda d: d.ident)

    def snapshot(self) -> dict:
        """JSON friendly summary, most frequent classes first."""
        with self._lock:
            groups = [
                {
                    "source": source,
                    "aircraft_type": aircraft_type,
                    "type_name": type_name(aircraft_type),
                    "reported_type": reported_type,
                    "positions": g.positions,
                    "devices": len(g.devices),
                    "outcomes": dict(g.outcomes),
                }
                for (source, aircraft_type, reported_type), g in self._groups.items()
            ]
            skips = [{"source": s, "reason": r, "count": n} for (s, r), n in self._skips.most_common()]
            samples = {reason: list(lines) for reason, lines in self._samples.items()}
        groups.sort(key=lambda g: (-g["positions"], g["source"]))
        return {"groups": groups, "skips": skips, "samples": samples}
