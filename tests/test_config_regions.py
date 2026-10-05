from __future__ import annotations

import re
from pathlib import Path

import pytest

from prack.config import ConfigError, Settings, parse_dotenv
from prack.regions import (
    FALLBACK_BASEMAP,
    Region,
    aprs_filter,
    available_region_files,
    load_region_file,
    load_regions,
    region_for,
)

# ---------------------------------------------------------------- regions


def swiss() -> Region:
    return load_regions(["ch"])[0]


def test_switzerland_filter_is_the_one_in_the_brief():
    assert swiss().aprs_filter(30) == "a/48.120/5.456/45.480/10.944"


def test_filter_without_margin_is_the_plain_box():
    assert swiss().aprs_filter() == "a/47.850/5.850/45.750/10.550"
    assert swiss().expanded_bbox(0) == swiss().bbox


def test_several_regions_are_or_ed_in_one_filter():
    ch = swiss()
    assert aprs_filter([ch, ch], 0) == f"{ch.aprs_filter()} {ch.aprs_filter()}"


def test_the_built_in_swiss_region():
    ch = swiss()
    assert (ch.id, ch.name, ch.timezone) == ("ch", "Switzerland", "Europe/Zurich")
    assert ch.bbox == (5.85, 45.75, 10.55, 47.85)
    assert [m["id"] for m in ch.basemaps] == ["swisstopo-grey", "swisstopo-colour", "swisstopo-aerial"]
    assert ch.default_basemap == "swisstopo-grey"
    assert all("wmts.geo.admin.ch" in m["tiles"][0] and m["attribution"] == "© swisstopo" for m in ch.basemaps)
    assert str(ch.tz) == "Europe/Zurich"
    public = ch.public()
    assert public["center"] == [8.23, 46.8] and public["bbox"] == [5.85, 45.75, 10.55, 47.85]


def test_contains_and_region_for():
    ch = swiss()
    assert ch.contains(46.95, 7.45)  # Bern
    assert not ch.contains(48.14, 11.58)  # Munich
    assert region_for([ch], 46.95, 7.45) is ch
    assert region_for([ch], 48.14, 11.58) is None


def write_region(directory: Path, name: str, text: str) -> Path:
    path = directory / f"{name}.toml"
    path.write_text(text, encoding="utf-8")
    return path


GOOD = """
id = "at"
name = "Austria"
timezone = "Europe/Vienna"
bbox = [9.5, 46.3, 17.2, 49.1]
"""


def test_a_minimal_region_file_gets_sensible_defaults(tmp_path):
    region = load_region_file(write_region(tmp_path, "at", GOOD))
    assert region.center == pytest.approx((13.35, 47.7))
    assert region.zoom == 7.0
    assert [m["id"] for m in region.basemaps] == [FALLBACK_BASEMAP["id"]]
    assert region.default_basemap == "osm"


def test_extra_directory_adds_and_overrides_regions(tmp_path):
    write_region(tmp_path, "at", GOOD)
    write_region(tmp_path, "ch", GOOD.replace('"at"', '"ch"').replace("Austria", "My Switzerland"))
    assert load_regions(["ch", "at"], tmp_path)[0].name == "My Switzerland"
    assert {"ch", "at"} <= set(available_region_files(tmp_path))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (GOOD.replace("[9.5, 46.3, 17.2, 49.1]", "[17.2, 46.3, 9.5, 49.1]"), "west < east"),
        (GOOD.replace("[9.5, 46.3, 17.2, 49.1]", "[9.5, 46.3, 17.2]"), "list of 4 numbers"),
        (GOOD.replace("[9.5, 46.3, 17.2, 49.1]", '["a", 46.3, 17.2, 49.1]'), "list of 4 numbers"),
        (GOOD.replace("Europe/Vienna", "Mars/Olympus"), "unknown time zone"),
        (GOOD.replace('id = "at"', 'id = "Bad Id"'), "id must be"),
        (GOOD + 'default_basemap = "nope"', "default_basemap"),
        (GOOD + "[[basemaps]]\nid = 'x'\nname = 'X'\n", "needs tiles"),
        (GOOD + "[[basemaps]]\nid='x'\nname='X'\ntiles=['t']\n[[basemaps]]\nid='x'\nname='Y'\ntiles=['t']\n", "unique"),
        ("this is = = not toml", "invalid TOML"),
    ],
)
def test_bad_region_files_say_what_is_wrong(tmp_path, text, message):
    with pytest.raises(ConfigError, match=re.escape(message)) as info:
        load_region_file(write_region(tmp_path, "at", text))
    assert "at.toml" in str(info.value)


def test_unknown_region_lists_the_available_ones():
    with pytest.raises(ConfigError, match=r"unknown region 'xx'.*ch"):
        load_regions(["xx"])
    with pytest.raises(ConfigError, match="at least one region"):
        load_regions([])


# ---------------------------------------------------------------- settings


def env(**values: str) -> dict[str, str]:
    return {f"PRACK_{k}": v for k, v in values.items()}


