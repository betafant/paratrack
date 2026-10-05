"""Other sources and edge cases. Sample lines follow the examples in glidernet/ogn-aprs-protocol."""

from __future__ import annotations

import random
from datetime import datetime

import pytest

from prack.ogn.parser import (
    Beacon,
    Skip,
    Skipped,
    Status,
    callsign_address,
    clean_text,
    decode_id,
    decode_time,
    parse_line,
)

from .conftest import REF, utc


def beacon(line: str, ref: datetime = REF) -> Beacon:
    result = parse_line(line, ref)
    assert isinstance(result, Beacon), result
    return result


def skipped(line: str) -> Skip:
    result = parse_line(line, REF)
    assert isinstance(result, Skipped), result
    return result.reason


# ---------------------------------------------------------------- sources with their own id formats


def test_naviter_paraglider_with_flags_in_bits_34_to_37():
    line = "NAV07220E>OGNAVI,qAS,NAVITER:/125447h4557.77N/01220.19E'258/056/A=006562 !W76! id1C4007220E +180fpm +0.0rot"
    b = beacon(line)
    assert (b.source, b.aircraft_type, b.address, b.address_type) == ("Naviter", 7, "07220E", 4)
    assert not (b.stealth or b.no_tracking)


def test_naviter_stealth_and_no_tracking_bits():
    # top byte 0xDC = 1101 1100: bit 39 stealth, bit 38 no-track, bits 37-34 type 7
    line = "NAV07220E>OGNAVI,qAS,NAVITER:/125447h4557.77N/01220.19E'258/056/A=006562 !W76! idDC4007220E +180fpm"
    b = beacon(line)
    assert b.stealth and b.no_tracking and b.aircraft_type == 7


def test_naviter_versioned_tocall():
    line = "NAV042121>OGNAVI-1,qAS,NAVITER:/140648h4550.36N/01314.85E'090/152/A=001086 !W47! id0440042121"
    assert beacon(line).source == "Naviter"


def test_flymaster_with_real_prefix_and_decimal_looking_address():
    line = "FMT924469>OGFLYM,qAS,FLYMASTER:/155232h3720.70N/00557.97W^222/092/A=000029 !W52!"
    b = beacon(line, utc(2026, 7, 15, 15, 53))
    assert (b.address, b.aircraft_type) == ("924469", 7)
    assert b.lat > 0 and b.lon < 0  # northern and western hemisphere


def test_capturs_has_no_id_and_no_type():
    b = beacon("FLRDDEEF1>OGCAPT,qAS,CAPTURS:/064243h4839.64N/00236.78E'000/085/A=000410")
    assert (b.source, b.address, b.aircraft_type) == ("Capturs", "DDEEF1", 0)


def test_capturs_without_altitude_is_skipped():
    assert skipped("FLRDDEEF1>OGCAPT,qAS,CAPTURS:/062744h4845.03N/00230.46E'000/000/") is Skip.NO_ALTITUDE


def test_airmate_plain_address_type_unknown_and_unitless_climb_ignored():
    line = "AIRF00108>OGAIRM,qAS,Airmate:/151551h4326.16N\\00637.42E^245/186/A=002555 !W18! idf00108 +198"
    b = beacon(line, utc(2026, 7, 15, 15, 16))
    assert (b.source, b.address, b.aircraft_type, b.address_type) == ("AirMate", "F00108", 0, 0)
    assert b.climb_ms is None  # "+198" has no unit; we do not guess one


def test_wingman_flags_byte_followed_by_own_id():
    line = "N0ABC7>OGNWMN,qAS,WMN:/134300h4923.60N/01535.54E'000/000/A=001624 id07N0ABC7A39971"
    b = beacon(line, utc(2026, 7, 15, 13, 43))
    assert (b.source, b.address, b.aircraft_type, b.address_type) == ("Wingman", "N0ABC7", 1, 3)


