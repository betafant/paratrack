"""``prack diagnose``: listen to the feed for a while and explain what arrives and why things are dropped.

Compare the output with https://live.glidernet.org. If a source or an aircraft type that you see there is
missing here, the "Sources x aircraft types" table says where it went.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .db import utcnow
from .ogn.client import AprsClient, LinkStatus
from .ogn.constants import AIRCRAFT_TYPES, UNTYPED_SOURCES, type_name
from .ogn.ddb import DeviceDatabase
from .ogn.filters import FilterPolicy
from .ogn.ingest import Ingest
from .ogn.pipeline import LineClassifier
from .ogn.survey import Survey
from .regions import Region, aprs_filter
from .stats import Counters

KEPT_ROWS = 60

DROP_TEXT = {
    "type": "type not tracked",
    "no_tracking": "no-tracking flag",
    "stealth": "stealth flag",
    "ddb_untracked": "owner opted out (device database)",
}


def _table(headers: list[str], rows: list[list[str]], right: frozenset[int] = frozenset()) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        parts = [c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(cells)]
        return "  " + "  ".join(parts).rstrip()

    return [fmt(headers), fmt(["-" * w for w in widths]), *(fmt(r) for r in rows)]


def _type_label(group: dict) -> str:
    label = group["type_name"]
    reported = group["reported_type"]
    if reported != group["aircraft_type"]:
        label += f" (id says {reported}: {type_name(reported)})" if reported else " (no type sent, source default)"
    return label


def _result(group: dict, policy: FilterPolicy) -> str:
    outcomes = group["outcomes"]
    parts = []
    if outcomes.get("ok"):
        parts.append(f"tracked {outcomes['ok']:,}")
    for reason, count in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        if reason == "ok":
            continue
        text = DROP_TEXT.get(reason, reason)
        if reason == "type":
            wanted = ", ".join(f"{t} {AIRCRAFT_TYPES.get(t, '?')}" for t in sorted(policy.tracked_types))
            if group["reported_type"] != group["aircraft_type"] and group["source"] == "ADS-B":
                text = "ADS-B 'paraglider/hang glider' category is really a microlight, forced to unknown"
            elif group["aircraft_type"] == 0 and group["source"] in UNTYPED_SOURCES:
                text = "this source sends no aircraft type"
            else:
                text = f"type not tracked (PRACK_TRACKED_TYPES={wanted})"
        parts.append(f"dropped {count:,}: {text}")
    return "; ".join(parts)


def render_report(
    *,
    counters: Counters,
    survey: Survey,
    policy: FilterPolicy,
    link: LinkStatus | None = None,
    filter_expr: str = "",
    ddb: DeviceDatabase | None = None,
    now_epoch: float | None = None,
) -> str:
    """The full text report. ASCII only, so it prints on any Windows console."""
    lines: list[str] = []
    add = lines.append
    snapshot = survey.snapshot()

    if link is not None:
        add("=== OGN link")
        add(f"  server   {link.server or '-'}")
        add(f"  login    {link.login or '-'}")
        add(f"  state    {link.state} (reconnects {link.reconnects}, last error: {link.last_error or '-'})")
        add(f"  filter   {filter_expr}")
        add(
            f"  lines    {link.lines:,} delivered, {link.comments:,} server comments, "
            f"{counters.get('ingest.queue_dropped'):,} dropped because the queue was full"
        )
        add("")

    drops = counters.snapshot("drop.")
    skips = counters.snapshot("skip.")
    add("=== Lines")
    add(
        f"  total {counters.get('lines'):,}  |  passed the filters {counters.get('accepted'):,}  |  "
        f"dropped {sum(drops.values()):,}  |  skipped {sum(skips.values()):,}  |  "
        f"pilot names {counters.get('status'):,}"
    )
    if drops:
        add("  dropped by reason: " + ", ".join(f"{k[5:]} {v:,}" for k, v in drops.items()))
    if skips:
        add("  skipped by reason: " + ", ".join(f"{k[5:]} {v:,}" for k, v in skips.items()))
    add("")

    add("=== Sources x aircraft types: what arrives, and what happens to it")
    if snapshot["groups"]:
        rows = [
            [g["source"], _type_label(g), f"{g['devices']:,}", f"{g['positions']:,}", _result(g, policy)]
            for g in snapshot["groups"]
        ]
        lines.extend(_table(["SOURCE", "AIRCRAFT TYPE", "DEVICES", "POSITIONS", "RESULT"], rows, frozenset({2, 3})))
    else:
        add("  no aircraft positions received")
    add("")

    if snapshot["skips"]:
        add("=== Skipped lines by source (receivers and weather stations are expected)")
        rows = [[s["source"], s["reason"], f"{s['count']:,}"] for s in snapshot["skips"]]
        lines.extend(_table(["SOURCE", "REASON", "COUNT"], rows, frozenset({2})))
        add("")

    kept = survey.kept_devices()
    add(f"=== Aircraft that pass the filters ({len(kept)}); compare with https://live.glidernet.org")
    if kept:
        reference = now_epoch if now_epoch is not None else max(d.epoch for d in kept)
        rows = [
            [
                d.ident,
                d.source,
                f"{d.alt_m:,.0f}",
                "-" if d.speed_kmh is None else f"{d.speed_kmh:.0f}",
                f"{d.lat:.4f}, {d.lon:.4f}",
                f"{max(0, round(reference - d.epoch))} s",
            ]
            for d in kept[:KEPT_ROWS]
        ]
        lines.extend(
            _table(["DEVICE", "SOURCE", "ALT m", "KM/H", "POSITION", "LAST HEARD"], rows, frozenset({2, 3, 5}))
        )
        if len(kept) > KEPT_ROWS:
            add(f"  ... and {len(kept) - KEPT_ROWS} more")
    else:
        add("  none")
    add("")

    if snapshot["samples"]:
        add("=== Lines that looked like aircraft but could not be decoded (please report these)")
        for reason, samples in snapshot["samples"].items():
            for sample in samples:
                add(f"  [{reason}] {sample}")
        add("")

    add("=== Device database")
    if ddb is None:
        add("  not loaded: owners' opt-outs (tracked=N) are NOT applied")
    else:
        st = ddb.stats()
        add(
            f"  {st['devices']:,} devices, {st['untracked']:,} opted out of tracking, "
            f"{st['unidentified']:,} not identified, updated {st['updated_at'] or '-'}"
        )
    return "\n".join(lines)


def open_cached_ddb(settings: Settings) -> DeviceDatabase | None:
    """The device database copy stored in the database, if there is one (never creates a database)."""
    from sqlalchemy.engine import make_url

    from .db import Database

    url = make_url(settings.db_url)
    if url.get_backend_name() == "sqlite" and not (url.database and Path(url.database).is_file()):
        return None
    db = Database(settings.db_url)
    try:
        ddb = DeviceDatabase(db, settings.ddb_url)
        return ddb if ddb.load_from_db() else None
    except Exception:  # noqa: BLE001 - e.g. the table does not exist yet
        return None


def _load_ddb(settings: Settings, out: Callable[[str], None]) -> DeviceDatabase | None:
    if not settings.ddb_enabled:
        out("Device database disabled (PRACK_DDB_ENABLED=false).")
        return None
    ddb = DeviceDatabase(None, settings.ddb_url)
    try:
        out(f"Downloading the device database from {settings.ddb_url} ...")
        ddb.refresh()
        return ddb
    except Exception as exc:  # noqa: BLE001
        out(f"  could not download it ({type(exc).__name__}: {exc})")
    cached = open_cached_ddb(settings)
    out("  using the cached copy." if cached else "  no cached copy either: opt-outs will NOT be applied.")
    return cached


def run_diagnose(
    settings: Settings,
    regions: list[Region],
    seconds: float,
    *,
    record: Path | None = None,
    out: Callable[[str], None] = print,
) -> str:
    """Listen for ``seconds``, print progress through ``out`` and return the final report."""
    counters, survey = Counters(), Survey()
    policy = FilterPolicy.from_settings(settings)
    ddb = _load_ddb(settings, out)
    classifier = LineClassifier(policy, ddb, counters, survey)
    filter_expr = aprs_filter(regions, settings.ogn_filter_margin_km)
    client = AprsClient(
        settings.ogn_host, settings.ogn_port, settings.resolved_callsign(), filter_expr, lambda _l: None
    )

    record_file = record.open("a", encoding="utf-8") if record else None

    def consume(received_at: datetime, line: str) -> None:
        if record_file is not None:
            record_file.write(f"{received_at.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%f}"[:-3] + f"Z {line}\n")
        classifier.classify(line, received_at)

    ingest = Ingest(client, consume, counters)
    out(
        f"Listening to {settings.ogn_host}:{settings.ogn_port} as {client.callsign} for {seconds:g} s, "
        f"filter {filter_expr}"
    )
    if record:
        out(f"Recording every line to {record} (replay it with: prack replay {record})")
    ingest.start()
    started = time.monotonic()
    try:
        next_progress = 10.0
        while (elapsed := time.monotonic() - started) < seconds:
            time.sleep(min(0.5, seconds - elapsed))
            if elapsed >= next_progress:
                next_progress += 10.0
                out(
                    f"  {elapsed:4.0f} s  link={client.status.state}  lines={client.status.lines:,}  "
                    f"aircraft passing filters={len(survey.kept_devices())}"
                )
    except KeyboardInterrupt:
        out("Interrupted, reporting what was received so far.")
    link = copy.copy(client.status)  # as it was while listening, before the shutdown changes the state
    ingest.stop()
    if record_file is not None:
        record_file.close()
    return render_report(
        counters=counters,
        survey=survey,
        policy=policy,
        link=link,
        filter_expr=filter_expr,
        ddb=ddb,
        now_epoch=utcnow().timestamp(),
    )
