"""Simulated paragliders as APRS lines, for ``prack demo`` and for end-to-end tests without the internet.

Each pilot flies a deterministic flight: stand at the launch, a launch run, thermals (circles with the climb and
turn rate a FLARM would report) joined by glides, a final glide, landing and ten minutes standing in the field.
A pilot can be heard over several protocols at once, like real ones: FLARM every second, FANET every four seconds
with a slightly different position, plus a FANET status line with the pilot name. Noise that the filters must
reject is included: a glider, a hang glider, an ADS-B "paraglider" at 300 km/h, a stealth device.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..regions import Region
from .builder import build_position, build_status

M_PER_DEG_LAT = 111_195.0
KMH = 3.6


@dataclass(frozen=True)
class PilotSpec:
    address: str
    protocols: tuple[str, ...] = ("flarm",)  # flarm, fanet, ogn, adsl
    name: str | None = None  # FANET pilot name
    delay_s: int = 0  # starts this long after the simulator
    aircraft_type: int = 7
    flight_s: int = 2400  # about how long the pilot stays up
    launch: tuple[float, float] | None = None  # lat, lon; default: somewhere in the region
    stealth: bool = False
    sender: str = "pilot"  # "pilot" or "adsb" (an ADS-B target that claims to be a paraglider, at 300 km/h)
    stand_s: int = 600  # standing in the landing field at the end
    drop_m: float | None = None  # altitude lost between launch and landing (default 800-1100 m)


@dataclass(slots=True)
class Sample:
    lat: float
    lon: float
    alt: float
    speed: float  # km/h
    course: int
    climb: float  # m/s
    turn: float  # deg/s


@dataclass
class _Pilot:
    spec: PilotSpec
    samples: list[Sample] = field(default_factory=list)


Ground = Callable[[float, float], float | None]


def build_flight(spec: PilotSpec, launch: tuple[float, float], seed: int, ground: Ground | None = None) -> list[Sample]:
    """One flight at 1 Hz, in a flat local frame (x east, y north, metres) converted to latitude and longitude.

    With ``ground`` (terrain height at a position) the flight is planned as height above the ground and then lifted
    onto the real terrain, so it agrees with a 3D map; without it, altitudes are invented and the flight loses
    ``drop_m`` metres between launch and landing.
    """
    rng = random.Random(seed)
    lat0, lon0 = launch
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    x = y = 0.0
    on_terrain = ground is not None and ground(lat0, lon0) is not None
    if on_terrain:
        alt = landing_alt = 2.0  # metres above the ground; launch and landing are on the ground
        g_smooth = ground(lat0, lon0)
    else:
        alt = 2000.0 + rng.uniform(-300, 300)
        landing_alt = alt - (spec.drop_m if spec.drop_m is not None else rng.uniform(800, 1100))
        g_smooth = 0.0
    heading = rng.uniform(0, 360)
    wind = (rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5))  # m/s drift while thermalling
    out: list[Sample] = []

    def emit(speed_kmh: float, climb: float, turn: float = 0.0, jitter: float = 0.0) -> None:
        nonlocal g_smooth
        lat = lat0 + (y + rng.gauss(0, jitter)) / M_PER_DEG_LAT
        lon = lon0 + (x + rng.gauss(0, jitter)) / m_per_deg_lon
        msl = alt
        if on_terrain:  # the planned height rides on terrain that may not change faster than 5 m/s
            g = ground(lat, lon)
            if g is not None:
                g_smooth += max(-5.0, min(5.0, g - g_smooth))
                msl = max(g_smooth + alt, g + min(alt, 20.0))  # and never closer than 20 m to the real ground
            else:
                msl = g_smooth + alt
        out.append(Sample(lat, lon, msl + rng.gauss(0, 0.3), speed_kmh, round(heading) % 360, climb, turn))

    for _ in range(60):  # standing at the launch
        emit(0.0, 0.0, jitter=0.3)
    for i in range(12):  # launch run, then off the ground
        speed = 28.0 * (i + 1) / 12
        step = speed / KMH
        x, y = x + step * math.sin(math.radians(heading)), y + step * math.cos(math.radians(heading))
        alt += 1.5 if i > 7 else 0.0
        emit(speed, 1.5 if i > 7 else 0.0)

    def fly(seconds: int, speed_kmh: float, sink: float, turn_dps: float = 0.0, radius: float = 0.0) -> None:
        nonlocal x, y, alt, heading
        for _ in range(seconds):
            if radius:  # circling: the centre drifts with the wind
                heading = (heading + turn_dps) % 360
                x, y = x + wind[0], y + wind[1]
            step = speed_kmh / KMH
            x, y = x + step * math.sin(math.radians(heading)), y + step * math.cos(math.radians(heading))
            alt += sink
            emit(speed_kmh, sink, turn_dps)

    ceiling = alt + 700  # cloud base: no thermalling above it
    while len(out) < spec.flight_s:
        if alt < landing_alt + 350 or (
            alt < ceiling and rng.random() < 0.55
        ):  # a thermal: circles of 45-80 m, climbing
            radius, climb = rng.uniform(45, 80), rng.uniform(1.4, 3.2)
            speed = rng.uniform(27, 32)
            turn = math.degrees(speed / KMH / radius) * rng.choice((-1, 1))
            fly(rng.randint(80, 220), speed, climb, turn, radius)
        else:  # glide towards the next one
            heading = (heading + rng.uniform(-60, 60)) % 360
            fly(rng.randint(100, 320), rng.uniform(36, 41), -rng.uniform(1.0, 1.4))
    sink = -1.2  # final glide, then the flare and landing
    while alt + sink > landing_alt + 6:
        fly(1, 37.0, sink)
    for i in range(8):
        fly(1, 37.0 * (7 - i) / 8, -0.3)
    for _ in range(spec.stand_s):  # standing in the landing field
        emit(0.0, 0.0, jitter=0.3)
    return out


class Simulator:
    def __init__(
        self,
        start: datetime,
        pilots: list[PilotSpec],
        region: Region | None = None,
        seed: int = 7,
        ground: Ground | None = None,
    ) -> None:
        self.start = start
        self.region = region
        rng = random.Random(seed)
        self.pilots: list[_Pilot] = []
        for i, spec in enumerate(pilots):
            launch = spec.launch or self._random_launch(rng)
            samples = build_flight(spec, launch, seed * 1000 + i, ground) if spec.sender == "pilot" else []
            self.pilots.append(_Pilot(spec, samples))
        self.launches = [p.samples[0] for p in self.pilots if p.samples]

    def _random_launch(self, rng: random.Random) -> tuple[float, float]:
        west, south, east, north = self.region.bbox if self.region else (7.5, 46.4, 8.5, 46.9)
        return (rng.uniform(south + 0.25 * (north - south), north - 0.25 * (north - south)),
                rng.uniform(west + 0.25 * (east - west), east - 0.25 * (east - west)))  # fmt: skip

    @classmethod
    def demo(
        cls,
        start: datetime,
        region: Region | None = None,
        pilots: int = 12,
        seed: int = 7,
        ground: Ground | None = None,
        *,
        protocols: Sequence[tuple[str, ...]] | None = None,
        noise: bool = True,
        stagger: int = 150,
        address_base: int = 0xD00000,
    ) -> Simulator:
        """A day's mix: FLARM, FANET and dual-protocol pilots starting ``stagger`` seconds apart, plus noise."""
        names = ["Mia", "Jonas", "Lena", "Noah", "Elin", "Luca", "Sofia", "Matteo", "Anna", "Finn", "Nora", "Elias"]
        combos = list(protocols or [("flarm",), ("fanet",), ("flarm", "fanet"), ("ogn",), ("flarm", "fanet", "adsl")])
        specs = []
        for i in range(pilots):
            combo = combos[i % len(combos)]
            specs.append(
                PilotSpec(
                    f"{address_base + 17 * i:06X}",
                    combo,
                    names[i % len(names)] if "fanet" in combo else None,
                    delay_s=stagger * i,
                    flight_s=1800 + 240 * (i % 5),
                )  # fmt: skip
            )
        if noise:
            specs += [
                PilotSpec("A00001", aircraft_type=1, delay_s=30),  # a glider
                PilotSpec("A00002", aircraft_type=6, delay_s=60, flight_s=1200),  # a hang glider
                PilotSpec("A00003", delay_s=90, stealth=True),  # a paraglider that does not want to be tracked
                PilotSpec("3FF19F", delay_s=0, sender="adsb"),  # an ADS-B target that says "paraglider", at 300 km/h
            ]
        return cls(start, specs, region, seed, ground)

    def lines(self, now: datetime) -> list[str]:
        """The APRS lines to send at this second of simulated time."""
        elapsed = int((now - self.start).total_seconds())
        out: list[str] = []
        for pilot in self.pilots:
            spec = pilot.spec
            if spec.sender == "adsb":
                out.append(self._adsb(spec, now, elapsed))
                continue
            i = elapsed - spec.delay_s
            if not 0 <= i < len(pilot.samples):
                continue
            s = pilot.samples[i]
            for protocol in spec.protocols:
                if protocol == "flarm":
                    out.append(self._beacon(spec, s, now, "FLR", "OGFLR", turn=True))
                elif protocol == "ogn" and i % 2 == 0:
                    out.append(self._beacon(spec, s, now, "OGN", "OGNTRK", turn=True, address_type=3))
                elif protocol == "adsl" and i % 3 == 0:
                    out.append(self._beacon(spec, s, now, "OGN", "OGADSL", address_type=3))
                elif protocol == "fanet" and (i + int(spec.address, 16)) % 4 == 0:
                    out.append(self._beacon(spec, s, now, "FNT", "OGNFNT", offset_m=11.0, address_type=3))
            if "fanet" in spec.protocols and spec.name and i % 60 == 5:  # FANET announces the pilot's name now and then
                out.append(build_status(now, spec.name, address=spec.address))
        return out

    @staticmethod
    def _beacon(spec: PilotSpec, s: Sample, now: datetime, prefix: str, tocall: str, *, turn: bool = False,
                offset_m: float = 0.0, address_type: int = 2) -> str:  # fmt: skip
        rich = prefix == "FLR"
        return build_position(
            now, s.lat + offset_m / M_PER_DEG_LAT, s.lon, s.alt, address=spec.address, prefix=prefix, tocall=tocall,
            speed_kmh=s.speed, course=s.course, climb_ms=s.climb, turn_dps=s.turn if turn else None,
            aircraft_type=spec.aircraft_type, address_type=address_type, stealth=spec.stealth,
            signal_db=12.5 if rich else None, errors=0 if rich else None, freq_khz=1.2 if rich else None,
            gps="2x3" if rich else None,
        )  # fmt: skip

    def _adsb(self, spec: PilotSpec, now: datetime, elapsed: int) -> str:
        lat, lon = self._random_launch(random.Random(1))
        return build_position(
            now, lat, lon + 0.0008 * (elapsed % 600), 3000.0, address=spec.address, prefix="ICA", tocall="OGADSB",
            speed_kmh=300.0, course=90, climb_ms=0.0, aircraft_type=7, address_type=1, symbol_table="\\", symbol="^",
        )  # fmt: skip

    def duration_s(self) -> int:
        """Seconds until every simulated pilot has landed and stood still for ten minutes."""
        return max((p.spec.delay_s + len(p.samples) for p in self.pilots), default=0)

    def at(self, seconds: int) -> datetime:
        return self.start + timedelta(seconds=seconds)
