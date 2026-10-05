# prack: paraglider tracker (OGN to SQL)

Listens to the [Open Glider Network](https://www.glidernet.org) live feed, keeps **paragliders only** (OGN aircraft
type 7), cuts the stream into flights and stores every track point in an SQL database, with a minimal map on top:

- **Live:** the paragliders in the air, labelled, with trails; click one for its whole flight and a card; 2D or 3D.
- **History:** a calendar shaded by flights per day; pick a day to see all its flights, click one for the details.
- **Data:** one row per flight, about one fix per second (SQLite, or PostgreSQL), in real units through two views.

It runs on a Windows PC or a small Linux server (Docker, systemd), needs no build step and no CDN, and `prack demo`
works without internet.

## Quick start (Windows 10/11, PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1      # if scripts are blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install .
prack config                      # what will run, and the APRS-IS filter that will be sent
prack demo                        # simulated pilots, works offline: http://127.0.0.1:8000
prack diagnose 60                 # listen for 60 s and explain what arrives (needs internet)
prack run                         # record flights and serve them: http://127.0.0.1:8000 (Ctrl+C stops)
```

Open <http://127.0.0.1:8000>. `prack track` records without the web server (data in `data\prack.db`).
A real run needs internet for the OGN feed, the terrain tiles and the swisstopo base maps; `prack demo` only needs
the last two, and falls back to a plain background and invented altitudes without them.

## Quick start (Linux)

Needs Python 3.11 or newer (`python3 --version`; [older systems](#1-requirements) are covered in the manual below).

```sh
sudo apt install python3 python3-venv git      # Debian, Ubuntu. Fedora: sudo dnf install python3 git
git clone https://github.com/betafant/paratrack.git ~/prack
cd ~/prack
python3 -m venv .venv && . .venv/bin/activate
pip install .
prack demo                        # simulated pilots, works offline: http://127.0.0.1:8000
prack run                         # the real thing: record flights from OGN and serve them (Ctrl+C stops)
```

Open <http://127.0.0.1:8000> (`xdg-open http://127.0.0.1:8000`). The database and caches go to `data/` in the
directory you start prack from. To keep it running in the background and start it at boot, see
[Run it as a service](#3-run-it-as-a-service); for a server on the internet, [HTTPS and the
internet](#4-https-and-the-internet). macOS should work the same way (`brew install python@3.12`); it has not been tested.

## Commands

| Command | What it does |
|---|---|
| `prack config` | Show the effective configuration, regions and APRS-IS filter. |
| `prack diagnose [seconds]` | Connect to OGN (read only) and report which sources and aircraft types arrive and why each class is kept or dropped. `--record FILE` also saves every line. |
| `prack run [--host H] [--port P]` | The whole app: connect to OGN, record flights, serve the web API on `http://127.0.0.1:8000` ([details](#web-api)). Ctrl+C stops it cleanly. |
| `prack demo [--host H] [--port P] [--speed X] [--pilots N] [--history-days D]` | The same app fed by simulated pilots instead of OGN, so it works offline. It uses its own database `data/demo.db`, recreated at every start, and never touches `prack.db`. Needs no terrain download: if the tile server is reachable the simulated flights follow the real mountains, otherwise they use invented altitudes. `--speed` runs the clock faster (1 to 60, default 1). |
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
| `PRACK_HOST` / `PRACK_PORT` | `127.0.0.1` / `8000` | Web server. Anything but loopback without a password logs a warning. |
| `PRACK_AUTH_USER` / `PRACK_AUTH_PASSWORD` | | Optional HTTP basic auth, set both or neither. |
| `PRACK_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### Regions

A region is one TOML file: id, name, time zone, bounding box, map centre and zoom, base maps. See
[`prack/regions/ch.toml`](prack/regions/ch.toml). To add a country, copy that file, change the values, put it in a
folder and point `PRACK_REGIONS_DIR` at it, then list its id in `PRACK_REGIONS`. The box is where flights may
*start*; the feed is requested with `PRACK_OGN_FILTER_MARGIN_KM` extra so flights that cross the border keep
being tracked.

## The map

One full-screen map and a few small controls. Everything is served by the app itself (no CDN, no build step).

| Control | What it does |
|---|---|
| Marker | A paraglider in the air, turned to its heading. Grey dots are paragliders on the ground (chip **Ground**, off by default). |
| Label | `Mia · 1 587 m · −1.9`: name, altitude, vario. Names appear from zoom 9, the details from zoom 11 and for the selected one. Labels never overlap each other or other markers. The name is the FANET pilot name, else competition number and registration (only where the owner agreed to be identified in the OGN device database), else the callsign. |
| Faded line | The last six minutes of a paraglider, every position received. |
| Click a marker or label | Selects it: the whole flight is drawn, coloured by altitude, and a card shows altitude, height above ground, speed, vario, heading, take-off time, duration and distance. **Follow** keeps the map on it (dragging the map stops following), the corner button fits the whole flight. Escape or × closes the card. Selecting survives a paraglider that switches from FANET to FLARM. |
| **2D \| 3D** | 3D shows the terrain (exaggeration 1, pitch 60°, sky and fog), the track at its true altitude and a faint curtain down to the ground. Needs the terrain tiles (`PRACK_TERRAIN_ENABLED`, on by default). |
| **Live \| History** | Switches the map between the paragliders in the air and the flights of one day (below). |
| Layers button | Cycles through the base maps of the region file (swisstopo grey, colour, aerial). |
| Sun / moon | Light or dark theme. Dark unless you chose otherwise or your system asks for light. |
| Dot | Green: the OGN feed is up. Amber: connecting. Red: the browser has lost the server. |

The address bar holds the view, so it can be bookmarked and the back button works:
`#/live?sel=112880&view=3d&ll=46.80,8.23&z=11` (selected device address, 3D, camera) and
`#/day/2026-07-15?sel=1234` (a day, a selected flight). Times are shown in the region's time zone. On a phone the card
is a bottom sheet; all controls are at least 44 px; keyboard focus is always visible.

### History

Click **History** (or the date button, or the calendar) to look at the past. A day is the *local* calendar day on which
a flight started.

| Control | What it does |
|---|---|
| Date button | Opens the calendar: a month grid in which every day is shaded by its number of flights (square-root scaled against the busiest day of that month; hover or screen reader: "3 flights"). Click a day to show it. Keyboard: arrows move by day and week, Home/End to the ends of the week, Page Up/Down by month, Enter picks, Escape closes. Days after today cannot be picked. |
| ◀ ▶ **Today** | The previous and next day, and back to today. |
| The map | Every flight of the day as a thin line coloured by altitude (the simplified `preview` path, 500 points at most). Flights still in the air have no stored preview yet: their path is fetched from their track, a few at a time, and the day is loaded again every minute while you look at today. Hover a line for who and when; click it to select. |
| Selecting | The flight's whole track at full resolution is drawn, bold, with a green take-off and a red landing dot; the other flights fade. The card shows max altitude, altitude gain, best climb, take-off time (local to where it took off), airborne time, distance and the landing time. The corner button fits the track. |
| Strip at the bottom | One chip per flight (name and take-off time): click to select it and zoom to it. |
| 2D \| 3D | As in Live: the day's flights at their true altitude over the terrain. |

A flight only counts for the calendar and the day view once it has really flown (see [Web API](#web-api)); short hops
and hikes with a device in the pocket stay in the database.

Front-end files are in `prack/static/` (`js/` ES modules, `css/app.css`, all texts in `js/strings.js`). The libraries
are vendored and pinned, see [`prack/static/vendor/README.md`](prack/static/vendor/README.md): MapLibre GL JS 5.24.0
and deck.gl 9.4.0 (MapLibre 6 removed `map.transform`, which deck.gl 9.4 reads).

## Web API

`prack run` and `prack demo` serve everything under one port. All responses are JSON unless noted; times are epoch
seconds (UTC), dates are the local calendar day of the region, altitudes metres above sea level, speeds km/h.

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `{"ok": true}`; the only path that never asks for a password. |
| `GET /api/status` | Link state, counters, drop reasons per class, tracker, queue and database sizes. |
| `GET /api/config` | Version, regions (box, centre, zoom, base maps), terrain tile URL, stream settings. |
| `GET /api/live` | Full snapshot: every aircraft heard in the last 10 minutes with its last six minutes of trail. |
| `GET /api/live/stream` | Server-sent events, one message per second: only what changed (`aircraft` with new `pts`, `removed`), and every minute a full snapshot. A reconnecting client starts with a full one. |
| `GET /api/days?start=&end=&region=` | `[{date, total}]` for the calendar (default: the last 90 days). |
| `GET /api/days/{date}/flights` | Flights that started that day, with statistics and a simplified preview path. |
| `GET /api/flights/{id}` | One flight, same shape. |
| `GET /api/flights/{id}/track` | The whole track as columns (`t`, `lat`, `lon`, `alt`, `gnd`, `spd`, `vs`, `hdg`); also the points of a running flight not yet written to the database. |
| `GET /api/dem/{z}/{x}/{y}.png` | Terrain tile for the 3D view, fetched once, cached in `data/dem`. Only tiles near a configured region are served. |

A finished flight appears in the lists only if it counted as airborne (at least a minute more than 50 m above the
ground), and a running one once its altitude has changed by more than 50 m, so car rides and hikes with a device in
the pocket stay in the database but off the lists. The registration and competition number of a device are shown only when the OGN device database says
the owner agreed to be identified.

**Password.** Set `PRACK_AUTH_USER` and `PRACK_AUTH_PASSWORD` to ask for HTTP basic auth on everything but
`/api/health`. Without TLS the password travels in clear text: put a reverse proxy with HTTPS in front when the
port is reachable from the internet. Responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
`Referrer-Policy: same-origin`.

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

## Linux manual

Tested on Ubuntu 24.04 (x86-64) with Python 3.11, 3.12 and 3.13 (the whole test suite, browser tests included, passes
on all three), with the service set up as described below and run as an unprivileged user, and with the Docker image. ARM servers (Oracle Always Free Ampere, Raspberry Pi 64-bit) are expected to work: every dependency
publishes `aarch64` wheels, so nothing is compiled; that has not been run on ARM hardware here.

### 1. Requirements

Python **3.11 or newer** with `venv`, and `git` (or unpack a source archive). Check with `python3 --version`.
`pip install` refuses older Pythons with *"requires a different Python"*, and `python -m prack` says so too.

| System | Python | What to do |
|---|---|---|
| Ubuntu 24.04, Debian 12 and 13, Fedora 39+, Arch | 3.11 to 3.13 | `sudo apt install python3 python3-venv git` (Debian, Ubuntu), `sudo dnf install python3 git` (Fedora), `sudo pacman -S python git` (Arch) |
| Ubuntu 22.04 | 3.10: too old | `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update && sudo apt install python3.12 python3.12-venv git`, then use `python3.12` instead of `python3` below |
| RHEL, Oracle Linux, Rocky, AlmaLinux 8 and 9 | 3.6 or 3.9 by default: too old | `sudo dnf install python3.12 git` (or `python3.11`), then use `python3.12` instead of `python3` below |
| Anything else, or you would rather not touch the system Python | any | Use the [Docker image](#docker) |

The last three rows are the usual routes, not something this project has run. Debian and Ubuntu need the
`python3-venv` package, otherwise `python3 -m venv` fails with *"ensurepip is not available"*.

### 2. Install and run as yourself

```sh
git clone https://github.com/betafant/paratrack.git ~/prack
cd ~/prack
python3 -m venv .venv
. .venv/bin/activate              # in every new shell; or call ~/prack/.venv/bin/prack directly
pip install .
prack config                      # what will run: regions, the APRS-IS filter, where the data goes
prack demo                        # simulated pilots: http://127.0.0.1:8000 (Ctrl+C stops)
cp .env.example .env              # settings, all optional (see Configuration); prack reads .env from the directory it starts in
prack diagnose 60                 # 60 seconds of the real feed, and why every dropped line was dropped
prack run                         # record and serve
```

* **Where things are.** `data/prack.db` (SQLite) and `data/dem/` (terrain cache) in the current directory, or wherever
  `PRACK_DATA_DIR` points. Start prack from the same directory each time, or set `PRACK_DATA_DIR=$HOME/.local/share/prack`.
* **Who can connect.** Only this machine (`127.0.0.1`). From your laptop, tunnel instead of opening a port:
  `ssh -L 8000:127.0.0.1:8000 you@server`, then browse to <http://127.0.0.1:8000>. To listen on the network set
  `PRACK_HOST=0.0.0.0` **and** `PRACK_AUTH_USER` / `PRACK_AUTH_PASSWORD`; without a password prack logs a warning.
* **The server's time zone does not matter**: days are counted in the region's time zone. Its **clock** does: keep NTP
  running (`timedatectl`), see [Troubleshooting](#troubleshooting).
* **For a quick test in the background** use `tmux`, or `nohup .venv/bin/prack run > prack.log 2>&1 &`. For anything
  longer, use a service.

### 3. Run it as a service

systemd stops programs with SIGTERM; prack then writes what it has and exits normally (flights still open are closed as
gaps at the next start and resume if the pilot is still flying), and it is restarted if it crashes.

**For one user** (a PC or home server, no root): [`deploy/prack-user.service`](deploy/prack-user.service). It expects
the clone in `~/prack` and the virtual environment in `~/prack/.venv`, as above.

```sh
mkdir -p ~/.config/systemd/user
cp ~/prack/deploy/prack-user.service ~/.config/systemd/user/prack.service
systemctl --user daemon-reload
systemctl --user enable --now prack
systemctl --user status prack
journalctl --user -u prack -f     # the log
loginctl enable-linger "$USER"    # keep it running when you are logged out, and start it at boot
```

Settings live in `~/prack/.env`; after changing them, `systemctl --user restart prack`.

**For a server**, system-wide: [`deploy/prack.service`](deploy/prack.service) runs prack as its own unprivileged user
`prack`, with a strict sandbox (read-only system, private `/tmp`, no capabilities, only the network and its own data
directory). The program lives in `/opt/prack/venv`, the settings in `/etc/prack/prack.env`, the data in `/var/lib/prack`
(systemd creates it).

```sh
sudo useradd --system --no-create-home --home-dir /var/lib/prack --shell /usr/sbin/nologin prack
sudo python3 -m venv /opt/prack/venv
sudo /opt/prack/venv/bin/pip install ~/prack          # leaves build/ and *.egg-info owned by root in ~/prack: harmless
sudo install -d -m 750 -o root -g prack /etc/prack
sudo install -m 600 ~/prack/.env.example /etc/prack/prack.env
sudo nano /etc/prack/prack.env                        # at least PRACK_AUTH_USER and PRACK_AUTH_PASSWORD
sudo cp ~/prack/deploy/prack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prack
systemctl status prack
journalctl -u prack -f
```

Change settings with `sudo nano /etc/prack/prack.env && sudo systemctl restart prack`. The service listens on
`127.0.0.1:8000`; the next section puts HTTPS in front of it.

### 4. HTTPS and the internet

1. Set `PRACK_AUTH_USER` and `PRACK_AUTH_PASSWORD` (basic auth sends the password in clear text, so HTTPS is a must).
2. Keep prack on `127.0.0.1` and let a reverse proxy take ports 80 and 443. **Caddy** gets and renews the certificate by
   itself; point a DNS name at the machine first:

   ```sh
   sudo apt install caddy             # Debian 12+, Ubuntu 24.04; other systems: https://caddyserver.com/docs/install
   sudo tee /etc/caddy/Caddyfile <<'EOF'
   tracker.example.org {
   	reverse_proxy 127.0.0.1:8000 {
   		flush_interval -1
   	}
   }
   EOF
   sudo systemctl reload caddy
   ```

   `flush_interval -1` matters: the live view is a server-sent event stream and must not be buffered. With **nginx**
   (certificate from `certbot --nginx`) use, inside `server { ... }`:

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_http_version 1.1;
       proxy_buffering off;          # the live stream
       proxy_read_timeout 1h;
   }
   ```
3. Open the ports. `sudo ufw allow 80,443/tcp` (Ubuntu), or `sudo firewall-cmd --permanent --add-service={http,https} &&
   sudo firewall-cmd --reload` (Fedora, RHEL family). **Oracle Cloud** needs two things: ingress rules for TCP 80 and 443 in
   the subnet's security list (or network security group), and the instance's own firewall (Oracle's Ubuntu images ship
   iptables rules that reject new connections: look at `sudo iptables -L INPUT -n --line-numbers` and allow 80 and 443
   above the `REJECT` rule, then `sudo netfilter-persistent save`). Do not open port 8000.

### 5. Update, back up, uninstall

```sh
# update (user install)
cd ~/prack && git pull && .venv/bin/pip install . && systemctl --user restart prack
# update (system install)
cd ~/prack && git pull && sudo /opt/prack/venv/bin/pip install . && sudo systemctl restart prack
```

**Backup** of the SQLite database while prack runs (the database is in WAL mode; the standard library's online backup
copies a consistent state, no extra packages needed). Do it as the service user, so that no file in the data directory
ends up owned by root:

```sh
sudo -u prack python3 - <<'EOF'
import sqlite3
src = sqlite3.connect("/var/lib/prack/prack.db")     # user install: ~/prack/data/prack.db, without sudo -u prack
dst = sqlite3.connect("/var/lib/prack/backup.db")
src.backup(dst)
EOF
sudo install -m 600 -o "$USER" /var/lib/prack/backup.db ~/prack-backup.db && sudo rm /var/lib/prack/backup.db
```

(`sqlite3 prack.db ".backup backup.db"` does the same if you have the `sqlite3` tool; PostgreSQL: `pg_dump`.) Restore by
stopping prack and putting the file back as `prack.db`, owned by `prack`. The terrain cache in `dem/` can be thrown away.
Run other maintenance the same way, as the service user, for example
`sudo -u prack env PRACK_DATA_DIR=/var/lib/prack /opt/prack/venv/bin/prack repair` (add `PRACK_DATABASE_URL` for PostgreSQL).

**Uninstall** (system install): `sudo systemctl disable --now prack && sudo rm /etc/systemd/system/prack.service &&
sudo systemctl daemon-reload && sudo userdel prack && sudo rm -rf /opt/prack /etc/prack /var/lib/prack` (the last one
deletes the data). User install: `systemctl --user disable --now prack`, remove `~/.config/systemd/user/prack.service`
and `~/prack`.

## Docker

Any Linux server, including ARM; the image is about 220 MB, runs as an unprivileged user (uid 10001), has a health check
and keeps its data in the volume `prack-data`. It also sidesteps the Python version of the host.

```sh
git clone https://github.com/betafant/paratrack.git ~/prack && cd ~/prack
cp .env.example .env              # set PRACK_AUTH_USER and PRACK_AUTH_PASSWORD before anyone else can reach it
docker compose up -d --build      # http://127.0.0.1:8000, reachable from this machine only
docker compose logs -f prack
```

`docker-compose.yml` publishes the port on `127.0.0.1` only (`PRACK_PUBLISH_PORT` in `.env` changes the number). To serve
it on the internet with a certificate, point a DNS name at the machine, put `PRACK_DOMAIN=tracker.example.org` and the
password in `.env` and run `docker compose --profile https up -d`: Caddy (see [`deploy/Caddyfile`](deploy/Caddyfile)) takes
ports 80 and 443, gets a Let's Encrypt certificate and forwards to prack. Without a password do not do this.
To upgrade, update the source and run `docker compose up -d --build` again; the data stays in the volume. The
container's health check calls `/api/health`. `docker stop` (SIGTERM) makes the app write everything it has; flights
still open are closed as gaps at the next start and resume if the pilot is still flying.

## Resources and PostgreSQL

**Resources.** Measured with the simulator and 300 paragliders in the air at once: about 100 MB of RAM (55 MB when
idle) and a few percent of one core. The database grows by roughly 60 bytes per fix, so 100 one-hour flights are about
20 MB. Nothing is deleted automatically: remove old flights with SQL, or start a new database.

**PostgreSQL** instead of SQLite: `pip install ".[postgres]"` (already in the Docker image) and
`PRACK_DATABASE_URL=postgresql+psycopg://user:password@host/prack`. Tables and views are created at the first start.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `requires a different Python: 3.10.x not in '>=3.11'`, or *"prack needs Python 3.11 or newer"* | Your `python3` is too old: see [Requirements](#1-requirements). |
| `ensurepip is not available` (Debian, Ubuntu) | `sudo apt install python3-venv` (or `python3.12-venv`). |
| `Activate.ps1 cannot be loaded` (PowerShell) | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `cmd` and `.venv\Scripts\activate.bat`. |
| The map is empty | `prack diagnose 60` and compare with <https://live.glidernet.org>; every dropped line has a reason. The dot in the top bar shows whether the OGN feed is up. |
| The feed is up but nothing is recorded; `drops` in `/api/status` (or the report of `prack diagnose`) show `future` or `stale` | The machine's clock is wrong: positions more than 2 minutes ahead of it, or more than 30 minutes behind it, are rejected. Fix the time: `timedatectl set-ntp true` (systemd), or install `chrony`. |
| Grey background, no base map | The swisstopo tiles are blocked or offline; the region file's `tiles` can point to another server. |
| **3D** is greyed out | The terrain tiles are switched off (`PRACK_TERRAIN_ENABLED=false`). |
| Aircraft without heights above ground | No terrain tiles reached the server (check `data/dem`); flights recorded meanwhile have no `ground` values. |
| `Address already in use` | Another program uses port 8000 (`ss -ltnp \| grep :8000` shows which): `prack run --port 8001`. |
| `Permission denied: 'data'` | The directory you started in is not writable: start from your home directory, or set `PRACK_DATA_DIR`. |
| Works on the server, not from your laptop | By design it listens on `127.0.0.1` only: use an SSH tunnel or [HTTPS](#4-https-and-the-internet). |
| `systemctl --user` says *Failed to connect to bus* over SSH | `export XDG_RUNTIME_DIR=/run/user/$(id -u)`, and make sure `loginctl enable-linger "$USER"` was run. |
| The service fails with `status=203/EXEC` and `journalctl` shows `avc: denied` (SELinux: RHEL, Oracle Linux, Fedora) | Run `sudo restorecon -Rv /opt/prack`. If it persists, label the program as executable: `sudo semanage fcontext -a -t bin_t '/opt/prack/venv/bin(/.*)?' && sudo restorecon -Rv /opt/prack`. (The usual remedy for programs under `/opt`; not tried here.) |
| The live view arrives in bursts behind a reverse proxy | Turn proxy buffering off, see [HTTPS and the internet](#4-https-and-the-internet). |

## Development

```powershell
pip install -e ".[dev]"
playwright install chromium     # once, for the browser tests
pytest          # parser, filters, tracker, de-duplication, schema, finalizer, client, API, demo, end-to-end, CLI, UI, deploy
ruff check .
```

Two groups of tests concern the front-end. `tests/js/*.test.mjs` test its pure logic (live store, track buffer, URL
state, label placement, formatting) with `node --test`; they run from `pytest` when Node.js is installed.
`tests/test_ui.py` and `tests/test_ui_history.py` drive headless Chromium (software WebGL, no GPU needed) through the
real app with made-up flights: markers appear and are really drawn, labels follow the zoom, selecting shows the card and
the track, 2D and 3D, the calendar and the day view, themes, the phone layout, keyboard use, a broken stream, and no
console errors. They save screenshots to `tests/shots/` and are skipped when Playwright or Chromium is missing;
`PRACK_TEST_CHROMIUM` points them at a browser binary. `tests/test_deploy.py` checks the Dockerfile, the compose file
and the systemd unit (with `docker compose config` and `systemd-analyze verify` where they exist).

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
  api.py demo.py labels.py cli.py diagnose.py replay.py
  static/    index.html  css/app.css  vendor/ (MapLibre, deck.gl)
             js/  main.js map.js layers.js ui.js dom.js calendar.js store.js track.js trails.js previews.js
                  dayview.js declutter.js state.js dates.js api.js format.js colors.js strings.js theme-boot.js
  regions/ch.toml
  ogn/       constants.py parser.py filters.py pipeline.py survey.py client.py ingest.py ddb.py
             builder.py fake_server.py simulator.py
  tracking/  tracker.py rules.py finalizer.py flightstats.py maintenance.py
tests/        pytest files; js/ (node --test), test_ui*.py with uirig.py (Playwright), test_deploy.py
Dockerfile  docker-compose.yml  .dockerignore  deploy/ (prack.service, prack-user.service, Caddyfile)
```
