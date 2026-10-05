"""Runtime configuration from environment variables (prefix ``PRACK_``) and an optional ``.env`` file.

Variables already set in the environment win over the ``.env`` file, so the same setup works on a
laptop, in Docker and under systemd.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from .ogn.constants import PARAGLIDER

T = TypeVar("T")

_CALLSIGN_RE = re.compile(r"^[A-Za-z0-9-]{1,9}$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class ConfigError(ValueError):
    """A configuration value is missing or invalid; the message names the variable."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines. Supports ``export``, quotes and trailing `` # comments``."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value.count(value[0]) >= 2:
            value = value[1 : value.index(value[0], 1)]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if key:
            values[key] = value
    return values


def read_dotenv(path: Path) -> dict[str, str]:
    """Read a ``.env`` file. Windows editors and PowerShell redirection may write UTF-8 or UTF-16 with a BOM."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: cannot read the file; save it as UTF-8") from None
    return parse_dotenv(text)


@dataclass
class Settings:
    data_dir: Path = Path("data")
    database_url: str = ""  # empty: SQLite file in data_dir
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # Regions to track: ids of region files, plus an optional folder with extra region files.
    regions: list[str] = field(default_factory=lambda: ["ch"])
    regions_dir: Path | None = None

    # OGN APRS-IS feed (read only)
    ogn_host: str = "aprs.glidernet.org"
    ogn_port: int = 14580
    ogn_callsign: str = ""  # empty: PRACK + 4 random digits
    ogn_filter_margin_km: float = 30.0  # receive this far beyond the region box

    # What is tracked
    tracked_types: tuple[int, ...] = (PARAGLIDER,)
    respect_stealth: bool = True
    min_fix_interval: float = 1.0  # seconds between stored fixes per aircraft

    # OGN device database (registrations, competition numbers, opt-out flags)
    ddb_enabled: bool = True
    ddb_url: str = "https://ddb.glidernet.org/download/?j=1&t=1"

    # Optional HTTP basic auth
    auth_user: str = ""
    auth_password: str = ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL. The default is a SQLite file below the data directory."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'prack.db').resolve().as_posix()}"

    def resolved_callsign(self) -> str:
        """Read-only APRS login name; it must be unique per connection on the OGN servers."""
        return self.ogn_callsign or f"PRACK{secrets.randbelow(10000):04d}"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, dotenv: Path | None = Path(".env")) -> Settings:
        """Build settings from ``environ`` (default: the process environment) and a ``.env`` file."""
        env: dict[str, str] = {}
        if dotenv is not None and dotenv.is_file():
            env.update(read_dotenv(dotenv))
        env.update(os.environ if environ is None else environ)
        s = cls()

        def get(name: str, default: T, convert: Callable[[str], T]) -> T:
            raw = env.get(f"PRACK_{name}", "").strip()
            if raw == "":
                return default
            try:
                return convert(raw)
            except ValueError as exc:
                raise ConfigError(f"PRACK_{name}={raw!r}: {exc}") from None

        s.data_dir = get("DATA_DIR", s.data_dir, Path)
        s.database_url = get("DATABASE_URL", s.database_url, str)
        s.host = get("HOST", s.host, str)
        s.port = get("PORT", s.port, _port)
        s.log_level = get("LOG_LEVEL", s.log_level, _log_level)
        s.regions = get("REGIONS", s.regions, _csv)
        s.regions_dir = get("REGIONS_DIR", s.regions_dir, Path)
        s.ogn_host = get("OGN_HOST", s.ogn_host, str)
        s.ogn_port = get("OGN_PORT", s.ogn_port, _port)
        s.ogn_callsign = get("OGN_CALLSIGN", s.ogn_callsign, _callsign)
        s.ogn_filter_margin_km = get("OGN_FILTER_MARGIN_KM", s.ogn_filter_margin_km, _non_negative)
        s.tracked_types = get("TRACKED_TYPES", s.tracked_types, _types)
        s.respect_stealth = get("RESPECT_STEALTH", s.respect_stealth, _bool)
        s.min_fix_interval = get("MIN_FIX_INTERVAL", s.min_fix_interval, _fix_interval)
        s.ddb_enabled = get("DDB_ENABLED", s.ddb_enabled, _bool)
        s.ddb_url = get("DDB_URL", s.ddb_url, _url)
        s.auth_user = get("AUTH_USER", s.auth_user, str)
        s.auth_password = get("AUTH_PASSWORD", s.auth_password, str)
        if bool(s.auth_user) != bool(s.auth_password):
            raise ConfigError("PRACK_AUTH_USER and PRACK_AUTH_PASSWORD must be set together")
        if not s.regions:
            raise ConfigError("PRACK_REGIONS: at least one region is required")
        return s


def _bool(raw: str) -> bool:
    value = raw.lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError("expected true or false")


def _port(raw: str) -> int:
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError("expected a port between 1 and 65535")
    return port


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _types(raw: str) -> tuple[int, ...]:
    types = tuple(dict.fromkeys(int(item) for item in _csv(raw)))
    if not types or any(not 0 <= t <= 15 for t in types):
        raise ValueError("expected OGN aircraft types between 0 and 15, e.g. 7")
    return types


def _callsign(raw: str) -> str:
    if not _CALLSIGN_RE.match(raw):
        raise ValueError("expected 1 to 9 letters, digits or '-'")
    return raw


def _log_level(raw: str) -> str:
    level = raw.upper()
    if level not in _LOG_LEVELS:
        raise ValueError(f"expected one of {', '.join(_LOG_LEVELS)}")
    return level


def _non_negative(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise ValueError("expected a number >= 0")
    return value


def _fix_interval(raw: str) -> float:
    value = float(raw)
    if not 0 < value <= 60:
        raise ValueError("expected seconds between 0 and 60")
    return value


def _url(raw: str) -> str:
    if not raw.startswith(("http://", "https://")):
        raise ValueError("expected an http(s) URL")
    return raw
