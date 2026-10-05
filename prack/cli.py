"""Command line: ``prack config | init-db | ddb | diagnose | replay``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url

from . import __version__
from .config import ConfigError, Settings
from .db import Database
from .diagnose import open_cached_ddb, render_report, run_diagnose
from .ogn.ddb import DdbError, DeviceDatabase
from .ogn.filters import FilterPolicy
from .ogn.pipeline import LineClassifier
from .ogn.survey import Survey
from .regions import aprs_filter, load_regions
from .replay import replay_file
from .stats import Counters


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
    counters, survey = Counters(), Survey()
    policy = FilterPolicy.from_settings(settings)
    ddb = open_cached_ddb(settings) if settings.ddb_enabled else None
    stats = replay_file(path, LineClassifier(policy, ddb, counters, survey), day=day)
    span = (
        f"{stats.first:%Y-%m-%d %H:%M:%S} to {stats.last:%Y-%m-%d %H:%M:%S} UTC" if stats.first and stats.last else "-"
    )
    print(f"Replayed {stats.lines:,} lines from {path} ({span})")
    print()
    print(render_report(counters=counters, survey=survey, policy=policy, ddb=ddb))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prack", description="Paraglider tracker: OGN live feed to SQL.")
    parser.add_argument("--version", action="version", version=f"prack {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

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

    p = sub.add_parser("replay", help="run a recorded APRS log through the parser and filters")
    p.add_argument("file")
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
