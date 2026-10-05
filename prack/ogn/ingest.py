"""Reader thread -> bounded queue -> consumer thread.

The socket reader never touches the database or any slow code: it stamps each line with its reception
time and puts it on a bounded queue. When the consumer falls behind, lines are dropped (and counted)
instead of growing memory without limit.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime

from ..db import utcnow
from ..stats import Counters
from .client import AprsClient

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 50_000


class Ingest:
    """Runs an :class:`AprsClient` and hands every line, with its reception time, to ``consumer``.

    Counters: ``ingest.lines``, ``ingest.queue_dropped``, ``ingest.consumer_errors``.
    """

    def __init__(
        self,
        client: AprsClient,
        consumer: Callable[[datetime, str], None],
        counters: Counters,
        *,
        max_queue: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.client = client
        self.consumer = consumer
        self.counters = counters
        self.queue: queue.Queue[tuple[datetime, str]] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._reader_done = threading.Event()
        self._threads: list[threading.Thread] = []
        client.on_line = self.put

    def put(self, line: str) -> None:
        """Called by the reader thread for every line."""
        self.counters.inc("ingest.lines")
        try:
            self.queue.put_nowait((utcnow(), line))
        except queue.Full:
            self.counters.inc("ingest.queue_dropped")

    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._read, name="ogn-reader", daemon=True),
            threading.Thread(target=self._consume, name="ogn-consumer", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop reading, let the consumer finish what is queued, then return."""
        self._stop.set()
        self.client.stop()
        for thread in self._threads:
            thread.join(timeout)

    def _read(self) -> None:
        try:
            self.client.run(self._stop)
        finally:
            self._reader_done.set()

    def _consume(self) -> None:
        while True:
            try:
                received_at, line = self.queue.get(timeout=0.2)
            except queue.Empty:
                if self._reader_done.is_set():
                    return
                continue
            try:
                self.consumer(received_at, line)
            except Exception:  # noqa: BLE001 - one bad line must not stop the feed
                errors = self.counters.get("ingest.consumer_errors") + 1
                self.counters.inc("ingest.consumer_errors")
                if errors <= 20 or errors % 1000 == 0:
                    log.exception("Failed to process line #%d: %s", errors, line[:200])
