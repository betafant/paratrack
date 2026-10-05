from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from prack.db import Database, make_engine, utcnow
from prack.ogn.ddb import DdbError, DdbInfo, DeviceDatabase, parse_ddb

PAYLOAD = {
    "devices": [
        {
            "device_type": "F",
            "device_id": "112880",
            "aircraft_model": "Gin Boomerang 12",
            "registration": "HB-3123",
            "cn": "X1",
            "tracked": "Y",
            "identified": "Y",
            "aircraft_type": "1",
        },
        {
            "device_type": "F",
            "device_id": "aabbcc",
            "aircraft_model": "Ozone Rush",
            "registration": "",
            "cn": "",
            "tracked": "N",
            "identified": "N",
        },
        {
            "device_type": "O",
            "device_id": "3FF19F",
            "aircraft_model": "",
            "registration": "D-1234",
            "cn": "Z",
            "tracked": "Y",
            "identified": "N",
            "aircraft_type": "x",
        },
    ]
}


def transport(payload=PAYLOAD, status=200, calls: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=payload) if status == 200 else httpx.Response(status)

    return httpx.MockTransport(handler)


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.init()
    yield database
    database.dispose()


# ---------------------------------------------------------------- database


def test_sqlite_is_tuned_for_one_writer_and_many_readers(db):
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000


def test_the_data_directory_is_created(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'a' / 'b' / 'x.db'}")
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    assert (tmp_path / "a" / "b" / "x.db").exists()
    engine.dispose()


def test_in_memory_database_is_shared_between_connections():
    database = Database("sqlite://")
    database.init()
    database.set_meta("k", "v")
    assert database.get_meta("k") == "v" and database.get_meta("missing") is None
    database.set_meta("k", "w")
    assert database.get_meta("k") == "w"


def test_a_failed_session_rolls_back(db):
    with pytest.raises(RuntimeError), db.session() as session:
        session.execute(text("INSERT INTO meta (key, value) VALUES ('a', 'b')"))
        raise RuntimeError("boom")
    assert db.get_meta("a") is None


# ---------------------------------------------------------------- parsing


def test_parse_ddb():
    entries = parse_ddb(PAYLOAD)
    assert set(entries) == {"112880", "AABBCC", "3FF19F"}
    gin = entries["112880"]
    assert (gin.model, gin.registration, gin.competition_id, gin.tracked, gin.identified) == (
        "Gin Boomerang 12",
        "HB-3123",
        "X1",
        True,
        True,
    )
    assert gin.ddb_aircraft_type == 1
    opted_out = entries["AABBCC"]
    assert (opted_out.tracked, opted_out.identified, opted_out.registration, opted_out.competition_id) == (
        False,
        False,
        None,
        None,
    )
    assert entries["3FF19F"].identified is False and entries["3FF19F"].ddb_aircraft_type is None


def test_registration_and_competition_number_only_when_identified():
    entries = parse_ddb(PAYLOAD)
    assert entries["112880"].shown_registration == "HB-3123" and entries["112880"].shown_competition_id == "X1"
    assert entries["3FF19F"].registration == "D-1234"
    assert entries["3FF19F"].shown_registration is None and entries["3FF19F"].shown_competition_id is None


def test_missing_flags_default_to_tracked_and_identified():
    (info,) = parse_ddb({"devices": [{"device_id": "ABCDEF"}]}).values()
    assert info.tracked and info.identified


def test_bad_rows_are_skipped_and_text_is_sanitised():
    payload = {
        "devices": [
            "junk",
            {"device_id": "12345"},
            {"device_id": "ZZZZZZ"},
            {"device_id": 7},
            {"device_id": "ABCDEF", "registration": "HB-\x00123\n" + "x" * 80},
        ]
    }
    entries = parse_ddb(payload)
    assert list(entries) == ["ABCDEF"] and entries["ABCDEF"].registration == "HB- 123 " + "x" * 24


def test_duplicate_addresses_keep_the_stricter_privacy_choice():
    payload = {
        "devices": [
            {"device_type": "F", "device_id": "ABCDEF", "registration": "A", "tracked": "Y", "identified": "Y"},
            {
                "device_type": "I",
                "device_id": "ABCDEF",
                "registration": "B",
                "cn": "7",
                "tracked": "N",
                "identified": "Y",
            },
        ]
    }
    (info,) = parse_ddb(payload).values()
    assert (info.tracked, info.identified, info.registration, info.competition_id) == (False, True, "A", "7")