def test_wingman_callsign_starting_with_a_hex_letter_is_still_decoded():
    # "DL1ABC": the third character is a hex digit, which fools a purely structural test
    assert decode_id("1FDL1ABCA39971", "DL1ABC", "OGNWMN") == ("DL1ABC", 3, 7, False, False)
    assert decode_id("1FDL1ABCA39971", "DL1ABC")[2] == 0  # without the tocall there is no way to know


def test_wingman_shaped_id_from_another_source_is_not_decoded_as_wingman():
    assert decode_id("1FX0ABC7A39971", "FLRDD1234", "OGNFNT") == ("DD1234", 0, 0, False, False)


@pytest.mark.parametrize(
    "line",
    [
        "FLRDDE48A>OGLT24,qAS,LT24:/102606h4030.47N/00338.38W'000/018/A=002267 id25387 +000fpm GPS",
        "ICA3E7540>OGSPOT,qAS,SPOT:/161427h1448.35S/04610.86W'000/000/A=008677 id0-2860357 SPOT3 GOOD",
        "FLRDDF944>OGSPID,qAS,SPIDER:/190930h3322.78S/07034.60W'000/000/A=002263 id300234010617040 +19dB LWE 3D",
        "FLRDDDD78>OGSKYL,qAS,SKYLINES:/134403h4225.90N/00144.83E'000/000/A=008438 id2816 +000fpm",
    ],
)
def test_sources_without_aircraft_type_are_unknown(line):
    b = beacon(line, utc(2026, 7, 15, 13, 0))
    assert b.aircraft_type == 0


def test_address_comes_from_the_id_not_the_callsign():
    line = "ICA3ECE59>OGFLR,qAS,GLDRTR:/171254h5144.78N/00616.67E'263/000/A=000075 id093D0930 +000fpm +0.0rot"
    b = beacon(line, utc(2026, 7, 15, 17, 13))
    assert b.address == "3D0930"
    assert b.ident == "ICA3D0930"  # keeps the identity consistent with the address


# ---------------------------------------------------------------- receivers, servers and look-alikes


@pytest.mark.parametrize("tocall", ["OGNSDR", "OGNDVS", "OGNEMO", "OGNSXR"])
def test_ground_station_tocalls(tocall):
    assert skipped(f"LILH>{tocall},TCPIP*,qAC,GLIDERN2:/132201h4457.61NI00900.58E&/A=000423") is Skip.RECEIVER
    assert skipped(f"LILH>{tocall},TCPIP*,qAC,GLIDERN2:>132201h v0.2.7.RPI-GPU CPU:0.7") is Skip.RECEIVER


def test_legacy_receiver_beacons():
    line = "Lachens>APRS,TCPIP*,qAC,GLIDERN2:/165334h4344.70NI00639.19E&/A=005435 v0.2.1 CPU:0.3 RAM:1764.4/2121.4MB"
    assert skipped(line) is Skip.RECEIVER
    assert skipped("Cordoba>APRS,TCPIP*,qAC,GLIDERN3:>194847h v0.2.5.ARM CPU:0.4") is Skip.RECEIVER


def test_weather_station_symbol():
    line = "FNT0828B8>OGNFNT,qAS,Huenenb2:/210414h4710.43N/00826.96E_152/001g002t057r000p000h48b10227 0.0dB"
    assert skipped(line) is Skip.WEATHER


def test_fanet_position_without_altitude_is_skipped():
    line = "FNT1118C1>OGNFNT,qAS,BelaVista:/191919h3841.98N\\00919.39Wn !W68! id3E1118C1 FNT71 26.3dB -12.4kHz"
    assert skipped(line) is Skip.NO_ALTITUDE


