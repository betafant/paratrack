# prack: paraglider tracker (OGN to SQL)

Listens to the [Open Glider Network](https://www.glidernet.org) live feed, keeps **paragliders only**, cuts the
stream into flights and stores every track point in an SQL database, with a minimal live map on top.

> **Status: milestone 2 of 5 (tracker and database).** `prack track` records flights from the live OGN feed into
> SQL, with terrain height and statistics; `diagnose`, `replay` and `repair` work. The web API, the demo mode and the
> map come in the next milestones.

## Quick start (Windows 10/11, PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1      # if scripts are blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install .
prack config                      # what will run, and the APRS-IS filter that will be sent
prack diagnose 60                 # listen for 60 s and explain what arrives (needs internet)
prack track                       # record flights (Ctrl+C stops); data\prack.db
```

Linux and macOS: `python3 -m venv .venv && . .venv/bin/activate && pip install .`

## Commands

| Command | What it does |
|---|---|
| `prack config` | Show the effective configuration, regions and APRS-IS filter. |
| `prack diagnose [seconds]` | Connect to OGN (read only) and report which sources and aircraft types arrive and why each class is kept or dropped. `--record FILE` also saves every line. |
| `prack track` | Record flights headless: connect to OGN and store them until Ctrl+C. Prints a status line every 30 s. Open flights are closed as gaps at the next start and resume if the pilot is still flying. |
| `prack replay FILE [--date YYYY-MM-DD] [--database URL] [--classify-only]` | Run a recorded log through the parser, filters and tracker into the database (in simulated time) and list the flights it found. `--database` stores somewhere else (e.g. `sqlite:///scratch.db`); `--classify-only` stores nothing. Replaying a file twice records its flights twice. |
| `prack repair [--all]` | Merge a pilot's flights that were recorded over two protocols, delete flights of impossible "paragliders", finish unfinished flights. Runs over the last week at every start; `--all` covers everything. |
| `prack ddb` | Download the OGN device database into the local database. |
| `prack init-db` | Create the tables and the views `fixes_v` and `flights_v`. |

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
| `PRACK_TERRAIN_ENABLED` | `true` | Terrain height under every fix ([Terrarium](https://registry.opendata.aws/terrain-tiles/) tiles, cached in `data/dem`). |
| `PRACK_TERRAIN_URL` / `PRACK_TERRAIN_ZOOM` | AWS tiles / `12` | Tile URL (`{z}/{x}/{y}`) and the zoom used for heights: 12 is about 30 m per pixel in the Alps. |
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

## How a feed becomes flights

Per device, keyed by its 24 bit address:

| Step | Rule |
|---|---|
| Take-off | Two consecutive fixes at 15 km/h or more open a flight, with the last 60 s before them so the launch run is included. Only inside a region: a flight that began outside and flew in is not recorded. |
| Landing | Under 5 km/h within 150 m for 4 minutes (and under 80 m above known ground, otherwise a vario of 0.5 m/s or less). `landing_time` is when standing still began. |
| Gap | 20 minutes without data close the flight as `gap`. Airborne again within 90 minutes: the same flight resumes (coverage holes), unless the device vanished within 50 m of the ground, which means it landed. |
| Fixes | Every fix, at most one per `PRACK_MIN_FIX_INTERVAL`, `INSERT ... ON CONFLICT DO NOTHING` on (flight, second). |
| Sanity | Time stamps more than 2 min ahead or 30 min old are dropped, as are positions that are not newer than the last. A jump of over 2 km at over 500 km/h is a glitch (three in a row, then the new place is accepted). A paraglider faster than 130 km/h in 2 of its last 10 fixes is a mis-configured device: its flight is deleted and it is ignored. |
| Speed | The reported ground speed, or, when the sender does not report one (`000/000`), derived from the track over about 5 s. |
| One pilot, several protocols | FLARM, FANET, OGN tracker and ADS-L of one address are one aircraft when heard within 3 km and 10 minutes of each other. The best protocol heard in the last 30 s wins (FLARM > FANET > OGN tracker > ADS-L); the others only fill gaps longer than that. The FANET pilot name is kept whichever wins. If the better protocol shows up mid-flight the flight and device switch to it. |
| After the flight | A background finalizer fills terrain height per fix, computes distance, altitude gain (climbs of 5 m or more, so sensor noise does not add up), best climb and sink over 20 s, and decides `airborne` (at least 50 m above ground for a minute; without terrain data: a minute and either more than 100 m of altitude range or more than 20 km/h). Walking, driving and GPS noise are stored but not `airborne`. It also stores a simplified `preview` path (at most 500 points) for the day overview. |

## The data

SQLite by default (WAL mode), PostgreSQL by URL. All times are UTC; `flights.date` is the **local** date of the start
in the region's time zone.

| Table | Content |
|---|---|
| `devices` | One row per device; `callsign` is the identity of its best protocol, e.g. `FLR112880`. Registration and competition number only for owners who agreed to be identified. |
| `flights` | One row per flight: times, take-off and landing, statistics, `airborne`, `close_reason` (`landed`/`gap`), simplified `preview`. |
| `fixes` | One row per track point, **scaled integers** (about 60 bytes each, so a 2 h FLARM flight is about 0.4 MB). Do not query it directly. |
| `ddb`, `meta` | Cached device database; small key/value store. |

**Query the views, not the tables.** `fixes_v` has real units (`utc`, `local_time`, `lat`, `lon`, `alt_m`, `ground_m`,
`agl_m`, `speed_kmh`, `heading`, `vario_ms`, `turn_dps`, `source`, `receiver`, `signal_db`, ...) joined with pilot
and flight columns; `flights_v` has local dates and times and durations in minutes.

```sql
SELECT * FROM fixes_v LIMIT 5;
```

Ready-to-run examples (SQLite syntax; on PostgreSQL use `to_char(local_time, 'HH24')` for `substr(local_time, 12, 2)`):

```sql
-- flights per day
SELECT local_date, COUNT(*) AS flights, ROUND(AVG(duration_min), 1) AS avg_minutes, ROUND(MAX(max_alt_m)) AS highest_m
FROM flights_v WHERE airborne GROUP BY local_date ORDER BY local_date;
```

```sql
-- activity per hour of the day (local time): how many pilots were in the air
SELECT substr(local_time, 12, 2) AS hour, COUNT(DISTINCT flight_id) AS pilots
FROM fixes_v GROUP BY hour ORDER BY hour;
```

```sql
-- launch sites: take-off positions rounded to about a kilometre
SELECT ROUND(takeoff_lat, 2) AS lat, ROUND(takeoff_lon, 2) AS lon, COUNT(*) AS flights
FROM flights_v WHERE airborne GROUP BY 1, 2 ORDER BY flights DESC LIMIT 20;
```

```sql
-- thermals: seconds of sustained climb (> 0.5 m/s) while turning (> 4 deg/s), per flight
SELECT flight_id, callsign, COUNT(*) AS seconds_in_thermals, ROUND(AVG(vario_ms), 2) AS avg_climb_ms
FROM fixes_v WHERE vario_ms > 0.5 AND ABS(turn_dps) > 4
GROUP BY flight_id, callsign ORDER BY seconds_in_thermals DESC LIMIT 20;
```

```sql
-- receiver coverage: how many fixes each receiver heard, and how strongly
SELECT receiver, COUNT(*) AS fixes, ROUND(AVG(signal_db), 1) AS avg_signal_db
FROM fixes_v WHERE receiver IS NOT NULL GROUP BY receiver ORDER BY fixes DESC LIMIT 20;
```

With pandas:

```python
import sqlite3, pandas as pd

con = sqlite3.connect("data/prack.db")
fixes = pd.read_sql("SELECT * FROM fixes_v WHERE flight_id = 42", con, parse_dates=["utc", "local_time"])
fixes.plot(x="local_time", y=["alt_m", "ground_m"])
flights = pd.read_sql("SELECT * FROM flights_v WHERE airborne", con)
print(flights.groupby("local_date").distance_km.describe())
```

## Development

```powershell
pip install -e ".[dev]"
pytest          # parser, filters, tracker, de-duplication, schema, finalizer, client, end-to-end, CLI
ruff check .
```

To run the database tests on PostgreSQL as well, point `PRACK_TEST_PG_URL` at an empty scratch database (its tables
are dropped!): `PRACK_TEST_PG_URL=postgresql+psycopg://user@localhost/prack_test pytest` (needs `pip install ".[postgres]"`).

The tests need no internet: `prack.ogn.fake_server.FakeOgnServer` speaks enough APRS-IS for the client,
`prack.ogn.builder` builds synthetic beacons and `prack.ogn.simulator` flies whole synthetic pilots (FLARM and FANET
twins, noise that must be rejected). The end-to-end tests send such a feed over TCP into the tracker and check the
database.

## Layout

```
prack/
  config.py regions.py db.py models.py views.py units.py geo.py stats.py terrain.py runtime.py
  cli.py diagnose.py replay.py
  regions/ch.toml
  ogn/       constants.py parser.py filters.py pipeline.py survey.py client.py ingest.py ddb.py
             builder.py fake_server.py simulator.py
  tracking/  tracker.py rules.py finalizer.py flightstats.py maintenance.py
tests/
```
