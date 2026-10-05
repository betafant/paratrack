"""OGN code tables: aircraft types, APRS destination calls ("tocalls") and data sources.

The tocall list follows https://github.com/glidernet/ogn-aprs-protocol/blob/master/tocalls.txt
"""

from __future__ import annotations

PARAGLIDER = 7

# Aircraft type, bits 5..2 of the OGN "id" flags byte (STttttaa).
AIRCRAFT_TYPES: dict[int, str] = {
    0: "unknown",
    1: "glider",
    2: "tow plane",
    3: "helicopter",
    4: "skydiver",
    5: "drop plane",
    6: "hang glider",
    7: "paraglider",
    8: "powered aircraft",
    9: "jet",
    10: "UFO",
    11: "balloon",
    12: "airship",
    13: "drone",
    14: "ground support",
    15: "static object",
}

# APRS destination call -> data source (the "-N" version suffix is stripped first).
SOURCES: dict[str, str] = {
    "APRS": "OGN (legacy)",
    "OGFLR": "FLARM",
    "OGNFLR": "FLARM",
    "OGFLR6": "FLARM",
    "OGFLR7": "FLARM",
    "OGNFNT": "FANET",
    "OGNTRK": "OGN tracker",
    "OGADSL": "OGN tracker (ADS-L)",
    "OGADSB": "ADS-B",
    "OGNADSB": "ADS-B",
    "OGNAVI": "Naviter",
    "OGFLYM": "Flymaster",
    "OGNSKY": "SafeSky",
    "OGNPUR": "PureTrack",
    "OGCAPT": "Capturs",
    "OGAIRM": "AirMate",
    "OGNWMN": "Wingman",
    "OGLT24": "LiveTrack24",
    "OGSPOT": "SPOT",
    "OGSPID": "Spider",
    "OGSKYL": "SkyLines",
    "OGNINRE": "inReach",
    "OGNMYC": "MyCloudbase",
    "OGEVARIO": "eVario",
    "OGNDELAY": "OGN delayed",
    "OGNPAW": "PilotAware",
    "OGPAW": "PilotAware",
    "OGNMTK": "MicroTrak",
    "OGNMKT": "MicroTrak",
    "OGNDSX": "DSX",
    "OGNMAV": "MAVLink",
    "OGNTTN": "TTN",
    "OGNHEL": "Helium",
    "OGAVZ": "Aviaze",
    "OGSTUX": "Stratux",
    "OGAPIK": "APIK",
    "OGMSHT": "Meshtastic",
    "OGBSTOP": "BirdStop",
    "OGNVOL": "Volandoo",
    "OGNWGL": "WeGlide",
    "OGSKYB": "SkyBase",
    "OGMLAT": "OGN MLAT",
    "OGPGP": "pgpilot",
    "OGNALP": "Alpium",
    "OGNVVO": "VarioVoice",
    "OGNFNO": "Flying Neurons",
    "FXCAPP": "flyXC",
}

# Destination calls used by ground stations and weather stations, never by aircraft.
RECEIVER_TOCALLS = frozenset({"OGNSDR", "OGNDVS", "OGNEMO", "OGNSXR"})

# Sources whose position messages carry no "id" field at all.
NO_ID_SOURCES = frozenset({"OGFLYM", "OGCAPT"})

# Aircraft type assumed for sources that do not transmit one. Flymaster builds paragliding
# instruments, so its users are paragliders.
SOURCE_DEFAULT_TYPES: dict[str, int] = {"OGFLYM": PARAGLIDER}

# The ADS-B emitter category "ultralight / hang glider / paraglider" arrives as OGN type 7.
# Real paragliders carry no ADS-B transponder, microlights do: such targets are "unknown".
ADSB_TOCALLS = frozenset({"OGADSB", "OGNADSB"})

# Sources that carry no aircraft type at all (type 0 by design); only used for diagnostics.
UNTYPED_SOURCES = frozenset({"LiveTrack24", "SPOT", "Spider", "SkyLines", "inReach", "Capturs", "AirMate"})

# Protocol of a stored fix (``fixes.src``). 0-3 are the four that take part in de-duplication.
SOURCE_CODES: dict[str, int] = {
    "FLARM": 0,
    "FANET": 1,
    "OGN tracker": 2,
    "OGN tracker (ADS-L)": 3,
    "Flymaster": 4,
    "Naviter": 5,
    "SafeSky": 6,
    "PureTrack": 7,
    "SkyBase": 8,
    "Wingman": 9,
    "VarioVoice": 10,
    "pgpilot": 11,
    "Alpium": 12,
    "flyXC": 13,
    "WeGlide": 14,
    "eVario": 15,
    "AirMate": 16,
    "Capturs": 17,
}
OTHER_SOURCE_CODE = 99

# One device heard over several protocols is stored once, under the best of them. FLARM first: it sends
# about one position per second with climb and turn rate. Lower number = preferred.
SOURCE_PRIORITY: dict[str, int] = {"FLARM": 0, "FANET": 1, "OGN tracker": 2, "OGN tracker (ADS-L)": 3}


def source_priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 9)


def source_code(source: str) -> int:
    return SOURCE_CODES.get(source, OTHER_SOURCE_CODE)


NAVITER_TOCALL = "OGNAVI"
WINGMAN_TOCALL = "OGNWMN"


def normalize_tocall(tocall: str) -> str:
    """Strip an APRS version suffix and upper-case: ``OGNAVI-1`` -> ``OGNAVI``."""
    return tocall.split("-", 1)[0].upper()


def source_for(tocall: str) -> str:
    """Human readable data source for a destination call; unknown calls are returned as they are."""
    tocall = normalize_tocall(tocall)
    if tocall in SOURCES:
        return SOURCES[tocall]
    if tocall.startswith("OGFLR"):
        return "FLARM"
    return tocall


def type_name(aircraft_type: int) -> str:
    return AIRCRAFT_TYPES.get(aircraft_type, f"type {aircraft_type}")
