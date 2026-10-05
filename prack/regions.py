"""Regions: where to listen, where flights may start, which time zone and base maps the UI uses.

A region is one small TOML file, see ``prack/regions/ch.toml``. Adding a country means adding a file
(next to the built-in ones, or in ``PRACK_REGIONS_DIR``) and listing its id in ``PRACK_REGIONS``.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import ConfigError

BUILTIN_DIR = Path(__file__).parent / "regions"
EARTH_KM_PER_DEGREE = 6371.0 * math.pi / 180.0  # 111.195 km

# Used when a region file lists no base maps of its own.
FALLBACK_BASEMAP = {
    "id": "osm",
    "name": "OpenStreetMap",
    "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    "tile_size": 256,
    "max_zoom": 19,
    "attribution": "© OpenStreetMap contributors",
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    timezone: str
    bbox: tuple[float, float, float, float]  # west, south, east, north (WGS84)
    center: tuple[float, float]  # lon, lat
    zoom: float
    default_basemap: str
    basemaps: tuple[dict, ...]
    tz: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tz", ZoneInfo(self.timezone))

    def contains(self, lat: float, lon: float) -> bool:
        west, south, east, north = self.bbox
        return south <= lat <= north and west <= lon <= east

    def expanded_bbox(self, margin_km: float) -> tuple[float, float, float, float]:
        """The bounding box grown by ``margin_km`` on every side (longitude scaled at the mid latitude)."""
        west, south, east, north = self.bbox
        if margin_km <= 0:
            return self.bbox
        dlat = margin_km / EARTH_KM_PER_DEGREE
        dlon = margin_km / (EARTH_KM_PER_DEGREE * max(0.1, math.cos(math.radians((south + north) / 2))))
        return (west - dlon, south - dlat, east + dlon, north + dlat)

    def aprs_filter(self, margin_km: float = 0.0) -> str:
        """APRS-IS area filter ``a/latN/lonW/latS/lonE`` for the box plus ``margin_km``."""
        west, south, east, north = self.expanded_bbox(margin_km)
        return f"a/{north:.3f}/{west:.3f}/{south:.3f}/{east:.3f}"

    def public(self) -> dict:
        """What the browser needs (served by ``/api/config``)."""
        return {
            "id": self.id,
            "name": self.name,
            "timezone": self.timezone,
            "bbox": list(self.bbox),
            "center": list(self.center),
            "zoom": self.zoom,
            "default_basemap": self.default_basemap,
            "basemaps": list(self.basemaps),
        }


def aprs_filter(regions: list[Region], margin_km: float) -> str:
    """One area filter per region, space separated (APRS-IS ORs them)."""
    return " ".join(r.aprs_filter(margin_km) for r in regions)


def _number(data: dict, key: str, path: Path) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{path.name}: '{key}' must be a number")
    return float(value)


def _pair(data: dict, key: str, path: Path, size: int) -> tuple[float, ...]:
    value = data.get(key)
    if not isinstance(value, list) or len(value) != size:
        raise ConfigError(f"{path.name}: '{key}' must be a list of {size} numbers")
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        raise ConfigError(f"{path.name}: '{key}' must be a list of {size} numbers") from None


def _basemaps(data: dict, path: Path) -> tuple[dict, ...]:
    raw = data.get("basemaps") or [FALLBACK_BASEMAP]
    maps = []
    for entry in raw:
        missing = [k for k in ("id", "name", "tiles") if not entry.get(k)]
        if missing:
            raise ConfigError(f"{path.name}: basemap needs {', '.join(missing)}")
        maps.append(
            {
                "id": str(entry["id"]),
                "name": str(entry["name"]),
                "tiles": [str(t) for t in entry["tiles"]],
                "tile_size": int(entry.get("tile_size", 256)),
                "max_zoom": int(entry.get("max_zoom", 18)),
                "attribution": str(entry.get("attribution", "")),
            }
        )
    if len({m["id"] for m in maps}) != len(maps):
        raise ConfigError(f"{path.name}: basemap ids must be unique")
    return tuple(maps)


def load_region_file(path: Path) -> Region:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: invalid TOML: {exc}") from None
    region_id = str(data.get("id", path.stem))
    if not _ID_RE.match(region_id):
        raise ConfigError(f"{path.name}: id must be lower case letters, digits, '-' or '_'")
    west, south, east, north = _pair(data, "bbox", path, 4)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ConfigError(f"{path.name}: bbox must be [west, south, east, north] with west < east, south < north")
    center = _pair(data, "center", path, 2) if "center" in data else ((west + east) / 2, (south + north) / 2)
    timezone = str(data.get("timezone", "UTC"))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"{path.name}: unknown time zone '{timezone}' (on Windows the 'tzdata' package is required)"
        ) from None
    basemaps = _basemaps(data, path)
    default_basemap = str(data.get("default_basemap", basemaps[0]["id"]))
    if default_basemap not in {m["id"] for m in basemaps}:
        raise ConfigError(f"{path.name}: default_basemap '{default_basemap}' is not defined")
    return Region(
        id=region_id,
        name=str(data.get("name", region_id)),
        timezone=timezone,
        bbox=(west, south, east, north),
        center=(center[0], center[1]),
        zoom=_number(data, "zoom", path) if "zoom" in data else 7.0,
        default_basemap=default_basemap,
        basemaps=basemaps,
    )


def available_region_files(extra_dir: Path | None = None) -> dict[str, Path]:
    """Region files by id (file name without ``.toml``); files in ``extra_dir`` override built-in ones."""
    files: dict[str, Path] = {}
    for directory in (BUILTIN_DIR, extra_dir):
        if directory is not None and directory.is_dir():
            for path in sorted(directory.glob("*.toml")):
                files[path.stem] = path
    return files


def load_regions(ids: list[str], extra_dir: Path | None = None) -> list[Region]:
    files = available_region_files(extra_dir)
    regions = []
    for region_id in ids:
        if region_id not in files:
            raise ConfigError(f"unknown region '{region_id}' (available: {', '.join(sorted(files))})")
        regions.append(load_region_file(files[region_id]))
    if not regions:
        raise ConfigError("at least one region must be configured (PRACK_REGIONS)")
    return regions


def region_for(regions: list[Region], lat: float, lon: float) -> Region | None:
    """The first region whose box contains the point."""
    return next((r for r in regions if r.contains(lat, lon)), None)