def test_aircraft_sources_that_arrive_over_a_server_path_are_not_receivers():
    # SkyBase and VarioVoice paragliders carry an id although their path has TCPIP / qAC.
    skybase = "SKYBASE>OGSKYB,TCPIP*:/130837h4704.16N/01526.90E'123/018/A=001201 id1C97DDFD +120fpm"
    b = beacon(skybase, utc(2026, 7, 15, 13, 9))
    assert (b.source, b.aircraft_type, b.address, b.receiver) == ("SkyBase", 7, "97DDFD", None)
    assert b.ident == "SKY97DDFD"  # every SkyBase pilot has the callsign SKYBASE

    vario = (
        "VVO1A2B3C>OGNVVO,qAC,GLIDERN1:/124213h4549.77N/00918.24Eg310/018/A=003471 id1F1A2B3C +177fpm +1.0rot gps6x9"
    )
    v = beacon(vario, utc(2026, 7, 15, 12, 43))
    assert (v.source, v.aircraft_type, v.address) == ("VarioVoice", 7, "1A2B3C")


def test_position_without_id_from_an_unlisted_source_is_reported_not_swallowed():
    assert skipped("FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/019/A=005300 !W00!") is Skip.NO_ID


def test_status_without_a_name_is_skipped():
    line = "OGN2FD00F>OGNTRK,qAS,LZHL:>092840h h00 v00 11sat/2 165m 1001.9hPa +27.1degC 0% 3.28V 14/-111.5dBm 127/min"
    assert skipped(line) is Skip.STATUS_NO_NAME


def test_pilot_name_is_cleaned_and_limited():
    long_name = "A" * 100
    status = parse_line(f'FNT1103CE>OGNFNT,qAS,Rx:>101520h Name="{long_name}" 45.0dB', REF)
    assert isinstance(status, Status) and len(status.name) == 40
    assert clean_text("Jür\tgen\x00 X\n") == "Jür gen X"
    assert clean_text("   ") is None
    assert skipped('FNT1103CE>OGNFNT,qAS,Rx:>101520h Name="   "') is Skip.STATUS_NO_NAME


# ---------------------------------------------------------------- relays and receivers in the path


@pytest.mark.parametrize(
    ("path", "relayed", "receiver"),
    [
        ("qAS,LZHL", False, "LZHL"),
        ("OGN2FD00F*,qAS,LZHL", True, "LZHL"),
        ("RELAY*,qAS,PGPILOT", True, "PGPILOT"),
        ("LEMD,OGNDELAY*,qAS,DLY2APRS", True, "DLY2APRS"),
        ("qAS,relayed", True, "relayed"),
    ],
)
def test_relayed_packets_are_normal_positions(path, relayed, receiver):
    line = f"FLRDD9C70>OGNTRK,{path}:/094214h4848.77N/01708.33E'000/000/A=000515 !W56! id06DD9C70 -019fpm"
    b = beacon(line, utc(2026, 7, 15, 9, 42))
    assert (b.relayed, b.receiver) == (relayed, receiver)
    assert b.address == "DD9C70"


def test_relayed_token():
    line = (
        "ICAD23456>OGFLR,qAS,K2B9:/172500h4432.07N/07306.44W^000/000/A=000646 !W72! id06D23456 "
        "+039fpm -2.1rot 15.0dB relayed"
    )
    b = beacon(line, utc(2026, 7, 15, 17, 25))
    assert b.relayed and b.turn_dps == pytest.approx(-6.3)


# ---------------------------------------------------------------- flags and units


@pytest.mark.parametrize(
    ("flags", "stealth", "no_tracking", "aircraft_type", "address_type"),
    [
        ("1E", False, False, 7, 2),
        ("9E", True, False, 7, 2),
        ("5E", False, True, 7, 2),
        ("DE", True, True, 7, 2),
        ("06", False, False, 1, 2),
        ("3F", False, False, 15, 3),
    ],
)
def test_flags_byte(flags, stealth, no_tracking, aircraft_type, address_type):
    b = beacon(f"FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/019/A=005300 id{flags}112880")
    assert (b.stealth, b.no_tracking, b.aircraft_type, b.address_type) == (
        stealth,
        no_tracking,
        aircraft_type,
        address_type,
    )


