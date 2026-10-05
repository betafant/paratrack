"""Stateless filters: aircraft type and privacy.

These need only the beacon itself (and the device database), so they are shared by the tracker,
``diagnose`` and ``replay``. Stateful filters (time sanity, glitches, plausibility, region) live in the
tracker.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .constants import PARAGLIDER
from .parser import Beacon

if TYPE_CHECKING:
    from ..config import Settings
    from .ddb import DdbInfo


class DropReason(enum.Enum):
    # stateless (this module)
    TYPE = "type"  # aircraft type is not tracked
    NO_TRACKING = "no_tracking"  # no-tracking flag in the id field
    STEALTH = "stealth"  # stealth flag in the id field
    DDB_UNTRACKED = "ddb_untracked"  # owner opted out in the device database
    # stateful (tracker)
    FUTURE = "future"  # time stamp more than 2 minutes ahead of the reception time
    STALE = "stale"  # time stamp more than 30 minutes old
    OUT_OF_ORDER = "out_of_order"  # not newer than the last position of this device (also: duplicates)
    GLITCH = "glitch"  # a jump of more than 2 km at over 500 km/h
    SPIKE = "spike"  # a single implausibly fast position
    MISCONFIGURED = "misconfigured"  # device keeps flying faster than a paraglider can
    OUTSIDE_REGION = "outside_region"  # not heard inside a region box yet
    LOWER_SOURCE = "lower_source"  # a better protocol of the same device was heard in the last 30 s


class DdbLookup(Protocol):
    def lookup(self, address: str) -> DdbInfo | None: ...


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    tracked_types: frozenset[int] = frozenset({PARAGLIDER})
    respect_stealth: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> FilterPolicy:
        return cls(frozenset(settings.tracked_types), settings.respect_stealth)


def static_drop_reason(beacon: Beacon, policy: FilterPolicy, ddb: DdbLookup | None = None) -> DropReason | None:
    """Why this beacon must not be tracked, or None when it passes. Order matters: type first."""
    if beacon.aircraft_type not in policy.tracked_types:
        return DropReason.TYPE
    if beacon.no_tracking:
        return DropReason.NO_TRACKING
    if beacon.stealth and policy.respect_stealth:
        return DropReason.STEALTH
    if ddb is not None:
        info = ddb.lookup(beacon.address)
        if info is not None and not info.tracked:
            return DropReason.DDB_UNTRACKED
    return None
