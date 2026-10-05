"""diagnose, replay and the command line, end to end against the fake OGN server."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from prack.cli import main
from prack.config import Settings
from prack.diagnose import render_report, run_diagnose
from prack.ogn.builder import build_position, build_status
from prack.ogn.ddb import DeviceDatabase
from prack.ogn.fake_server import FakeOgnServer
from prack.ogn.filters import FilterPolicy
from prack.ogn.pipeline import LineClassifier
from prack.ogn.survey import Survey
from prack.regions import load_regions
from prack.replay import ReplayClock, read_log, replay_file
from prack.stats import Counters

from .conftest import utc

NOON = utc(2026, 7, 15, 12, 0, 0)
LT24 = "FLRDDE48A>OGLT24,qAS,LT24:/102606h4030.47N/00338.38W'000/018/A=002267 id25387 +000fpm GPS"
RECEIVER = "LILH>OGNSDR,TCPIP*,qAC,GLIDERN2:/132201h4457.61NI00900.58E&/A=000423"
WEATHER = "FNT0828B8>OGNFNT,qAS,Huenenb2:/210414h4710.43N/00826.96E_152/001g002t057r000p000h48b10227 0.0dB"


def mixed_feed(with_comment: bool = True) -> list[str]:
    """What a busy minute looks like: paragliders and everything that must not become a flight.

    39 lines; server comments (``#``) never reach a client as lines, so the live-feed tests leave that one out.
    """
    now = datetime.now(UTC)

    def pos(address: str, **kw) -> str:
        return build_position(now, 46.6, 8.2, 2200, address=address, **kw)

    lines = [pos(f"00000{i % 4}") for i in range(20)]  # 4 FLARM paragliders, 20 positions
    lines += [pos("0000A0", aircraft_type=1) for _ in range(3)]  # gliders
    lines += [pos("0000B0", aircraft_type=6)]  # hang glider
    lines += [pos("3FF19F", prefix="ICA", tocall="OGADSB", address_type=1, speed_kmh=300)] * 2  # ADS-B "paraglider"
    lines += [pos("9A1B2C", prefix="FMT", tocall="OGFLYM", include_id=False)] * 2  # Flymaster
    lines += [pos("0000C0", stealth=True), pos("0000D0", no_tracking=True)]
    lines += [pos("1103CE", prefix="FNT", tocall="OGNFNT", errors=None, freq_khz=None, gps=None)]
    lines += [LT24, LT24, RECEIVER, WEATHER, "complete garbage"]
    if with_comment:
        lines.append("# comment")
    lines += [build_status(now, "Mia", address="1103CE")]
    lines += [pos("0000E0", include_id=False)]  # FLARM position without an id
    return lines


def classifier_for(policy: FilterPolicy | None = None, ddb=None):
    counters, survey = Counters(), Survey()
    return LineClassifier(policy or FilterPolicy(), ddb, counters, survey), counters, survey


# ---------------------------------------------------------------- replay


def test_log_reader_understands_stamped_and_plain_lines(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text(f"\n2026-07-15T18:30:37.512Z {LT24}\n   \n{LT24}\n2026-07-15T18:30:38Z # x\n", encoding="utf-8")
    assert list(read_log(path)) == [
        (datetime(2026, 7, 15, 18, 30, 37, 512000, tzinfo=UTC), LT24),
        (None, LT24),
        (datetime(2026, 7, 15, 18, 30, 38, tzinfo=UTC), "# x"),
    ]


def test_plain_lines_get_their_date_from_the_option_and_roll_over_midnight(tmp_path):
    times = [
        utc(2026, 7, 15, 23, 59, 50),
        utc(2026, 7, 15, 23, 59, 58),
        utc(2026, 7, 16, 0, 0, 3),
        utc(2026, 7, 16, 0, 5),
    ]
    path = tmp_path / "plain.log"
    path.write_text("\n".join(build_position(t, 46.6, 8.2, 2200) for t in times), encoding="utf-8")
    stamps = []
    c, _, _ = classifier_for()
    stats = replay_file(path, c, day=date(2026, 7, 15), sink=lambda r, received: stamps.append(r.beacon.timestamp))
    assert stamps == times and stats.lines == 4
    assert stats.first == times[0] and stats.last == times[-1]


def test_recorded_reception_times_win_over_the_date_option(tmp_path):
    line = build_position(utc(2026, 7, 15, 18, 30, 37), 46.6, 8.2, 2200)
    path = tmp_path / "rec.log"
    path.write_text(f"2026-07-15T18:30:39.100Z {line}\n", encoding="utf-8")
    seen = []
    c, _, _ = classifier_for()
    replay_file(path, c, day=date(2001, 1, 1), sink=lambda r, received: seen.append((received, r.beacon.timestamp)))
    assert seen == [(datetime(2026, 7, 15, 18, 30, 39, 100000, tzinfo=UTC), utc(2026, 7, 15, 18, 30, 37))]


def test_the_clock_starts_at_noon_when_the_first_line_has_no_time():
    clock = ReplayClock(date(2026, 7, 15))
    assert clock.reference(None, "garbage") == utc(2026, 7, 15, 12)
    assert ReplayClock(date(2026, 7, 15)).reference(None, "X>Y,qAS,Z:/256000h") == utc(2026, 7, 15, 12)


def test_replay_runs_the_same_filters_as_the_live_feed(tmp_path):
    path = tmp_path / "mixed.log"
    path.write_text("\n".join(mixed_feed()), encoding="utf-8")
    c, counters, survey = classifier_for()
    replay_file(path, c, day=datetime.now(UTC).date())
    assert counters.snapshot() == {
        "accepted": 23,  # 20 FLARM + 1 FANET + 2 Flymaster
        "drop.no_tracking": 1,
        "drop.stealth": 1,
        "drop.type": 8,  # 3 gliders, 1 hang glider, 2 ADS-B "paragliders", 2 LiveTrack24
        "lines": 39,
        "skip.comment": 1,
        "skip.malformed": 1,
        "skip.no_id": 1,
        "skip.receiver": 1,
        "skip.weather": 1,
        "status": 1,
    }


# ---------------------------------------------------------------- report


def test_report_explains_every_class(tmp_path):
    path = tmp_path / "mixed.log"
    path.write_text("\n".join(mixed_feed()), encoding="utf-8")
    c, counters, survey = classifier_for()
    replay_file(path, c, day=datetime.now(UTC).date())
    report = render_report(counters=counters, survey=survey, policy=FilterPolicy())
    rows = {tuple(line.split()[:2]): line for line in report.splitlines() if line.startswith("  ")}
    flarm_pg = rows[("FLARM", "paraglider")]
    assert (
        "tracked 20" in flarm_pg and "dropped 1: stealth flag" in flarm_pg and "dropped 1: no-tracking flag" in flarm_pg
    )
    assert "type not tracked (PRACK_TRACKED_TYPES=7 paraglider)" in rows[("FLARM", "glider")]
    assert "id says 7: paraglider" in rows[("ADS-B", "unknown")] and "microlight" in rows[("ADS-B", "unknown")]
    assert "this source sends no aircraft type" in rows[("LiveTrack24", "unknown")]
    assert "tracked 2" in rows[("Flymaster", "paraglider")]
    assert "no type sent, source default" in rows[("Flymaster", "paraglider")]
    assert "FLARM   no_id" in report  # the position without an id is listed under its source
    assert "pilot names 1" in report and "NOT applied" in report  # no device database was given
    assert (
        "Aircraft that pass the filters (6)" in report and "FLR000000" in report
    )  # 4 FLARM + 1 FANET + 1 Flymaster pilot
    assert "[no_id]" in report and "[malformed]" in report  # samples of what could not be decoded


def test_report_mentions_the_device_database_when_there_is_one():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"devices": [{"device_id": "AAAAAA", "tracked": "N"}]})
    )
    ddb = DeviceDatabase(None, "https://x", transport=transport)
    ddb.refresh()
    c, counters, survey = classifier_for(ddb=ddb)
    c.classify(build_position(NOON, 46.6, 8.2, 2200, address="AAAAAA"), NOON)
    report = render_report(counters=counters, survey=survey, policy=FilterPolicy(), ddb=ddb)
    assert "owner opted out (device database) 1" not in report  # shown as "dropped 1: owner opted out ..."
    assert "dropped 1: owner opted out (device database)" in report
    assert "1 devices, 1 opted out of tracking" in report


def test_report_for_an_empty_feed():
    report = render_report(counters=Counters(), survey=Survey(), policy=FilterPolicy())
    assert "no aircraft positions received" in report and "none" in report


# ---------------------------------------------------------------- diagnose against the fake server


def test_diagnose_listens_reports_and_records(settings, tmp_path):
    feed = mixed_feed(with_comment=False)
    record = tmp_path / "capture.log"
    regions = load_regions(["ch"])
    with FakeOgnServer(feed, per_tick=len(feed), interval=0.05) as server:
        settings.ogn_host, settings.ogn_port = "127.0.0.1", server.port
        messages: list[str] = []
        report = run_diagnose(settings, regions, 1.0, record=record, out=messages.append)
    assert server.logins[0].startswith("user PRACK") and "pass -1" in server.logins[0]
    assert server.logins[0].endswith("filter a/48.120/5.456/45.480/10.944")
    assert any("Listening to 127.0.0.1" in m for m in messages) and any("Recording every line" in m for m in messages)
    assert "state    connected" in report
    assert "tracked 20" in report and "ADS-B" in report and "LiveTrack24" in report
    assert "Device database" in report

    # The recording holds every line (comments from the server are not lines) and replays identically.
    recorded = [line for _, line in read_log(record)]
    assert recorded == feed
    c, counters, _ = classifier_for()
    stats = replay_file(record, c, day=date(2001, 1, 1))
    assert stats.lines == len(feed) and counters.get("accepted") == 23


def test_diagnose_survives_an_unreachable_server(settings):
    with FakeOgnServer() as server:
        port = server.port
    settings.ogn_host, settings.ogn_port = "127.0.0.1", port
    report = run_diagnose(settings, load_regions(["ch"]), 0.5, out=lambda m: None)
    assert "no aircraft positions received" in report and "ConnectionRefusedError" in report


# ---------------------------------------------------------------- command line


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("PRACK_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRACK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PRACK_DDB_ENABLED", "false")
    monkeypatch.setenv("PRACK_TERRAIN_ENABLED", "false")  # tests never touch the network
    return tmp_path


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exit_info:
        main(list(argv))
    out = capsys.readouterr()
    return exit_info.value.code, out.out, out.err


def test_cli_without_arguments_prints_help(cli_env, capsys):
    code, out, _ = run_cli(capsys)
    assert code == 0 and "diagnose" in out and "replay" in out


def test_cli_version(cli_env, capsys):
    code, out, _ = run_cli(capsys, "--version")
    assert code == 0 and out.startswith("prack 0.")


def test_cli_config_shows_the_filter(cli_env, capsys):
    code, out, _ = run_cli(capsys, "config")
    assert code == 0
    assert "a/48.120/5.456/45.480/10.944" in out and "ch (Switzerland, Europe/Zurich)" in out
    assert "aircraft types 7" in out and "disabled" in out  # DDB disabled by the fixture


def test_cli_config_hides_database_passwords(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("PRACK_DATABASE_URL", "postgresql+psycopg://user:s3cret@db/prack")
    _, out, _ = run_cli(capsys, "config")
    assert "s3cret" not in out and "user:***@db" in out


def test_cli_reports_configuration_errors_without_a_traceback(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("PRACK_PORT", "eighty")
    code, out, err = run_cli(capsys, "config")
    assert code == 2 and err.startswith("error: PRACK_PORT='eighty'") and "Traceback" not in err


def test_cli_unknown_region(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("PRACK_REGIONS", "atlantis")
    code, _, err = run_cli(capsys, "config")
    assert code == 2 and "unknown region 'atlantis'" in err


def test_cli_init_db(cli_env, capsys):
    code, out, _ = run_cli(capsys, "init-db")
    assert code == 0 and "Database ready" in out and (cli_env / "data" / "prack.db").is_file()


def test_cli_ddb_downloads_into_the_database(cli_env, capsys, monkeypatch):
    payload = {"devices": [{"device_id": "AAAAAA", "tracked": "N"}, {"device_id": "BBBBBB", "identified": "N"}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    class Stub(DeviceDatabase):
        def __init__(self, db, url):
            super().__init__(db, url, transport=transport)

    monkeypatch.setattr("prack.cli.DeviceDatabase", Stub)
    code, out, _ = run_cli(capsys, "ddb")
    assert code == 0 and "2 devices stored; 1 opted out of tracking, 1 not identified" in out


def test_cli_ddb_failure_is_a_message_not_a_traceback(cli_env, capsys, monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(503))

    class Stub(DeviceDatabase):
        def __init__(self, db, url):
            super().__init__(db, url, transport=transport)

    monkeypatch.setattr("prack.cli.DeviceDatabase", Stub)
    code, _, err = run_cli(capsys, "ddb")
    assert code == 1 and err.startswith("error: could not download the device database")


def test_cli_replay(cli_env, capsys):
    log = cli_env / "feed.log"
    log.write_text("\n".join(mixed_feed()), encoding="utf-8")
    code, out, _ = run_cli(capsys, "replay", str(log))
    assert code == 0 and "Replayed 39 lines" in out and "tracked 20" in out
    code, _, err = run_cli(capsys, "replay", str(cli_env / "missing.log"))
    assert code == 2 and "no such file" in err
    code, _, err = run_cli(capsys, "replay", str(log), "--date", "tomorrow")
    assert code == 2 and "invalid" in err


def test_cli_diagnose(cli_env, capsys, monkeypatch):
    feed = mixed_feed(with_comment=False)
    with FakeOgnServer(feed, per_tick=len(feed), interval=0.05) as server:
        monkeypatch.setenv("PRACK_OGN_HOST", "127.0.0.1")
        monkeypatch.setenv("PRACK_OGN_PORT", str(server.port))
        code, out, _ = run_cli(capsys, "diagnose", "1", "--record", str(cli_env / "rec.log"))
    assert code == 0 and "Listening to 127.0.0.1" in out and "=== Sources x aircraft types" in out
    assert (cli_env / "rec.log").read_text(encoding="utf-8").count("\n") == len(feed)


def test_the_package_runs_as_a_module(cli_env):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", "prack", "--version"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and result.stdout.startswith("prack ")
    assert isinstance(Settings().data_dir, Path)


# ---------------------------------------------------------------- the tracker-backed commands


def sim_log(path, *, stamped: bool = False) -> int:
    """A short simulated morning as a log file; returns the number of lines."""
    from datetime import timedelta

    from prack.ogn.simulator import PilotSpec, Simulator

    short = {"flight_s": 120, "drop_m": 300.0, "stand_s": 300}
    sim = Simulator(
        datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        [
            PilotSpec("D00001", ("flarm", "fanet"), name="Mia", launch=(46.6453, 7.6511), **short),
            PilotSpec("D00002", ("fanet",), name="Jonas", launch=(46.55, 8.2), delay_s=100, **short),
            PilotSpec("A00001", aircraft_type=1, launch=(46.6453, 7.6511), **short),
        ],
        seed=3,
    )
    lines = []
    for k in range(sim.duration_s() + 1):
        now = sim.at(k)
        stamp = f"{now + timedelta(milliseconds=250):%Y-%m-%dT%H:%M:%S.%f}"[:-3] + "Z " if stamped else ""
        lines += [stamp + line for line in sim.lines(now)]
    path.write_text("\n".join(lines), encoding="utf-8")
    return len(lines)


@pytest.mark.parametrize("stamped", [False, True])
def test_cli_replay_runs_the_tracker_into_the_database(cli_env, capsys, stamped):
    log = cli_env / "morning.log"
    n = sim_log(log, stamped=stamped)
    code, out, _ = run_cli(capsys, "replay", str(log), "--date", "2026-07-15")
    assert code == 0 and f"Replayed {n:,} lines" in out and "Stored in sqlite:///" in out
    assert "=== Flights recorded by this replay: 2 (2 airborne)" in out
    assert "FLRD00001" in out and "Mia" in out and "FNTD00002" in out and "landed" in out
    assert "type not tracked (PRACK_TRACKED_TYPES=7 paraglider)" in out  # the glider is accounted for
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{cli_env / 'data' / 'prack.db'}")
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT callsign, pilot_name, airborne, close_reason FROM flights_v ORDER BY callsign")
        ).all()
    assert rows == [("FLRD00001", "Mia", 1, "landed"), ("FNTD00002", "Jonas", 1, "landed")]
    engine.dispose()


def test_cli_replay_into_another_database_leaves_the_main_one_alone(cli_env, capsys):
    log = cli_env / "morning.log"
    sim_log(log)
    code, out, _ = run_cli(
        capsys, "replay", str(log), "--date", "2026-07-15", "--database", f"sqlite:///{cli_env / 'scratch.db'}"
    )
    assert code == 0 and (cli_env / "scratch.db").is_file() and not (cli_env / "data" / "prack.db").exists()


def test_cli_replay_classify_only_stores_nothing(cli_env, capsys):
    log = cli_env / "morning.log"
    sim_log(log)
    code, out, _ = run_cli(capsys, "replay", str(log), "--date", "2026-07-15", "--classify-only")
    assert code == 0 and "Flights recorded" not in out and not (cli_env / "data").exists()


def test_cli_repair_merges_and_purges(cli_env, capsys):
    from prack.config import Settings
    from prack.db import Database

    from .factory import make_flight

    db = Database(Settings.from_env({"PRACK_DATA_DIR": str(cli_env / "data")}, dotenv=None).db_url)
    db.init()
    make_flight(db, address="AAAAAA", source="FLARM", seconds=300)
    make_flight(db, address="AAAAAA", source="FANET", seconds=300, step=4)
    make_flight(db, address="BBBBBB", spikes=4)
    db.dispose()
    code, out, _ = run_cli(capsys, "repair", "--all")
    assert code == 0 and "1 impossible flights deleted, 1 duplicate flights merged, 1 flights finished" in out


def test_cli_track_records_a_feed_until_interrupted(cli_env, capsys, monkeypatch):
    import time
    from datetime import timedelta

    from .test_runtime_e2e import all_lines, short_scenario, sim_span

    lines = all_lines(short_scenario(datetime.now(UTC) - timedelta(seconds=sim_span())))
    real_sleep = time.sleep
    intervals = []

    def sleep(seconds):
        if seconds < 1:  # short waits inside the program keep working
            return real_sleep(seconds)
        intervals.append(seconds)  # the status loop: first let the feed arrive, then press Ctrl+C
        real_sleep(2.0)
        if len(intervals) > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr("prack.cli.time.sleep", sleep)
    with FakeOgnServer(lines, per_tick=200, interval=0.005) as server:
        monkeypatch.setenv("PRACK_OGN_HOST", "127.0.0.1")
        monkeypatch.setenv("PRACK_OGN_PORT", str(server.port))
        code, out, _ = run_cli(capsys, "track", "--interval", "30")
    assert code == 0 and intervals == [30.0, 30.0]
    assert "Tracking a/48.120/5.456/45.480/10.944" in out and "link=connected" in out and "Stopping" in out
    assert "flights opened=3" in out
