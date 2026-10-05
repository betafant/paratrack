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
    TYPE = "type"  # aircraft type is not tracked
    NO_TRACKING = "no_tracking"  # no-tracking flag in the id field
    STEALTH = "stealth"  # stealth flag in the id field
    DDB_UNTRACKED = "ddb_untracked"  # owner opted out in the device database


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
