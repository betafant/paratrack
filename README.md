# prack: paraglider tracker (OGN to SQL)

Listens to the [Open Glider Network](https://www.glidernet.org) live feed, keeps **paragliders only**, cuts the
stream into flights and stores every track point in an SQL database, with a minimal live map on top.

> **Status: milestone 1 of 5 (ingest).** The feed client, parser, filters, device database, `diagnose` and
> `replay` work and are tested. Flight detection, the database of flights, the API and the map come in the next
> milestones.

## Quick start (Windows 10/11, PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1      # if scripts are blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install .
prack config                      # what will run, and the APRS-IS filter that will be sent
prack diagnose 60                 # listen for 60 s and explain what arrives (needs internet)
```

Linux and macOS: `python3 -m venv .venv && . .venv/bin/activate && pip install .`

## Commands

| Command | What it does |
|---|---|
| `prack config` | Show the effective configuration, regions and APRS-IS filter. |
| `prack diagnose [seconds]` | Connect to OGN (read only) and report which sources and aircraft types arrive and why each class is kept or dropped. `--record FILE` also saves every line. |
| `prack replay FILE [--date YYYY-MM-DD]` | Run a recorded log through the same parser and filters. Plain APRS lines get their date from `--date`. |
| `prack ddb` | Download the OGN device database into the local database. |
| `prack init-db` | Create the database tables. |

`prack diagnose` is the tool to use when something is missing on the map: compare its table with
<https://live.glidernet.org>. Every line that is not used is counted under a reason.

## Configuration

Environment variables with the prefix `PRACK_`, or a `.env` file in the working directory (see
[`.env.example`](.env.example)). Environment variables win over the file.

| Variable | Default | Meaning |
|---|---|---|
| `PRACK_REGIONS` | `ch` | Region files to use, comma separated. |
| `PRACK_REGIONS_DIR` | | Folder with your own region files. |
| `PRACK_OGN_HOST` / `PRACK_OGN_PORT` | `aprs.glidernet.org` / `14580` | APRS-IS server. |
| `PRACK_OGN_CALLSIGN` | `PRACK` + 4 digits | Read-only login name, at most 9 characters. |
| `PRACK_OGN_FILTER_MARGIN_KM` | `30` | Listen this far beyond the region box. |
| `PRACK_TRACKED_TYPES` | `7` | OGN aircraft types to keep (7 paraglider). |
| `PRACK_RESPECT_STEALTH` | `true` | Drop devices with the stealth flag. |
| `PRACK_MIN_FIX_INTERVAL` | `1` | Seconds between stored fixes per aircraft. |
| `PRACK_DDB_ENABLED` / `PRACK_DDB_URL` | `true` / OGN download | Device database (opt-outs, registrations). |
| `PRACK_DATA_DIR` | `data` | SQLite file and caches. |
| `PRACK_DATABASE_URL` | | Empty: SQLite. PostgreSQL: `postgresql+psycopg://user:pw@host/prack` (`pip install ".[postgres]"`). |
| `PRACK_HOST` / `PRACK_PORT` | `127.0.0.1` / `8000` | Web server (milestone 3). |
| `PRACK_AUTH_USER` / `PRACK_AUTH_PASSWORD` | | Optional HTTP basic auth, set both or neither. |
| `PRACK_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### Regions

A region is one TOML file: id, name, time zone, bounding box, map centre and zoom, base maps. See
[`prack/regions/ch.toml`](prack/regions/ch.toml). To add a country, copy that file, change the values, put it in a
folder and point `PRACK_REGIONS_DIR` at it, then list its id in `PRACK_REGIONS`. The box is where flights may
*start*; the feed is requested with `PRACK_OGN_FILTER_MARGIN_KM` extra so flights that cross the border keep
being tracked.

## What is filtered, and why

Every line is counted; nothing disappears silently (`prack diagnose` prints the counters).

* Only aircraft type 7 (paraglider) by default. The ADS-B "ultralight / hang glider / paraglider" category is
  forced to *unknown*: real paragliders carry no ADS-B transponder, microlights do.
* Privacy is respected: devices with the no-tracking flag, the stealth flag (switchable) or an opt-out in the
  device database (`tracked = N`) are dropped. Registration and competition number are shown only for devices
  that agreed to be identified.
* Ground stations, weather stations and server beacons are recognised and skipped.
* All known id formats are decoded (FLARM, FANET, OGN tracker, Naviter 40 bit, Wingman, AirMate, and the
  senders without an id such as Flymaster).

## Development

```powershell
pip install -e ".[dev]"
pytest          # parser golden lines, filters, client against a fake OGN server, diagnose, replay, CLI
ruff check .
```

The tests need no internet: `prack.ogn.fake_server.FakeOgnServer` speaks enough APRS-IS for the client, and
`prack.ogn.builder` builds synthetic beacons.

## Layout

```
prack/
  config.py regions.py db.py models.py stats.py cli.py diagnose.py replay.py
  regions/ch.toml
  ogn/  constants.py parser.py filters.py pipeline.py survey.py
        client.py ingest.py ddb.py builder.py fake_server.py
tests/
```
