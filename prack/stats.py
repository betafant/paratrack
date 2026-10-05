"""Thread-safe named counters (drop reasons, line counts, ...)."""

from __future__ import annotations

import threading
from collections import Counter


class Counters:
    """A bag of integer counters that can be bumped from several threads."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counts[key] += n

    def get(self, key: str) -> int:
        with self._lock:
            return self._counts[key]

    def snapshot(self, prefix: str = "") -> dict[str, int]:
        """Copy of all counters whose name starts with ``prefix``, sorted by name."""
        with self._lock:
            return {k: v for k, v in sorted(self._counts.items()) if k.startswith(prefix)}
