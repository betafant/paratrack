"""``prack replay FILE``: feed a recorded APRS log through the same pipeline as the live feed.

A recorded log (``prack diagnose --record FILE``) has one line per packet, optionally prefixed with the
reception time: ``2026-07-15T18:30:37.512Z FLR112880>OGFLR,...``. Plain APRS lines without that prefix
work too; their time stamps (HHMMSS only) are completed from ``--date`` and rolled over at midnight.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .ogn.pipeline import LineClassifier, Result

STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z\s+(.*)$")
TIME_IN_LINE_RE = re.compile(r":[/@>](\d{2})(\d{2})(\d{2})h")
if TYPE_CHECKING:
    from .runtime import Runtime

MAX_CLOCK_STEP = timedelta(hours=6)  # a larger jump of the time stamps does not move the clock


def read_log(path: Path) -> Iterator[tuple[datetime | None, str]]:
    """Yield (recorded reception time or None, APRS line) for every non-empty line."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            m = STAMP_RE.match(raw)
            if m:
                yield datetime.fromisoformat(m[1]).replace(tzinfo=UTC), m[2]
            else:
                yield None, raw


def _time_of_day(line: str) -> time:
    """HHMMSS from an APRS line, or noon when there is none."""
    m = TIME_IN_LINE_RE.search(line)
    if m:
        try:
            return time(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    return time(12, 0)


def _packet_time(result: Result) -> datetime | None:
    """The time stamp inside the packet, when the line was decoded."""
    if result.beacon is not None:
        return result.beacon.timestamp
    return result.status.timestamp if result.status is not None else None


class ReplayClock:
    """Reception time for lines without a recorded one: follows the time stamps seen so far."""

    def __init__(self, day: date) -> None:
        self.day = day
        self._now: datetime | None = None

    def reference(self, recorded: datetime | None, line: str) -> datetime:
        if recorded is not None:
            self._now = recorded
            return recorded
        if self._now is None:  # first line: its own time of day on the given date
            self._now = datetime.combine(self.day, _time_of_day(line), tzinfo=UTC)
        return self._now

    def observe(self, result: Result) -> None:
        """Move the clock to the time stamp of a decoded line, unless it jumps wildly."""
        stamp = _packet_time(result)
        if stamp is not None and self._now is not None and abs(stamp - self._now) < MAX_CLOCK_STEP:
            self._now = stamp


@dataclass
class ReplayStats:
    lines: int = 0
    first: datetime | None = None
    last: datetime | None = None


def replay_file(
    path: Path,
    classifier: LineClassifier,
    *,
    day: date,
    sink: Callable[[Result, datetime], None] | None = None,
) -> ReplayStats:
    """Classify every line of ``path``. ``sink`` receives each result with its reception time."""
    clock = ReplayClock(day)
    stats = ReplayStats()
    for recorded, line in read_log(path):
        received_at = clock.reference(recorded, line)
        result = classifier.classify(line, received_at)
        clock.observe(result)
        if recorded is None:  # a plain log has no reception time: assume the packet arrived when it was stamped
            received_at = _packet_time(result) or received_at
        if sink is not None:
            sink(result, received_at)
        stats.lines += 1
        stats.first = stats.first or received_at
        stats.last = received_at
    return stats


def replay_into(runtime: Runtime, path: Path, day: date) -> ReplayStats:
    """Run a log through the whole chain, tracker and database included, in simulated time.

    Silent flights close when the log's own clock says so; flights still open at the end are closed as gaps, and the
    finalizer runs before this returns.
    """
    seen = 0

    def sink(result: Result, received_at: datetime) -> None:
        nonlocal seen
        runtime.route(result, received_at)
        seen += 1
        if seen % 2000 == 0:
            runtime.tracker.flush()
            runtime.tracker.sweep(received_at)

    stats = replay_file(path, runtime.classifier, day=day, sink=sink)
    runtime.tracker.close_all()
    runtime.finalizer.drain()
    return stats
