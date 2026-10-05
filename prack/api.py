"""The web API and the static front-end.

    GET /api/config                      regions, base maps, terrain tiles, live window
    GET /api/live, /api/live/stream      aircraft heard in the last minutes (snapshot, server-sent events)
    GET /api/days                        airborne flights per local day (calendar)
    GET /api/days/{date}/flights         the flights of a day with statistics and a preview path
    GET /api/flights/{id}[/track]        one flight, and its track as columns
    GET /api/dem/{z}/{x}/{y}.png         terrain tiles for the browser's 3D view
    GET /api/health, /api/status         liveness, and what is arriving and why things are dropped

Optional HTTP basic auth covers everything except ``/api/health``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import mimetypes
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, or_, select, text
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .labels import label_for
from .models import Device, Fix, Flight
from .regions import Region
from .runtime import Runtime
from .tracking import rules
from .units import ALT, CLIMB, LATLON, SPEED

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_RANGE_DAYS = 400
MAX_ZOOM = 15
TILE_MARGIN_DEG = 0.6  # DEM tiles are served only near the configured regions
# Active flights are listed once they have climbed or descended this much: a car ride is not a flight.
ACTIVE_MIN_RANGE_M = 50.0
LISTED = or_(
    Flight.airborne.is_(True),
    and_(Flight.status == "active", Flight.max_alt - Flight.min_alt > ACTIVE_MIN_RANGE_M),
)


def register_mime_types() -> None:
    """Python takes MIME types from the Windows registry, where ``.js`` can be ``text/plain``, and browsers refuse
    ES modules served that way. Set the ones the front-end needs explicitly."""
    for mime, suffix in (
        ("text/javascript", ".js"),
        ("text/javascript", ".mjs"),
        ("text/css", ".css"),
        ("image/svg+xml", ".svg"),
        ("font/woff2", ".woff2"),
        ("application/json", ".json"),
    ):
        mimetypes.add_type(mime, suffix)


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser checks again on every load (a cheap 304), so an upgrade shows up at once."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class Guard:
    """Optional HTTP basic auth, plus a few headers that every response should carry. Pure ASGI, so it does not
    interfere with streaming responses."""

    OPEN_PATHS = frozenset({"/api/health"})
    HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
    }

    def __init__(self, app: ASGIApp, user: str = "", password: str = "") -> None:
        self.app = app
        self.user = user.encode()
        self.password = password.encode()
        self.enabled = bool(user and password)

    def _authorized(self, scope: Scope) -> bool:
        header = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, value = header.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            user, _, password = base64.b64decode(value.strip(), validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        # both comparisons always run, so the time taken does not tell which part was wrong
        ok_user = secrets.compare_digest(user, self.user)
        ok_password = secrets.compare_digest(password, self.password)
        return ok_user and ok_password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.enabled and scope["path"] not in self.OPEN_PATHS and not self._authorized(scope):
            await asyncio.sleep(0.4)  # makes guessing passwords slow
            response = Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="prack", charset="UTF-8"', **self.HEADERS},
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self.HEADERS.items():
                    headers.setdefault(key, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _epoch(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp())


def _point(t: datetime | None, lat: float | None, lon: float | None, alt: float | None) -> dict | None:
    if t is None and lat is None:
        return None
    return {"t": _epoch(t), "lat": lat, "lon": lon, "alt": alt}


def flight_json(flight: Flight, device: Device, *, preview: bool = True) -> dict[str, Any]:
    """A flight as the browser wants it."""
    end = flight.landing_time if flight.close_reason == "landed" and flight.landing_time else flight.end_time
    out: dict[str, Any] = {
        "id": flight.id,
        "label": label_for(device.pilot_name, device.competition_id, device.registration, device.callsign),
        "callsign": device.callsign,
        "address": device.address,
        "pilot": device.pilot_name,
        "reg": device.registration,
        "cn": device.competition_id,
        "model": device.model,
        "source": flight.source,
        "region": flight.region,
        "date": flight.date.isoformat(),
        "status": flight.status,
        "close_reason": flight.close_reason,
        "airborne": bool(flight.airborne),
        "live": flight.status == "active",
        "utc_offset_s": flight.utc_offset_s,
        "start": _epoch(flight.start_time),
        "end": _epoch(flight.end_time),
        "takeoff": _point(flight.takeoff_time, flight.takeoff_lat, flight.takeoff_lon, flight.takeoff_alt),
        "landing": (  # an open flight has not landed yet: its "landing" columns hold the latest position
            _point(flight.landing_time, flight.landing_lat, flight.landing_lon, flight.landing_alt)
            if flight.landing_time is not None
            else None
        ),
        "stats": {
            "fix_count": flight.fix_count,
            "duration_s": _epoch(flight.end_time) - _epoch(flight.start_time),
            "airtime_s": (_epoch(end) - _epoch(flight.takeoff_time)) if flight.takeoff_time and end else None,
            "max_alt": flight.max_alt,
            "min_alt": flight.min_alt,
            "max_agl": flight.max_agl,
            "alt_gain": flight.alt_gain,
            "max_climb": flight.max_climb,
            "max_sink": flight.max_sink,
            "max_speed": flight.max_speed,
            "distance_km": flight.distance_km,
            "straight_km": flight.straight_km,
            "max_from_start_km": flight.max_from_start_km,
        },
        "bbox": [flight.min_lon, flight.min_lat, flight.max_lon, flight.max_lat],
    }
    if preview:
        out["preview"] = flight.preview
    return out


def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a slippy-map tile."""
    n = 2**z

    def lat(tile_y: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * tile_y / n))))

    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def _terrain_bounds(regions: list[Region]) -> list[float]:
    """[west, south, east, north] around all regions, slightly inside what ``_near_regions`` serves: the browser is
    told not to ask for tiles beyond it (which would only be answered with 404)."""
    inset = TILE_MARGIN_DEG - 0.02
    return [
        round(min(r.bbox[0] for r in regions) - inset, 4),
        round(max(-85.0, min(r.bbox[1] for r in regions) - inset), 4),
        round(max(r.bbox[2] for r in regions) + inset, 4),
        round(min(85.0, max(r.bbox[3] for r in regions) + inset), 4),
    ]


