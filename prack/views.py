"""Analysis views: real units instead of scaled integers.

``fixes_v``   one row per track point: UTC and local time, lat/lon, altitude, height above ground, speed,
              heading, vario, turn rate, protocol, receiver quality, plus pilot and flight columns.
``flights_v`` one row per flight: local date and times, durations in minutes, take-off / landing, statistics.

Written for SQLite and PostgreSQL. SQLite has no time zone database, so local times use the offset stored
with each flight (``utc_offset_s``).
"""

from __future__ import annotations

from sqlalchemy import Connection, Engine, text

from .ogn.constants import SOURCE_CODES

VIEWS = ("fixes_v", "flights_v")


class _Sql:
    """The few expressions that differ between SQLite and PostgreSQL."""

    def __init__(self, dialect: str) -> None:
        self.pg = dialect == "postgresql"

    def epoch_utc(self, col: str) -> str:
        return f"to_timestamp({col}) AT TIME ZONE 'UTC'" if self.pg else f"datetime({col}, 'unixepoch')"

    def epoch_local(self, col: str, offset: str) -> str:
        if self.pg:
            return f"to_timestamp({col} + {offset}) AT TIME ZONE 'UTC'"
        return f"datetime({col} + {offset}, 'unixepoch')"

    def ts_utc(self, col: str) -> str:
        return col if self.pg else f"datetime({col})"

    def ts_local(self, col: str, offset: str) -> str:
        if self.pg:
            return f"({col} + {offset} * interval '1 second')"
        return f"datetime({col}, printf('%+d seconds', {offset}))"

    def minutes_between(self, end: str, start: str) -> str:
        if self.pg:
            return f"EXTRACT(EPOCH FROM ({end} - {start})) / 60.0"
        return f"(CAST(strftime('%s', {end}) AS INTEGER) - CAST(strftime('%s', {start}) AS INTEGER)) / 60.0"

    @staticmethod
    def real(expr: str, divisor: str) -> str:
        return f"CAST({expr} AS DOUBLE PRECISION) / {divisor}"


def _source_case() -> str:
    whens = " ".join(f"WHEN {code} THEN '{name}'" for name, code in SOURCE_CODES.items())
    return f"CASE f.src {whens} ELSE 'other' END"


def view_sql(dialect: str) -> dict[str, str]:
    q = _Sql(dialect)
    fixes_v = f"""
CREATE VIEW fixes_v AS
SELECT
    f.flight_id                                   AS flight_id,
    f.ts                                          AS ts,
    {q.epoch_utc("f.ts")}                         AS utc,
    {q.epoch_local("f.ts", "fl.utc_offset_s")}    AS local_time,
    {q.real("f.lat", "1000000.0")}                AS lat,
    {q.real("f.lon", "1000000.0")}                AS lon,
    {q.real("f.alt", "10.0")}                     AS alt_m,
    {q.real("f.ground", "10.0")}                  AS ground_m,
    {q.real("f.alt - f.ground", "10.0")}          AS agl_m,
    {q.real("f.speed", "10.0")}                   AS speed_kmh,
    f.track                                       AS heading,
    {q.real("f.climb", "100.0")}                  AS vario_ms,
    {q.real("f.turn", "10.0")}                    AS turn_dps,
    {_source_case()}                              AS source,
    f.receiver                                    AS receiver,
    {q.real("f.signal", "10.0")}                  AS signal_db,
    f.errors                                      AS bit_errors,
    {q.real("f.freq_offset", "10.0")}             AS freq_offset_khz,
    f.gps                                         AS gps,
    fl.date                                       AS flight_date,
    fl.region                                     AS region,
    fl.airborne                                   AS airborne,
    d.callsign                                    AS callsign,
    d.address                                     AS address,
    d.pilot_name                                  AS pilot_name,
    d.registration                                AS registration,
    d.competition_id                              AS competition_id,
    d.model                                       AS model
FROM fixes f
JOIN flights fl ON fl.id = f.flight_id
JOIN devices d ON d.id = fl.device_id
"""
    flights_v = f"""
CREATE VIEW flights_v AS
SELECT
    fl.id                                                       AS flight_id,
    d.callsign                                                  AS callsign,
    d.address                                                   AS address,
    d.pilot_name                                                AS pilot_name,
    d.registration                                              AS registration,
    d.competition_id                                            AS competition_id,
    d.model                                                     AS model,
    fl.region                                                   AS region,
    fl.date                                                     AS local_date,
    fl.status                                                   AS status,
    fl.close_reason                                             AS close_reason,
    fl.airborne                                                 AS airborne,
    fl.source                                                   AS source,
    {q.ts_utc("fl.start_time")}                                 AS start_utc,
    {q.ts_utc("fl.end_time")}                                   AS end_utc,
    {q.ts_local("fl.start_time", "fl.utc_offset_s")}            AS start_local,
    {q.ts_local("fl.end_time", "fl.utc_offset_s")}              AS end_local,
    {q.ts_local("fl.takeoff_time", "fl.utc_offset_s")}          AS takeoff_local,
    {q.ts_local("fl.landing_time", "fl.utc_offset_s")}          AS landing_local,
    {q.minutes_between("fl.end_time", "fl.start_time")}         AS duration_min,
    {q.minutes_between("fl.landing_time", "fl.takeoff_time")}   AS airtime_min,
    fl.fix_count                                                AS fix_count,
    fl.takeoff_lat AS takeoff_lat, fl.takeoff_lon AS takeoff_lon, fl.takeoff_alt AS takeoff_alt_m,
    fl.landing_lat AS landing_lat, fl.landing_lon AS landing_lon, fl.landing_alt AS landing_alt_m,
    fl.min_alt AS min_alt_m, fl.max_alt AS max_alt_m, fl.max_agl AS max_agl_m, fl.alt_gain AS alt_gain_m,
    fl.max_climb AS max_climb_ms, fl.max_sink AS max_sink_ms, fl.max_speed AS max_speed_kmh,
    fl.distance_km AS distance_km, fl.straight_km AS straight_km, fl.max_from_start_km AS max_from_start_km,
    fl.ground_filled AS ground_filled
FROM flights fl
JOIN devices d ON d.id = fl.device_id
"""
    return {"fixes_v": fixes_v, "flights_v": flights_v}


def create_views(target: Engine | Connection) -> None:
    """(Re)create the views, so a changed definition always wins."""

    def run(conn: Connection) -> None:
        for name, sql in view_sql(conn.dialect.name).items():
            conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(text(sql))

    if isinstance(target, Engine):
        with target.begin() as conn:
            run(conn)
    else:
        run(target)
