"""The synthetic line builder must produce what the parser reads back."""

from __future__ import annotations

import random

import pytest

from prack.ogn.builder import build_position, build_status, flags_byte
from prack.ogn.parser import Beacon, Skip, Skipped, Status, parse_line

from .conftest import utc

NOON = utc(2026, 7, 15, 12, 0, 0)


@pytest.mark.parametrize("seed", range(5))
def test_round_trip_within_the_resolution_of_the_format(seed):
    rng = random.Random(seed)
    for _ in range(200):
        lat, lon = rng.uniform(-80, 80), rng.uniform(-179, 179)
        alt = rng.uniform(-100, 6000)
        speed, course = rng.uniform(1, 90), rng.randrange(1, 360)
        climb = rng.uniform(-6, 6)
        line = build_position(NOON, lat, lon, alt, speed_kmh=speed, course=course, climb_ms=climb, turn_dps=3.0)
        b = parse_line(line, NOON)
        assert isinstance(b, Beacon), line
        assert b.lat == pytest.approx(lat, abs=1e-5)  # 1/1000 arc minute is 1.7e-5 degrees
        assert b.lon == pytest.approx(lon, abs=1e-5)
        assert b.alt_m == pytest.approx(alt, abs=0.2)  # whole feet
        assert b.speed_kmh == pytest.approx(speed, abs=0.95)  # whole knots
        assert b.track_deg == course
        assert b.climb_ms == pytest.approx(climb, abs=0.003)
        assert b.turn_dps == pytest.approx(3.0, abs=0.15)


def test_rounding_carries_into_the_next_arc_minute():
    line = build_position(NOON, 46.999999, 7.999999, 1000)  # 59.99994 arc minutes
    b = parse_line(line, NOON)
    assert isinstance(b, Beacon) and (b.lat, b.lon) == pytest.approx((47.0, 8.0), abs=1e-5)


def test_flags_and_identity_fields():
    line = build_position(
        NOON,
        46.5,
        8.0,
        2000,
        address="ABCDEF",
        prefix="FNT",
        tocall="OGNFNT",
        aircraft_type=7,
        address_type=3,
        stealth=True,
        no_tracking=True,
    )
    b = parse_line(line, NOON)
    assert isinstance(b, Beacon)
    assert (b.callsign, b.address, b.aircraft_type, b.address_type, b.stealth, b.no_tracking) == (
        "FNTABCDEF",
        "ABCDEF",
        7,
        3,
        True,
        True,
    )
    assert (
        flags_byte(7, 2) == 0x1E
        and flags_byte(7, 2, stealth=True) == 0x9E
        and flags_byte(1, 2, no_tracking=True) == 0x46
    )


def test_missing_fields_are_left_out_like_real_senders_do():
    line = build_position(
        NOON, 46.5, 8.0, 2000, speed_kmh=None, climb_ms=None, signal_db=None, errors=None, freq_khz=None, gps=None
    )
    b = parse_line(line, NOON)
    assert isinstance(b, Beacon)
    assert (b.speed_kmh, b.track_deg, b.climb_ms, b.signal_db, b.errors, b.freq_khz, b.gps) == (None,) * 7


def test_negative_altitude_and_southern_western_hemisphere():
    b = parse_line(build_position(NOON, -33.9, -70.6, -30.0), NOON)
    assert isinstance(b, Beacon)
    assert (b.lat, b.lon, b.alt_m) == pytest.approx((-33.9, -70.6, -30.0), abs=0.2)


def test_without_id_only_listed_sources_are_accepted():
    flarm = build_position(NOON, 46.5, 8.0, 2000, include_id=False)
    assert parse_line(flarm, NOON) == Skipped(Skip.NO_ID, "FLARM")
    flymaster = build_position(NOON, 46.5, 8.0, 2000, include_id=False, prefix="FMT", tocall="OGFLYM")
    b = parse_line(flymaster, NOON)
    assert isinstance(b, Beacon) and b.aircraft_type == 7


def test_relay_path_and_symbols():
    line = build_position(NOON, 46.5, 8.0, 2000, path="OGN2FD00F*,qAS,LZHL", symbol_table="\\", symbol="^")
    b = parse_line(line, NOON)
    assert isinstance(b, Beacon) and b.relayed and b.receiver == "LZHL"


def test_status_line():
    s = parse_line(build_status(NOON, "Mia", address="112880"), NOON)
    assert isinstance(s, Status) and (s.name, s.address, s.timestamp) == ("Mia", "112880", NOON)
