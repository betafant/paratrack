from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from prack.config import Settings


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# Midday on the day the golden lines were recorded: close enough to every golden time stamp
# that the parser picks the same calendar day.
REF = utc(2026, 7, 15, 12, 0, 0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.ddb_enabled = False
    return s
