"""Every SQL example in the README must run against a real database and return something."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from prack.config import Settings
from prack.ogn.simulator import Simulator
from prack.regions import load_regions
from prack.runtime import Runtime

README = Path(__file__).resolve().parent.parent / "README.md"


def sql_blocks() -> list[str]:
    return re.findall(r"```sql\n(.*?)```", README.read_text(encoding="utf-8"), flags=re.S)


@pytest.fixture(scope="module")
def sample_db(tmp_path_factory):
    """A morning of simulated flying, recorded by the real tracker and finalizer."""
    data = tmp_path_factory.mktemp("readme")
    sim = Simulator.demo(datetime(2026, 7, 15, 8, 0, tzinfo=UTC), load_regions(["ch"])[0], pilots=6, seed=2)
    runtime = Runtime(Settings(data_dir=data, ddb_enabled=False, terrain_enabled=False), feed=False)
    runtime.prepare()
    for k in range(sim.duration_s() + 2):
        for line in sim.lines(sim.at(k)):
            runtime.consume(sim.at(k), line)
    runtime.tracker.close_all()
    runtime.finalizer.drain()
    runtime.db.dispose()
    return data / "prack.db"


def test_the_readme_has_the_examples_the_brief_asks_for():
    blocks = sql_blocks()
    assert len(blocks) >= 6
    text = "\n".join(blocks)
    for topic in (
        "flights per day",
        "activity per hour",
        "launch sites",
        "thermals",
        "receiver coverage",
        "fixes_v LIMIT 5",
    ):
        assert topic in text


@pytest.mark.parametrize("sql", sql_blocks(), ids=lambda s: s.strip().splitlines()[0][:50])
def test_each_readme_query_runs_and_returns_rows(sample_db, sql):
    con = sqlite3.connect(sample_db)
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    assert rows


def test_select_star_from_the_view_shows_real_units(sample_db):
    con = sqlite3.connect(sample_db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM fixes_v LIMIT 5").fetchall()
    con.close()
    assert len(rows) == 5
    for row in rows:
        assert 45.5 < row["lat"] < 48.0 and 5.5 < row["lon"] < 10.8 and 500 < row["alt_m"] < 4000
        assert row["utc"] < row["local_time"] and row["source"] in {
            "FLARM",
            "FANET",
            "OGN tracker",
            "OGN tracker (ADS-L)",
        }
