"""The golden lines of the brief (section 11) plus the details that matter downstream."""

from __future__ import annotations

import pytest

from prack.ogn.parser import Beacon, Skip, Skipped, Status, parse_line

from .conftest import REF, utc

FLARM = (
    "FLR112880>OGFLR,qAS,Pizol:/183037h4702.30N/00926.07E'090/019/A=005300 !W00! id1E112880 "
    "-380fpm +0.0rot 12.5dB 0e +1.2kHz gps2x3"
)
FANET = "FNT1103CE>OGNFNT,qAS,FNB1103CE:/183727h5057.94N/00801.00Eg355/002/A=001042 !W10! id1E1103CE +03fpm"
FANET_STATUS = 'FNT1118C1>OGNFNT,qAS,BelaVista:>191924h Name="FlrmAIC" 26.0dB -12.1kHz'
OGN_TRACKER = "OGN3FF19F>OGNTRK,qAS,Rx:/090000h4638.71N/00814.12E'090/019/A=005000 !W00! id1D3FF19F +000fpm"
ADSB = "ICA3FF19F>OGADSB,qAS,Rx:/090000h4638.71N\\00814.12E^090/165/A=005000 !W00! id1D3FF19F +000fpm"
FLYMASTER = "FLM9A1B2C>OGFLYM,qAS,Rx:/090000h4638.71N/00814.12E'090/019/A=005000 !W00!"
NAVITER = "NAV042121>OGNAVI,qAS,NAVITER:/140648h4550.36N/01314.85E'090/152/A=001086 !W47! id0440042121 +000fpm +0.5rot"
RECEIVER = "FNB1103CE>OGNFNT,TCPIP*,qAC,GLIDERN3:/183738h5057.95NI00801.00E&/A=001042"


def beacon(line: str) -> Beacon:
    result = parse_line(line, REF)
    assert isinstance(result, Beacon), result
    return result


def test_flarm():
    b = beacon(FLARM)
    assert (b.callsign, b.tocall, b.source, b.receiver) == ("FLR112880", "OGFLR", "FLARM", "Pizol")
    assert (b.address, b.address_type, b.aircraft_type) == ("112880", 2, 7)
    assert b.timestamp == utc(2026, 7, 15, 18, 30, 37)
    assert b.lat == pytest.approx(47.038333, abs=1e-6)
    assert b.lon == pytest.approx(9.4345, abs=1e-6)
    assert b.alt_m == pytest.approx(1615.4, abs=0.05)
    assert b.speed_kmh == pytest.approx(35.2, abs=0.05)
    assert b.track_deg == 90
    assert b.climb_ms == pytest.approx(-1.93, abs=0.005)
    assert (b.turn_dps, b.signal_db, b.errors, b.freq_khz, b.gps) == (0.0, 12.5, 0, 1.2, "2x3")
    assert not (b.stealth or b.no_tracking or b.relayed)


def test_fanet():
    b = beacon(FANET)
    assert (b.source, b.address, b.aircraft_type) == ("FANET", "1103CE", 7)
    assert b.lat == pytest.approx(50.965683, abs=1e-6)  # the !W10! digit adds 0.001 arc minute
    assert b.lon == pytest.approx(8.016667, abs=1e-6)
    assert b.alt_m == pytest.approx(317.6, abs=0.05)
    assert b.speed_kmh == pytest.approx(3.7, abs=0.05)
    assert b.track_deg == 355
    assert b.climb_ms == pytest.approx(0.015, abs=0.001)
    assert b.turn_dps is None and b.signal_db is None


def test_fanet_status_carries_the_pilot_name():
    status = parse_line(FANET_STATUS, REF)
    assert isinstance(status, Status)
    assert (status.name, status.address, status.source) == ("FlrmAIC", "1118C1", "FANET")
    assert status.timestamp == utc(2026, 7, 15, 19, 19, 24)


def test_ogn_tracker():
    b = beacon(OGN_TRACKER)
    assert (b.source, b.aircraft_type, b.address) == ("OGN tracker", 7, "3FF19F")


def test_adsb_paraglider_category_is_forced_to_unknown():
    b = beacon(ADSB)
    assert b.source == "ADS-B"
    assert b.aircraft_type == 0
    assert b.reported_type == 7  # what the id said, kept for diagnostics
    assert b.speed_kmh == pytest.approx(165 * 1.852)


def test_flymaster_has_no_id_and_counts_as_paraglider():
    b = beacon(FLYMASTER)
    assert (b.source, b.aircraft_type, b.address) == ("Flymaster", 7, "9A1B2C")
    assert b.reported_type == 0  # nothing was transmitted


def test_naviter_ten_digit_id():
    b = beacon(NAVITER)
    assert (b.source, b.address, b.aircraft_type) == ("Naviter", "042121", 1)  # a glider: ignored by default
    assert b.address_type == 4
    assert b.turn_dps == pytest.approx(1.5)


def test_receiver_beacon_is_skipped():
    result = parse_line(RECEIVER, REF)
    assert isinstance(result, Skipped)
    assert result.reason is Skip.RECEIVER


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("", Skip.COMMENT),
        ("   \r\n", Skip.COMMENT),
        ("# aprsc 2.1.19-g730c5c0 05 Oct 2026 04:20:00 GMT GLIDERN2 51.15.1.1:14580", Skip.COMMENT),
        ("# logresp PRACK1234 unverified, server GLIDERN2", Skip.COMMENT),
        ("garbage", Skip.MALFORMED),
        ("FLR112880>OGFLR,qAS,Rx:not a position", Skip.MALFORMED),
        ("FLR112880>OGFLR,qAS,Rx:/183037h4702.30N", Skip.MALFORMED),
        ("TOOLONGCALLSIGN1>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/019/A=005300", Skip.MALFORMED),
    ],
)
def test_comments_and_garbage_never_raise(line, reason):
    result = parse_line(line, REF)
    assert isinstance(result, Skipped) and result.reason is reason
