"""Type and privacy filters, the classifier that counts drops, and the survey behind ``diagnose``."""

from __future__ import annotations

import pytest

from prack.config import Settings
from prack.ogn.builder import build_position, build_status
from prack.ogn.ddb import DdbInfo
from prack.ogn.filters import DropReason, FilterPolicy, static_drop_reason
from prack.ogn.parser import Beacon, parse_line
from prack.ogn.pipeline import LineClassifier
from prack.ogn.survey import KEPT_LIMIT, SAMPLES_PER_REASON, Survey
from prack.stats import Counters

from .conftest import utc

NOW = utc(2026, 7, 15, 12, 0, 0)
POLICY = FilterPolicy()


def make_beacon(**kwargs) -> Beacon:
    b = parse_line(build_position(NOW, 46.6, 8.2, 2200, **kwargs), NOW)
    assert isinstance(b, Beacon)
    return b


class FakeDdb:
    def __init__(self, **entries: DdbInfo) -> None:
        self.entries = {k.upper(): v for k, v in entries.items()}

    def lookup(self, address: str) -> DdbInfo | None:
        return self.entries.get(address.upper())


def ddb_entry(address: str, *, tracked: bool = True, identified: bool = True) -> DdbInfo:
    return DdbInfo(address, "D-1234", "XY", "Hook 5", tracked, identified)


# ---------------------------------------------------------------- static filters


def test_a_paraglider_passes():
    assert static_drop_reason(make_beacon(), POLICY) is None


@pytest.mark.parametrize("aircraft_type", [0, 1, 2, 3, 6, 8, 9, 15])
def test_only_paragliders_by_default(aircraft_type):
    assert static_drop_reason(make_beacon(aircraft_type=aircraft_type), POLICY) is DropReason.TYPE


def test_tracked_types_are_configurable():
    policy = FilterPolicy(frozenset({1, 6, 7}))
    assert static_drop_reason(make_beacon(aircraft_type=6), policy) is None
    assert static_drop_reason(make_beacon(aircraft_type=8), policy) is DropReason.TYPE


def test_no_tracking_flag_always_wins():
    assert static_drop_reason(make_beacon(no_tracking=True), POLICY) is DropReason.NO_TRACKING
    assert (
        static_drop_reason(make_beacon(no_tracking=True), FilterPolicy(respect_stealth=False)) is DropReason.NO_TRACKING
    )


def test_stealth_flag_is_respected_by_default_and_can_be_switched_off():
    assert static_drop_reason(make_beacon(stealth=True), POLICY) is DropReason.STEALTH
    assert static_drop_reason(make_beacon(stealth=True), FilterPolicy(respect_stealth=False)) is None


def test_type_is_checked_before_privacy():
    assert static_drop_reason(make_beacon(aircraft_type=1, no_tracking=True), POLICY) is DropReason.TYPE


def test_device_database_opt_out():
    ddb = FakeDdb(a1b2c3=ddb_entry("A1B2C3", tracked=False), d4e5f6=ddb_entry("D4E5F6", identified=False))
    assert static_drop_reason(make_beacon(address="A1B2C3"), POLICY, ddb) is DropReason.DDB_UNTRACKED
    assert static_drop_reason(make_beacon(address="D4E5F6"), POLICY, ddb) is None  # unidentified is still tracked
    assert static_drop_reason(make_beacon(address="000001"), POLICY, ddb) is None  # not in the database
    assert static_drop_reason(make_beacon(address="A1B2C3"), POLICY, None) is None


def test_adsb_paraglider_is_dropped_as_wrong_type():
    line = build_position(NOW, 46.6, 8.2, 2200, prefix="ICA", tocall="OGADSB", speed_kmh=300, address_type=1)
    b = parse_line(line, NOW)
    assert isinstance(b, Beacon) and static_drop_reason(b, POLICY) is DropReason.TYPE


def test_policy_from_settings():
    s = Settings(tracked_types=(6, 7), respect_stealth=False)
    assert FilterPolicy.from_settings(s) == FilterPolicy(frozenset({6, 7}), False)


# ---------------------------------------------------------------- classifier


def classifier(ddb=None) -> tuple[LineClassifier, Counters, Survey]:
    counters, survey = Counters(), Survey()
    return LineClassifier(POLICY, ddb, counters, survey), counters, survey


def test_classifier_counts_every_outcome():
    c, counters, _ = classifier()
    lines = [
        "# aprsc 2.1.19",
        build_position(NOW, 46.6, 8.2, 2200),
        build_position(NOW, 46.6, 8.2, 2200, address="AAAAAA", aircraft_type=1),
        build_position(NOW, 46.6, 8.2, 2200, address="BBBBBB", no_tracking=True),
        build_position(NOW, 46.6, 8.2, 2200, address="CCCCCC", stealth=True),
        build_status(NOW, "Mia"),
        "LILH>OGNSDR,TCPIP*,qAC,GLIDERN2:/132201h4457.61NI00900.58E&/A=000423",
        "complete garbage",
    ]
    kinds = [c.classify(line, NOW).kind for line in lines]
    assert kinds == ["skipped", "beacon", "dropped", "dropped", "dropped", "status", "skipped", "skipped"]
    assert counters.snapshot() == {
        "accepted": 1,
        "drop.no_tracking": 1,
        "drop.stealth": 1,
        "drop.type": 1,
        "lines": 8,
        "skip.comment": 1,
        "skip.malformed": 1,
        "skip.receiver": 1,
        "status": 1,
    }