def _near_regions(regions: list[Region], z: int, x: int, y: int) -> bool:
    west, south, east, north = _tile_bounds(z, x, y)
    for r in regions:
        rw, rs, re_, rn = r.bbox
        if (
            east >= rw - TILE_MARGIN_DEG
            and west <= re_ + TILE_MARGIN_DEG
            and north >= rs - TILE_MARGIN_DEG
            and south <= rn + TILE_MARGIN_DEG
        ):
            return True
    return False


def create_app(
    runtime: Runtime,
    *,
    manage_runtime: bool = False,
    stream_interval: float = 1.0,
    full_every: float = 60.0,
    static_dir: Path | None = STATIC_DIR,
    today: Callable[[], date] | None = None,
    on_start: Callable[[], None] | None = None,
    on_stop: Callable[[], None] | None = None,
) -> FastAPI:
    """The application. With ``manage_runtime`` it starts the runtime's threads on start-up and stops them on exit.

    ``today`` replaces the clock for the default range of ``/api/days`` (tests).
    """
    register_mime_types()
    settings, tracker, db = runtime.settings, runtime.tracker, runtime.db

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_runtime:
            await asyncio.to_thread(runtime.start)
        if on_start:
            await asyncio.to_thread(on_start)
        try:
            yield
        finally:
            if on_stop:
                await asyncio.to_thread(on_stop)
            if manage_runtime:
                await asyncio.to_thread(runtime.stop)

    app = FastAPI(
        title="prack",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # ------------------------------------------------------------------ small things

    @app.get("/api/health")
    def health() -> dict:
        try:
            with db.session() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"database unavailable: {type(exc).__name__}") from None
        return {"status": "ok", "version": __version__, "time": int(time.time())}

    @app.get("/api/status")
    def status() -> dict:
        return runtime.status()

    @app.get("/api/config")
    def config() -> dict:
        return {
            "version": __version__,
            "regions": [r.public() for r in runtime.regions],
            "terrain": {
                "enabled": settings.terrain_enabled,
                "bounds": _terrain_bounds(runtime.regions),
                "url": "/api/dem/{z}/{x}/{y}.png",
                "encoding": "terrarium",
                "tile_size": 256,
                "max_zoom": settings.terrain_zoom,
            },
            "live_window_s": rules.LIVE_WINDOW_S,
            "trail_points": rules.TRAIL_POINTS,
            "stream_interval_s": stream_interval,
            "auth": settings.auth_enabled,
        }

    # ------------------------------------------------------------------ live

    @app.get("/api/live")
    async def live() -> dict:
        """A full snapshot: every live aircraft with its recent trail."""
        return await asyncio.to_thread(tracker.live)

    @app.get("/api/live/stream")
    async def live_stream(request: Request, limit: int | None = Query(None, ge=1, le=100_000)) -> StreamingResponse:
        """One message per second: what changed since the last one, and every ``full_every`` seconds a full snapshot.

        ``limit`` ends the stream after that many messages (debugging, tests). A reconnecting browser starts again
        with a full snapshot.
        """

        async def events() -> AsyncIterator[str]:
            since: int | None = None
            next_full = time.monotonic() + full_every
            sent = 0
            while not await request.is_disconnected():
                full = since is None or time.monotonic() >= next_full
                message = await asyncio.to_thread(tracker.live, None if full else since)
                if full:
                    next_full = time.monotonic() + full_every
                since = message["seq"]
                yield f"id: {message['seq']}\ndata: {json.dumps(message, separators=(',', ':'))}\n\n"
                sent += 1
                if limit is not None and sent >= limit:
                    return
                await asyncio.sleep(stream_interval)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    # ------------------------------------------------------------------ history

    def local_today() -> date:
        return today() if today is not None else datetime.now(runtime.regions[0].tz).date()

    @app.get("/api/days")
    def days(
        start: date | None = None, end: date | None = None, region: str | None = Query(None, max_length=32)
    ) -> list[dict]:
        """``[{date, total}]``: flights per local day, for shading the calendar. Default: the last 90 days."""
        last = end or local_today()
        first = start or last - timedelta(days=89)
        if first > last:
            raise HTTPException(422, "start is after end")
        if (last - first).days > MAX_RANGE_DAYS:
            raise HTTPException(422, f"at most {MAX_RANGE_DAYS} days at a time")
        query = select(Flight.date, func.count()).where(LISTED, Flight.date >= first, Flight.date <= last)
        if region:
            query = query.where(Flight.region == region)
        with db.session() as session:
            rows = session.execute(query.group_by(Flight.date).order_by(Flight.date)).all()
        return [{"date": d.isoformat(), "total": n} for d, n in rows]

    @app.get("/api/days/{day}/flights")
    def day_flights(day: date, region: str | None = Query(None, max_length=32)) -> list[dict]:
        """The flights that started on this local day, with statistics and the simplified preview path."""
        query = select(Flight, Device).join(Device, Device.id == Flight.device_id).where(LISTED, Flight.date == day)
        if region:
            query = query.where(Flight.region == region)
        with db.session() as session:
            rows = session.execute(query.order_by(Flight.start_time, Flight.id)).all()
            return [flight_json(f, d) for f, d in rows]

    def load_flight(flight_id: int) -> tuple[Flight, Device]:
        with db.session() as session:
            row = session.execute(
                select(Flight, Device).join(Device, Device.id == Flight.device_id).where(Flight.id == flight_id)
            ).first()
        if row is None:
            raise HTTPException(404, "no such flight")
        return row[0], row[1]

    @app.get("/api/flights/{flight_id}")
    def flight(flight_id: int) -> dict:
        f, d = load_flight(flight_id)
        return flight_json(f, d)

    @app.get("/api/flights/{flight_id}/track")
    def track(flight_id: int) -> dict:
        """The whole track as columns: ``t`` (epoch s), ``lat``, ``lon``, ``alt`` (m), ``gnd`` (terrain, m),
        ``spd`` (km/h), ``vs`` (m/s), ``hdg`` (degrees). Positions not yet written to the database are included."""
        f, _ = load_flight(flight_id)
        with db.session() as session:
            rows = session.execute(
                select(Fix.ts, Fix.lat, Fix.lon, Fix.alt, Fix.ground, Fix.speed, Fix.climb, Fix.track)
                .where(Fix.flight_id == flight_id)
                .order_by(Fix.ts)
            ).all()
        out: dict[str, list] = {k: [] for k in ("t", "lat", "lon", "alt", "gnd", "spd", "vs", "hdg")}
        for ts, lat, lon, alt, ground, speed, climb, heading in rows:
            out["t"].append(ts)
            out["lat"].append(round(lat / LATLON, 6))
            out["lon"].append(round(lon / LATLON, 6))
            out["alt"].append(round(alt / ALT, 1))
            out["gnd"].append(None if ground is None else round(ground / ALT, 1))
            out["spd"].append(None if speed is None else round(speed / SPEED, 1))
            out["vs"].append(None if climb is None else round(climb / CLIMB, 2))
            out["hdg"].append(heading)
        if f.status == "active":
            known = out["t"][-1] if out["t"] else -1
            for p in tracker.unflushed(flight_id):
                if p.ts > known:
                    out["t"].append(p.ts)
                    out["lat"].append(round(p.lat, 6))
                    out["lon"].append(round(p.lon, 6))
                    out["alt"].append(round(p.alt, 1))
                    out["gnd"].append(None if p.ground is None else round(p.ground, 1))
                    out["spd"].append(None if p.speed is None else round(p.speed, 1))
                    out["vs"].append(None if p.climb is None else round(p.climb, 2))
                    out["hdg"].append(None if p.track is None else round(p.track) % 360)
        return {"flight_id": flight_id, "n": len(out["t"]), **out}

    # ------------------------------------------------------------------ terrain tiles

    @app.get("/api/dem/{z}/{x}/{y}.png")
    def dem(z: int, x: int, y: int) -> Response:
        """Terrarium tiles for the 3D terrain, fetched once from the tile server and cached on disk."""
        if not (0 <= z <= min(MAX_ZOOM, settings.terrain_zoom) and 0 <= x < 2**z and 0 <= y < 2**z):
            raise HTTPException(404, "no such tile")
        if not _near_regions(runtime.regions, z, x, y):
            raise HTTPException(404, "tile outside the configured regions")
        png = runtime.terrain.tile_png(z, x, y)
        if png is None:
            raise HTTPException(404, "tile unavailable")
        return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=604800, immutable"})

    # ------------------------------------------------------------------ static front-end, last

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", RevalidatedStaticFiles(directory=static_dir, html=True), name="static")

    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_middleware(Guard, user=settings.auth_user, password=settings.auth_password)
    return app
