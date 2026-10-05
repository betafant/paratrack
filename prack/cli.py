"""Command line: ``prack run | demo | track | diagnose | replay | repair | config | ddb | init-db``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from . import __version__
from .config import ConfigError, Settings
from .db import Database
from .diagnose import open_cached_ddb, render_report, run_diagnose
from .models import Device, Flight
from .ogn.ddb import DdbError, DeviceDatabase
from .ogn.filters import FilterPolicy
from .ogn.pipeline import LineClassifier
from .ogn.survey import Survey
from .regions import aprs_filter, load_regions
from .replay import replay_file, replay_into
from .runtime import Runtime
from .stats import Counters
from .tracking.maintenance import merge_duplicate_flights, purge_implausible


def _serve(runtime: Runtime, settings: Settings, **app_options) -> None:
    import uvicorn

    from .api import create_app

    app = create_app(runtime, manage_runtime=True, **app_options)
    loopback = settings.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not settings.auth_enabled:
        logging.getLogger("prack").warning(
            "Listening on %s without a password: anyone who can reach this port can read everything. "
            "Set PRACK_AUTH_USER and PRACK_AUTH_PASSWORD.", settings.host,
        )  # fmt: skip
    print(f"prack {__version__} on http://{settings.host}:{settings.port}  (Ctrl+C stops)", flush=True)
    uvicorn.run(
        app, host=settings.host, port=settings.port, log_level=settings.log_level.lower(),
        access_log=settings.log_level == "DEBUG", proxy_headers=True, forwarded_allow_ips="127.0.0.1",
    )  # fmt: skip


def _bind_overrides(args: argparse.Namespace, settings: Settings) -> None:
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """The web app and the OGN receiver."""
    _bind_overrides(args, settings)
    runtime = Runtime(settings)
    _serve(runtime, settings)
    return 0


def cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    """The same app, fed by simulated pilots instead of OGN; works offline and never touches the real database."""
    from .demo import Demo, demo_database_path, remove_demo_database

    _bind_overrides(args, settings)
    remove_demo_database(settings.data_dir)
    settings.database_url = f"sqlite:///{demo_database_path(settings.data_dir).resolve().as_posix()}"
    settings.ddb_enabled = False
    runtime = Runtime(settings, feed=False)
    demo = Demo(runtime, speed=args.speed, pilots=args.pilots, history_days=args.history_days)
    print(f"Demo mode: simulated pilots, separate database {demo_database_path(settings.data_dir)}")
    demo.prepare()
    _serve(runtime, settings, on_start=demo.start, on_stop=demo.stop)
    return 0


def cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    regions = load_regions(settings.regions, settings.regions_dir)
    types = ", ".join(f"{t}" for t in settings.tracked_types)
    print(f"prack {__version__}")
    print(f"  database   {make_url(settings.db_url).render_as_string(hide_password=True)}")
    print("  regions    " + "; ".join(f"{r.id} ({r.name}, {r.timezone})" for r in regions))
    login = settings.ogn_callsign or "PRACK + 4 random digits"
    print(f"  OGN feed   {settings.ogn_host}:{settings.ogn_port}, login {login}")
    print(f"  filter     {aprs_filter(regions, settings.ogn_filter_margin_km)}")
    print(
        f"  tracked    aircraft types {types}; respect stealth {'yes' if settings.respect_stealth else 'no'}; "
        f"one fix per {settings.min_fix_interval:g} s"
    )
    terrain = f"zoom {settings.terrain_zoom}, {settings.terrain_url}" if settings.terrain_enabled else "disabled"
    print(f"  terrain    {terrain}")
    print(f"  devices    {'enabled, ' + settings.ddb_url if settings.ddb_enabled else 'disabled'}")
    print(f"  web        {settings.host}:{settings.port}, basic auth {'on' if settings.auth_enabled else 'off'}")
    return 0


def cmd_init_db(args: argparse.Namespace, settings: Settings) -> int:
    db = Database(settings.db_url)
    db.init()
    print(f"Database ready: {make_url(settings.db_url).render_as_string(hide_password=True)}")
    db.dispose()
    return 0


def cmd_ddb(args: argparse.Namespace, settings: Settings) -> int:
    db = Database(settings.db_url)
    db.init()
    ddb = DeviceDatabase(db, settings.ddb_url)
    try:
        count = ddb.refresh()
    except (httpx.HTTPError, DdbError) as exc:
        print(f"error: could not download the device database from {settings.ddb_url}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.dispose()
    st = ddb.stats()
    print(f"{count:,} devices stored; {st['untracked']:,} opted out of tracking, {st['unidentified']:,} not identified")
    return 0


def cmd_diagnose(args: argparse.Namespace, settings: Settings) -> int:
    regions = load_regions(settings.regions, settings.regions_dir)
    report = run_diagnose(settings, regions, args.seconds, record=args.record, out=print)
    print()
    print(report)
    return 0


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    path = Path(args.file)
    if not path.is_file():
        raise ConfigError(f"no such file: {path}")
    day = args.date or datetime.now(UTC).date()
    if args.database:
        settings.database_url = args.database
    if args.classify_only:
        counters, survey = Counters(), Survey()
        policy = FilterPolicy.from_settings(settings)
        ddb = open_cached_ddb(settings) if settings.ddb_enabled else None
        stats = replay_file(path, LineClassifier(policy, ddb, counters, survey), day=day)
        _print_replay_header(path, stats)
        print(render_report(counters=counters, survey=survey, policy=policy, ddb=ddb))
        return 0
    runtime = Runtime(settings, feed=False)
    runtime.prepare()
    if runtime.ddb is not None:
        runtime.ddb.load_from_db()  # the cached copy; a replay never downloads
    with runtime.db.session() as session:
        first_new = (session.scalar(select(func.max(Flight.id))) or 0) + 1
    stats = replay_into(runtime, path, day)
    _print_replay_header(path, stats)
    print(f"Stored in {make_url(runtime.db.url).render_as_string(hide_password=True)}")
    print(render_report(counters=runtime.counters, survey=runtime.survey, policy=runtime.policy, ddb=runtime.ddb))
    print()
    _print_flights(runtime.db, first_new)
    runtime.db.dispose()
    return 0


def _print_replay_header(path: Path, stats) -> None:
    span = (
        f"{stats.first:%Y-%m-%d %H:%M:%S} to {stats.last:%Y-%m-%d %H:%M:%S} UTC" if stats.first and stats.last else "-"
    )
    print(f"Replayed {stats.lines:,} lines from {path} ({span})")


def _print_flights(db: Database, first_id: int, limit: int = 40) -> None:
    with db.session() as session:
        rows = session.execute(
            select(Flight, Device.callsign, Device.pilot_name)
            .join(Device, Device.id == Flight.device_id)
            .where(Flight.id >= first_id)
            .order_by(Flight.start_time)
        ).all()
    airborne = sum(1 for flight, _, _ in rows if flight.airborne)
    print(f"=== Flights recorded by this replay: {len(rows)} ({airborne} airborne)")
    for flight, callsign, pilot in rows[:limit]:
        minutes = (flight.end_time - flight.start_time).total_seconds() / 60
        print(
            f"  #{flight.id:<5d} {callsign:10s} {pilot or '':10.10s} {flight.date} {minutes:6.1f} min  "
            f"{flight.fix_count:6d} fixes  {flight.close_reason or flight.status:7s} "
            f"{'airborne' if flight.airborne else 'not airborne'}"
        )
    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more")


def cmd_track(args: argparse.Namespace, settings: Settings) -> int:
    """Headless collector: connect to OGN and record flights until interrupted (the web app comes later)."""
    runtime = Runtime(settings)
    runtime.start()
    target = make_url(runtime.db.url).render_as_string(hide_password=True)
    print(f"Tracking {runtime.filter_expr} -> {target}  (Ctrl+C stops)")
    try:
        while True:
            time.sleep(args.interval)
            st = runtime.status()
            t, link = st["tracker"], st["link"]
            print(
                f"{datetime.now():%H:%M:%S} link={link['state']} lines={link['lines']:,} live={t['live']} "
                f"flying={t['flying']} flights opened={st['counters'].get('flights.opened', 0)} "
                f"closed={st['counters'].get('flights.closed', 0)} fixes={st['counters'].get('fixes.stored', 0):,} "
                f"db={(st['database']['size_bytes'] or 0) / 1e6:.1f} MB",
                flush=True,
            )
    except KeyboardInterrupt:
        print("Stopping ...")
    finally:
        runtime.stop()
    return 0


def cmd_repair(args: argparse.Namespace, settings: Settings) -> int:
    """Merge a pilot's flights that were recorded twice and delete impossible ones; finish unfinished flights."""
    runtime = Runtime(settings, feed=False)
    runtime.db.init()
    since = None if args.all else datetime.now(UTC) - timedelta(days=7)
    purged = purge_implausible(runtime.db, since=since)
    merged = merge_duplicate_flights(runtime.db, since=since, on_merged=runtime.finalizer.enqueue)
    runtime.finalizer.enqueue_unfinished()
    finished = runtime.finalizer.drain()
    print(f"{purged} impossible flights deleted, {merged} duplicate flights merged, {finished} flights finished")
    runtime.db.dispose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prack", description="Paraglider tracker: OGN live feed to SQL.")
    parser.add_argument("--version", action="version", version=f"prack {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("run", help="start the web app and the OGN receiver")
    p.add_argument("--host", help="address to listen on (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default 8000)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("demo", help="the web app with simulated pilots; works offline")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--speed", type=float, default=1.0, help="simulated seconds per real second (1 to 60, default 1)")
    p.add_argument("--pilots", type=int, default=12, help="pilots per wave (default 12)")
    p.add_argument("--history-days", type=int, default=2, help="past days to record first (default 2)")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("config", help="show the effective configuration and the APRS-IS filter")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("init-db", help="create the database tables")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("ddb", help="download the OGN device database now")
    p.set_defaults(func=cmd_ddb)

    p = sub.add_parser("diagnose", help="listen to the OGN feed and report sources, types and drop reasons")
    p.add_argument("seconds", nargs="?", type=float, default=60.0, help="how long to listen (default 60)")
    p.add_argument("--record", type=Path, metavar="FILE", help="also write every received line to FILE")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("track", help="record flights headless: connect to OGN and store them (Ctrl+C stops)")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between status lines (default 30)")
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("repair", help="merge duplicate flights, delete impossible ones, finish unfinished ones")
    p.add_argument("--all", action="store_true", help="look at all stored flights, not only the last week")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("replay", help="run a recorded APRS log through the tracker and into the database")
    p.add_argument("file")
    p.add_argument("--database", metavar="URL", help="store in this database instead (e.g. sqlite:///scratch.db)")
    p.add_argument("--classify-only", action="store_true", help="only parse and filter, store nothing")
    p.add_argument(
        "--date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="UTC date for lines without a recorded time (default today)",
    )
    p.set_defaults(func=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # a legacy Windows console must not crash on odd characters
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        raise SystemExit(0)
    try:
        settings = Settings.from_env()
        logging.basicConfig(
            level=getattr(logging, settings.log_level),
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        raise SystemExit(args.func(args, settings))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