@pytest.mark.parametrize(
    ("value", "callsign", "tocall", "expected"),
    [
        ("1E112880", "FLR112880", "", ("112880", 2, 7, False, False)),
        ("1e112880", "FLR112880", "", ("112880", 2, 7, False, False)),  # upper-cased
        ("0440042121", "NAV042121", "OGNAVI", ("042121", 4, 1, False, False)),
        ("f00108", "AIRF00108", "OGAIRM", ("F00108", 0, 0, False, False)),
        ("25387", "FLRDDE48A", "OGLT24", ("DDE48A", 0, 0, False, False)),
        ("0-2860357", "ICA3E7540", "OGSPOT", ("3E7540", 0, 0, False, False)),
        ("07N0ABC7A39971", "N0ABC7", "", ("N0ABC7", 3, 1, False, False)),
    ],
)
def test_decode_id(value, callsign, tocall, expected):
    assert decode_id(value, callsign, tocall) == expected


def test_callsign_address():
    assert callsign_address("FLR112880") == "112880"
    assert callsign_address("flr1a2b3c") == "1A2B3C"
    assert callsign_address("SKYBASE") == "SKYBASE"
    assert callsign_address("ZK-GSC") == "ZK-GSC"


def test_units_are_converted():
    line = "FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/100/A=010000 id1E112880 +500fpm +2.0rot"
    b = beacon(line)
    assert b.speed_kmh == pytest.approx(185.2)  # knots to km/h
    assert b.alt_m == pytest.approx(3048.0)  # feet to metres
    assert b.climb_ms == pytest.approx(2.54)  # feet per minute to m/s
    assert b.turn_dps == pytest.approx(6.0)  # 1 rot = 3 deg/s


def test_zero_course_and_speed_means_no_data():
    b = beacon("FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'000/000/A=005300 id1E112880")
    assert b.speed_kmh is None and b.track_deg is None


def test_north_and_standing_still_are_real_values():
    north = beacon("FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'000/012/A=005300 id1E112880")
    assert north.track_deg == 0.0 and north.speed_kmh == pytest.approx(12 * 1.852)
    still = beacon("FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/000/A=005300 id1E112880")
    assert still.track_deg == 90.0 and still.speed_kmh == 0.0


def test_position_without_course_and_speed():
    b = beacon("SKYBASE>OGSKYB,TCPIP*:/130807h4704.15N/01526.89E'/A=001188 id1FFE221D", utc(2026, 7, 15, 13, 8))
    assert b.speed_kmh is None and b.track_deg is None and b.aircraft_type == 7


def test_southern_and_western_hemisphere_and_negative_altitude():
    line = "FLR123456>OGFLR,qAS,Rx:/120000h3249.16S/01902.35W'090/020/A=-00050 !W66! id1E123456 +000fpm"
    b = beacon(line, utc(2026, 7, 15, 12, 0, 5))
    assert b.lat == pytest.approx(-(32 + (49.16 + 0.006) / 60))
    assert b.lon == pytest.approx(-(19 + (2.35 + 0.006) / 60))
    assert b.alt_m == pytest.approx(-50 * 0.3048)


def test_hostile_numeric_tokens_never_become_nan():
    line = "FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/019/A=005300 id1E112880 nanfpm infkHz nandB 1e5rot"
    b = beacon(line)
    assert (b.climb_ms, b.freq_khz, b.signal_db, b.turn_dps) == (None, None, None, None)


# ---------------------------------------------------------------- time


def test_midnight_roll_over_both_ways():
    assert decode_time("235950", "h", utc(2026, 7, 16, 0, 0, 30)) == utc(2026, 7, 15, 23, 59, 50)
    assert decode_time("000010", "h", utc(2026, 7, 15, 23, 59, 55)) == utc(2026, 7, 16, 0, 0, 10)
    assert decode_time("123000", "h", utc(2026, 12, 31, 12, 0, 0)) == utc(2026, 12, 31, 12, 30, 0)
    assert decode_time("000010", "h", utc(2026, 12, 31, 23, 59, 55)) == utc(2027, 1, 1, 0, 0, 10)


