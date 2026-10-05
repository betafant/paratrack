"""Line -> classified result: parse, apply the stateless filters, keep the books.

The tracker, ``diagnose`` and ``replay`` all start here, so every one of them counts drops the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..stats import Counters
from .filters import DdbLookup, FilterPolicy, static_drop_reason
from .parser import Beacon, Skipped, Status, parse_line
from .survey import OK, Survey


@dataclass(slots=True)
class Result:
    """``kind`` is ``beacon`` (passed the filters), ``status`` (pilot name), ``dropped`` or ``skipped``."""

    kind: str
    beacon: Beacon | None = None
    status: Status | None = None
    reason: str | None = None  # drop or skip reason


class LineClassifier:
    """Counter names: ``lines``, ``accepted``, ``status``, ``skip.<reason>``, ``drop.<reason>``."""

    def __init__(
        self,
        policy: FilterPolicy,
        ddb: DdbLookup | None,
        counters: Counters,
        survey: Survey | None = None,
    ) -> None:
        self.policy = policy
        self.ddb = ddb
        self.counters = counters
        self.survey = survey

    def classify(self, line: str, received_at: datetime) -> Result:
        counters = self.counters
        counters.inc("lines")
        parsed = parse_line(line, received_at)
        if isinstance(parsed, Skipped):
            counters.inc(f"skip.{parsed.reason.value}")
            if self.survey is not None:
                self.survey.skip(parsed, line)
            return Result("skipped", reason=parsed.reason.value)
        if isinstance(parsed, Status):
            counters.inc("status")
            return Result("status", status=parsed)
        drop = static_drop_reason(parsed, self.policy, self.ddb)
        if drop is not None:
            counters.inc(f"drop.{drop.value}")
            if self.survey is not None:
                self.survey.record(parsed, drop.value)
            return Result("dropped", beacon=parsed, reason=drop.value)
        counters.inc("accepted")
        if self.survey is not None:
            self.survey.record(parsed, OK)
        return Result("beacon", beacon=parsed)