def test_classifier_result_carries_the_beacon_and_the_reason():
    c, _, _ = classifier()
    ok = c.classify(build_position(NOW, 46.6, 8.2, 2200), NOW)
    assert ok.kind == "beacon" and ok.beacon is not None and ok.reason is None
    dropped = c.classify(build_position(NOW, 46.6, 8.2, 2200, aircraft_type=8), NOW)
    assert dropped.kind == "dropped" and dropped.reason == "type" and dropped.beacon is not None
    status = c.classify(build_status(NOW, "Mia"), NOW)
    assert status.kind == "status" and status.status is not None and status.status.name == "Mia"


def test_classifier_applies_the_device_database():
    c, counters, _ = classifier(FakeDdb(aaaaaa=ddb_entry("AAAAAA", tracked=False)))
    assert c.classify(build_position(NOW, 46.6, 8.2, 2200, address="AAAAAA"), NOW).reason == "ddb_untracked"
    assert counters.get("drop.ddb_untracked") == 1


# ---------------------------------------------------------------- survey


def test_survey_groups_by_source_and_type_with_outcomes():
    c, _, survey = classifier()
    for i in range(3):
        c.classify(build_position(NOW, 46.6, 8.2, 2200, address=f"00000{i}"), NOW)
    c.classify(build_position(NOW, 46.6, 8.2, 2200, address="000009", stealth=True), NOW)
    c.classify(build_position(NOW, 46.6, 8.2, 2200, address="000010", aircraft_type=1), NOW)
    adsb = build_position(NOW, 46.6, 8.2, 2200, prefix="ICA", tocall="OGADSB", address="3FF19F", address_type=1)
    c.classify(adsb, NOW)
    groups = {(g["source"], g["aircraft_type"], g["reported_type"]): g for g in survey.snapshot()["groups"]}
    flarm_pg = groups[("FLARM", 7, 7)]
    assert (flarm_pg["positions"], flarm_pg["devices"], flarm_pg["outcomes"]) == (4, 4, {"ok": 3, "stealth": 1})
    assert groups[("FLARM", 1, 1)]["outcomes"] == {"type": 1}
    assert groups[("ADS-B", 0, 7)]["outcomes"] == {"type": 1}  # reported 7, effective 0
    assert survey.snapshot()["groups"][0]["source"] == "FLARM"  # most frequent first


def test_survey_lists_devices_that_pass_with_their_last_position():
    c, _, survey = classifier()
    c.classify(build_position(NOW, 46.6, 8.2, 2200, address="000001"), NOW)
    c.classify(build_position(utc(2026, 7, 15, 12, 0, 5), 46.7, 8.3, 2300, address="000001"), NOW)
    c.classify(build_position(NOW, 46.6, 8.2, 2200, address="000002", no_tracking=True), NOW)
    (device,) = survey.kept_devices()
    assert (device.ident, device.alt_m) == ("FLR000001", pytest.approx(2300, abs=0.2))
    assert device.lat == pytest.approx(46.7, abs=1e-5)


def test_survey_counts_skips_by_source_and_samples_undecodable_lines():
    c, _, survey = classifier()
    for i in range(SAMPLES_PER_REASON + 3):
        c.classify(f"FLR00000{i}>OGFLR,qAS,Rx:/120000h4600.00N/00800.00E'090/019/A=005300", NOW)  # no id
    c.classify("LILH>OGNSDR,TCPIP*,qAC,GLIDERN2:>132201h v0.2.7", NOW)
    c.classify("# comment", NOW)
    snap = survey.snapshot()
    assert {"source": "FLARM", "reason": "no_id", "count": SAMPLES_PER_REASON + 3} in snap["skips"]
    assert {"source": "?", "reason": "comment", "count": 1} not in snap["skips"]  # comments are not worth a row
    assert len(snap["samples"]["no_id"]) == SAMPLES_PER_REASON
    assert "receiver" not in snap["samples"]


def test_survey_memory_is_bounded():
    survey = Survey()
    for i in range(KEPT_LIMIT + 50):
        survey.record(make_beacon(address=f"{i:06X}"), "ok")
    assert len(survey.kept_devices()) == KEPT_LIMIT
    assert survey.snapshot()["groups"][0]["positions"] == KEPT_LIMIT + 50


def test_counters_are_thread_safe_and_prefix_filtered():
    import threading

    counters = Counters()

    def work() -> None:
        for _ in range(5000):
            counters.inc("drop.type")

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    counters.inc("lines")
    assert counters.get("drop.type") == 20000
    assert counters.snapshot("drop.") == {"drop.type": 20000}