def test_defaults_match_the_brief():
    s = Settings.from_env({}, dotenv=None)
    assert (s.host, s.port, s.regions, s.tracked_types) == ("127.0.0.1", 8000, ["ch"], (7,))
    assert (s.ogn_host, s.ogn_port, s.respect_stealth, s.min_fix_interval) == ("aprs.glidernet.org", 14580, True, 1.0)
    assert s.ogn_filter_margin_km == 30 and s.ddb_url == "https://ddb.glidernet.org/download/?j=1&t=1"
    assert not s.auth_enabled


def test_values_come_from_the_environment():
    s = Settings.from_env(
        env(
            HOST="0.0.0.0",
            PORT="9000",
            REGIONS="ch, at",
            TRACKED_TYPES="6,7,7",
            RESPECT_STEALTH="off",
            MIN_FIX_INTERVAL="2.5",
            OGN_CALLSIGN="MYCALL1",
            AUTH_USER="u",
            AUTH_PASSWORD="p",
            DDB_ENABLED="no",
            DATA_DIR="/var/lib/prack",
            REGIONS_DIR="/etc/prack/regions",
            OGN_FILTER_MARGIN_KM="0",
        ),
        dotenv=None,
    )
    assert (s.host, s.port, s.regions, s.tracked_types) == ("0.0.0.0", 9000, ["ch", "at"], (6, 7))
    assert (s.respect_stealth, s.min_fix_interval, s.ogn_callsign, s.auth_enabled, s.ddb_enabled) == (
        False,
        2.5,
        "MYCALL1",
        True,
        False,
    )
    assert s.db_url == "sqlite:////var/lib/prack/prack.db" and s.regions_dir == Path("/etc/prack/regions")


def test_database_url_wins_over_the_data_dir():
    s = Settings.from_env(env(DATABASE_URL="postgresql+psycopg://u:p@db/prack"), dotenv=None)
    assert s.db_url == "postgresql+psycopg://u:p@db/prack"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "70000"),
        ("PORT", "eighty"),
        ("TRACKED_TYPES", "16"),
        ("TRACKED_TYPES", "x"),
        ("RESPECT_STEALTH", "maybe"),
        ("MIN_FIX_INTERVAL", "0"),
        ("MIN_FIX_INTERVAL", "61"),
        ("OGN_CALLSIGN", "TOOLONGCALL"),
        ("OGN_CALLSIGN", "bad call"),
        ("LOG_LEVEL", "LOUD"),
        ("OGN_FILTER_MARGIN_KM", "-1"),
        ("DDB_URL", "ftp://x"),
        ("OGN_PORT", "-5"),
    ],
)
def test_invalid_values_name_the_variable(name, value):
    with pytest.raises(ConfigError, match=f"PRACK_{name}"):
        Settings.from_env(env(**{name: value}), dotenv=None)


def test_auth_needs_both_user_and_password():
    with pytest.raises(ConfigError, match="set together"):
        Settings.from_env(env(AUTH_USER="u"), dotenv=None)
    with pytest.raises(ConfigError, match="at least one region"):
        Settings.from_env(env(REGIONS=" , "), dotenv=None)


def test_login_callsign_is_valid_for_aprs_is():
    callsigns = {Settings().resolved_callsign() for _ in range(50)}
    assert all(re.fullmatch(r"PRACK\d{4}", c) for c in callsigns) and len(callsigns) > 1
    assert Settings(ogn_callsign="MINE").resolved_callsign() == "MINE"


def test_dotenv_parsing():
    text = "\n".join(
        [
            "# comment",
            "PRACK_PORT=9001",
            'export PRACK_HOST = "0.0.0.0"   ',  # padding after the value is ignored
            "PRACK_REGIONS='ch,at' # trailing comment",
            'PRACK_AUTH_PASSWORD="pa ss#word"',
            "EMPTY=",
            "no equals sign",
        ]
    )
    assert parse_dotenv(text) == {
        "PRACK_PORT": "9001",
        "PRACK_HOST": "0.0.0.0",
        "PRACK_REGIONS": "ch,at",
        "PRACK_AUTH_PASSWORD": "pa ss#word",
        "EMPTY": "",
    }


def test_the_process_environment_wins_over_the_dotenv_file(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("PRACK_PORT=9001\nPRACK_HOST=10.0.0.1\n", encoding="utf-8-sig")
    s = Settings.from_env({"PRACK_PORT": "9002"}, dotenv=dotenv)
    assert (s.port, s.host) == (9002, "10.0.0.1")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_dotenv_files_written_by_windows_tools_are_read(tmp_path, encoding):
    dotenv = tmp_path / ".env"
    dotenv.write_text("PRACK_PORT=9001\r\nPRACK_HOST=10.0.0.1\r\n", encoding=encoding)  # utf-16: what `>` writes
    s = Settings.from_env({}, dotenv=dotenv)
    assert (s.port, s.host) == (9001, "10.0.0.1")


def test_an_unreadable_dotenv_is_a_config_error(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(b"PRACK_HOST=caf\xe9\n")  # latin-1
    with pytest.raises(ConfigError, match="save it as UTF-8"):
        Settings.from_env({}, dotenv=dotenv)