def test_zulu_time_format_has_day_hour_minute():
    line = "ICAA8CBA8>OGFLR,qAS,MontCAIO:/231150z4512.12N\\01059.03E^192/106/A=009519 !W20! id21A8CBA8 -039fpm"
    b = beacon(line, utc(2026, 7, 23, 11, 52))
    assert b.timestamp == utc(2026, 7, 23, 11, 50)
    assert decode_time("011200", "z", utc(2026, 6, 30, 23, 0)) == utc(2026, 7, 1, 12, 0)  # next month
    assert decode_time("301200", "z", utc(2026, 7, 1, 1, 0)) == utc(2026, 6, 30, 12, 0)  # previous month
    assert decode_time("311200", "z", utc(2026, 6, 15, 0, 0)) == utc(2026, 5, 31, 12, 0)  # June has no 31st
    assert decode_time("321200", "z", REF) is None


def test_positions_without_a_time_stamp_use_the_reception_time():
    line = "FLR112880>OGFLR,qAS,Rx:!4702.30N/00926.07E'090/019/A=005300 id1E112880"
    assert beacon(line, utc(2026, 7, 15, 10, 11, 12)).timestamp == utc(2026, 7, 15, 10, 11, 12)


def test_naive_reference_time_is_taken_as_utc():
    b = beacon(
        "FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/019/A=005300 id1E112880",
        datetime(2026, 7, 15, 18, 31, 0),
    )
    assert b.timestamp == utc(2026, 7, 15, 18, 30, 37)
    assert b.epoch == int(utc(2026, 7, 15, 18, 30, 37).timestamp())


def test_impossible_time_and_position_are_reported():
    assert skipped("FLR112880>OGFLR,qAS,Rx:/256000h4702.30N/00926.07E'090/019/A=005300 id1E112880") is Skip.BAD_TIME
    assert skipped("FLR112880>OGFLR,qAS,Rx:/183037h4762.30N/00926.07E'090/019/A=005300 id1E112880") is Skip.BAD_POSITION
    assert skipped("FLR112880>OGFLR,qAS,Rx:/183037h9102.30N/00926.07E'090/019/A=005300 id1E112880") is Skip.BAD_POSITION
    assert skipped("FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/18126.07E'090/019/A=005300 id1E112880") is Skip.BAD_POSITION


# ---------------------------------------------------------------- identity helper


def test_ident():
    assert beacon("FLR112880>OGFLR,qAS,Rx:/183037h4702.30N/00926.07E'090/019/A=005300 id1E112880").ident == "FLR112880"
    wingman = "N0ABC7>OGNWMN,qAS,WMN:/134300h4923.60N/01535.54E'000/000/A=001624 id07N0ABC7A39971"
    assert beacon(wingman, utc(2026, 7, 15, 13, 43)).ident == "WINN0ABC7"


# ---------------------------------------------------------------- robustness


def test_parse_line_never_raises_on_mangled_input():
    seed = (
        "FLR112880>OGFLR,qAS,Pizol:/183037h4702.30N/00926.07E'090/019/A=005300 !W00! id1E112880 "
        "-380fpm +0.0rot 12.5dB 0e +1.2kHz gps2x3"
    )
    rng = random.Random(20260715)
    alphabet = "0123456789/:>,*!@#_ abcdefNSEW.-+'\\^\x00\xe9"
    for _ in range(3000):
        chars = list(seed)
        for _ in range(rng.randint(1, 6)):
            op = rng.random()
            i = rng.randrange(len(chars) + 1)
            if op < 0.4 and chars:
                del chars[min(i, len(chars) - 1)]
            elif op < 0.8:
                chars.insert(i, rng.choice(alphabet))
            else:
                chars = chars[:i]
        result = parse_line("".join(chars), REF)
        assert isinstance(result, Beacon | Status | Skipped)