@pytest.mark.parametrize("payload", [{}, [], {"devices": "x"}, None])
def test_unexpected_responses_are_errors(payload):
    with pytest.raises(DdbError):
        parse_ddb(payload)


# ---------------------------------------------------------------- download, cache, refresh


def test_refresh_downloads_stores_and_serves_lookups(db):
    calls: list = []
    ddb = DeviceDatabase(db, "https://ddb.example/download/?j=1", transport=transport(calls=calls))
    assert ddb.is_stale() and len(ddb) == 0
    assert ddb.refresh() == 3
    assert not ddb.is_stale() and len(ddb) == 3
    assert ddb.lookup("aabbcc") == DdbInfo("AABBCC", None, None, "Ozone Rush", False, False, 0)
    assert ddb.lookup("FFFFFF") is None
    assert calls[0].url == "https://ddb.example/download/?j=1" and calls[0].headers["user-agent"].startswith("prack/")
    assert ddb.stats()["untracked"] == 1 and ddb.stats()["unidentified"] == 2 and ddb.stats()["last_error"] is None


def test_the_cache_survives_a_restart(db):
    DeviceDatabase(db, "https://x", transport=transport()).refresh()
    restarted = DeviceDatabase(db, "https://x")
    assert restarted.load_from_db() == 3
    assert restarted.lookup("112880").registration == "HB-3123"
    assert restarted.updated_at is not None and not restarted.is_stale()


def test_refresh_replaces_the_old_copy(db):
    ddb = DeviceDatabase(db, "https://x", transport=transport())
    ddb.refresh()
    ddb._transport = transport({"devices": [{"device_id": "000001", "registration": "NEW"}]})
    assert ddb.refresh() == 1
    assert ddb.lookup("112880") is None and ddb.lookup("000001").registration == "NEW"
    fresh = DeviceDatabase(db, "https://x")
    assert fresh.load_from_db() == 1


@pytest.mark.parametrize("failure", ["http_error", "empty", "garbage"])
def test_a_failed_refresh_keeps_the_old_data(db, failure):
    ddb = DeviceDatabase(db, "https://x", transport=transport())
    ddb.refresh()
    ddb._transport = {
        "http_error": transport(status=503),
        "empty": transport({"devices": []}),
        "garbage": httpx.MockTransport(lambda request: httpx.Response(200, text="<html>nope</html>")),
    }[failure]
    with pytest.raises((httpx.HTTPError, DdbError, ValueError)):
        ddb.refresh()
    assert len(ddb) == 3 and ddb.lookup("AABBCC") is not None and ddb.stats()["last_error"]
    assert DeviceDatabase(db, "https://x").load_from_db() == 3  # the stored copy is untouched too


def test_staleness_follows_the_age_of_the_copy(db):
    ddb = DeviceDatabase(db, "https://x", transport=transport(), max_age=timedelta(hours=24))
    ddb.refresh()
    assert not ddb.is_stale()
    ddb.updated_at = utcnow() - timedelta(hours=25)
    assert ddb.is_stale()


def test_memory_only_mode_needs_no_database():
    ddb = DeviceDatabase(None, "https://x", transport=transport())
    assert ddb.refresh() == 3 and ddb.lookup("112880") is not None and ddb.load_from_db() == 0


def test_warns_when_nobody_has_opted_out(db, caplog):
    big = {"devices": [{"device_id": f"{i:06X}", "tracked": "Y"} for i in range(1200)]}
    with caplog.at_level(logging.WARNING, logger="prack.ogn.ddb"):
        DeviceDatabase(db, "https://x", transport=transport(big)).refresh()
    assert "opted out" in caplog.text and "PRACK_DDB_URL" in caplog.text


def test_background_thread_loads_the_cache_then_refreshes_when_stale(db):
    DeviceDatabase(db, "https://x", transport=transport()).refresh()  # a fresh cache exists
    calls: list = []
    ddb = DeviceDatabase(db, "https://x", transport=transport(calls=calls))
    stop = threading.Event()
    thread = ddb.start_background(stop)
    deadline = time.monotonic() + 3
    while len(ddb) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(3)
    assert len(ddb) == 3 and not calls and not thread.is_alive()  # served from the cache, no download


def test_background_thread_downloads_when_there_is_no_cache(db):
    calls: list = []
    ddb = DeviceDatabase(db, "https://x", transport=transport(calls=calls))
    stop = threading.Event()
    thread = ddb.start_background(stop)
    deadline = time.monotonic() + 3
    while len(ddb) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(3)
    assert len(ddb) == 3 and len(calls) == 1
