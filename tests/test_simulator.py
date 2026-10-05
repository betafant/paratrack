from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from prack.ogn.parser import Beacon, Status, parse_line
from prack.ogn.simulator import PilotSpec, Simulator, build_flight

START = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
LAUNCH = (46.6453, 7.6511)
SHORT = {"flight_s": 120, "drop_m": 300.0, "stand_s": 300}


def sloping(lat: float, lon: float) -> float:
    return 1000.0 + (lon - 7.65) * 75_900 * 0.3 + (lat - 46.64) * 111_195 * 0.2  # a 30% and 20% slope


def test_a_flight_has_the_phases_of_a_flight():
    s = build_flight(PilotSpec("D00001", **SHORT), LAUNCH, seed=1)
    assert all(x.speed == 0 for x in s[:60]) and s[75].speed > 20  # standing, then the launch run
    assert max(x.speed for x in s) < 42 and all(x.speed == 0 for x in s[-300:])  # stand_s in the landing field
    circling = [x for x in s if abs(x.turn) > 5]
    assert circling and all(x.climb > 1.0 for x in circling[:50])  # thermals climb, and turn at 8-14 deg/s
    assert max(abs(x.turn) for x in s) < 16 and len(s) > 60 + 12 + 120 + 300
    assert s[0].alt > s[-1].alt + 250  # lost the 300 m


def test_flights_are_reproducible_and_differ_between_pilots():
    spec = PilotSpec("D00001", **SHORT)
    assert build_flight(spec, LAUNCH, 5) == build_flight(spec, LAUNCH, 5)
    assert build_flight(spec, LAUNCH, 5) != build_flight(spec, LAUNCH, 6)


def test_on_terrain_the_flight_lies_on_the_ground_it_starts_and_ends_on():
    s = build_flight(PilotSpec("D00001", **SHORT), LAUNCH, 2, ground=sloping)
    agl = [x.alt - sloping(x.lat, x.lon) for x in s]
    assert min(agl) > -1.5  # never under the ground (the jitter is 0.3 m)
    assert all(a < 6 for a in agl[:60]) and agl[-1] < 12  # on the ground at launch and at landing
    assert max(agl) > 100 and all(
        a >= 19 for a, x in zip(agl, s, strict=True) if x.speed > 30 and a > 25
    )  # never grazing it
    steps = [abs(b.alt - a.alt) for a, b in zip(s, s[1:], strict=False)]
    assert max(steps) < 12  # the slope does not make the aircraft jump (terrain may change 5 m/s)


def test_without_a_usable_ground_height_altitudes_are_invented():
    s = build_flight(PilotSpec("D00001", **SHORT), LAUNCH, 2, ground=lambda lat, lon: None)
    assert 1600 < s[0].alt < 2400 and s[0].alt > s[-1].alt + 250


def test_lines_of_a_dual_protocol_pilot_parse_and_share_an_address():
    sim = Simulator(START, [PilotSpec("D00001", ("flarm", "fanet"), name="Mia", launch=LAUNCH, **SHORT)], seed=1)
    parsed = [parse_line(line, START + timedelta(seconds=k)) for k in range(120) for line in sim.lines(sim.at(k))]
    beacons = [p for p in parsed if isinstance(p, Beacon)]
    assert {b.address for b in beacons} == {"D00001"} and {b.source for b in beacons} == {"FLARM", "FANET"}
    assert {b.aircraft_type for b in beacons} == {7}
    flarm, fanet = [b for b in beacons if b.source == "FLARM"], [b for b in beacons if b.source == "FANET"]
    assert len(flarm) == 120 and 29 <= len(fanet) <= 31  # every second, and every fourth
    assert flarm[0].turn_dps is not None and fanet[0].turn_dps is None and fanet[0].signal_db is None
    names = [p for p in parsed if isinstance(p, Status)]
    assert [n.name for n in names] == ["Mia", "Mia"] and names[0].address == "D00001"


def test_pilots_start_when_they_are_due_and_stop_when_they_have_landed():
    sim = Simulator(START, [PilotSpec("D00001", delay_s=100, **SHORT)], seed=1)
    assert sim.lines(sim.at(99)) == [] and len(sim.lines(sim.at(100))) == 1
    assert len(sim.lines(sim.at(sim.duration_s() - 1))) == 1 and sim.lines(sim.at(sim.duration_s())) == []


def test_the_demo_mix_contains_noise_that_the_filters_must_reject():
    sim = Simulator.demo(START, pilots=3, seed=1)
    lines = [line for k in range(200) for line in sim.lines(sim.at(k))]
    kinds = {
        (b.source, b.aircraft_type, b.stealth) for b in (parse_line(x, START) for x in lines) if isinstance(b, Beacon)
    }
    assert (
        ("FLARM", 1, False) in kinds and ("FLARM", 6, False) in kinds and ("FLARM", 7, True) in kinds
    )  # glider, hang glider, stealth
    adsb = [b for b in (parse_line(x, START) for x in lines) if isinstance(b, Beacon) and b.source == "ADS-B"]
    assert adsb and all(b.aircraft_type == 0 and b.reported_type == 7 and b.speed_kmh > 250 for b in adsb)
    quiet = Simulator.demo(START, pilots=3, seed=1, noise=False)
    assert len(quiet.pilots) == 3


def test_protocol_mix_and_address_range_can_be_chosen():
    sim = Simulator.demo(START, pilots=4, protocols=[("ogn",)], noise=False, address_base=0xE00000, stagger=60)
    assert [p.spec.address for p in sim.pilots] == ["E00000", "E00011", "E00022", "E00033"]
    assert {p.spec.protocols for p in sim.pilots} == {("ogn",)} and [p.spec.delay_s for p in sim.pilots] == [
        0,
        60,
        120,
        180,
    ]


@pytest.mark.parametrize("seed", range(3))
def test_the_simulator_never_produces_a_line_the_parser_cannot_read(seed):
    sim = Simulator.demo(START, pilots=6, seed=seed)
    for k in range(0, sim.duration_s(), 7):
        for line in sim.lines(sim.at(k)):
            assert isinstance(parse_line(line, sim.at(k)), Beacon | Status), line
