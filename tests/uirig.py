"""Browser tests: headless Chromium (Playwright) against the real app served by uvicorn, with a made-up feed.

Nothing leaves the machine: map tiles from swisstopo are answered with a plain tile, everything else that is not
the app itself is refused. WebGL runs on SwiftShader, so no GPU is needed.
"""

from __future__ import annotations

import io
import math
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from prack.api import create_app
from prack.ogn.simulator import PilotSpec
from prack.runtime import LinkStatus

from .helpers import terrarium_png
from .rig import beacon
from .webrig import fly_day, make_runtime, serve

SHOTS = Path(__file__).parent / "shots"
LAUNCH_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
ACCENT = (139, 124, 255)  # the marker colour in the dark theme
LONG = {"flight_s": 1500, "stand_s": 300}
SHORT = {"flight_s": 120, "drop_m": 300.0, "stand_s": 300}
PAST_DAYS = {1: ["D00201", "D00202"], 2: ["D00101", "D00102", "D00103"]}  # days back -> pilots who flew then


def base_tile() -> bytes:
    image = Image.new("RGB", (256, 256), (226, 227, 230))
    draw = ImageDraw.Draw(image)
    for i in range(0, 256, 32):
        draw.line([(i, 0), (i, 255)], fill=(205, 206, 210))
        draw.line([(0, i), (255, i)], fill=(205, 206, 210))
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


def hills(x: int, y: int) -> float:
    """Terrain with real relief, so 3D has something to show."""
    return 1300.0 + 500.0 * math.sin(x / 23.0) * math.cos(y / 31.0)


def chromium_candidates() -> list[str]:
    found = []
    if os.environ.get("PRACK_TEST_CHROMIUM"):
        found.append(os.environ["PRACK_TEST_CHROMIUM"])
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    if root.is_dir():
        found += [str(p) for p in sorted(root.glob("chromium-*/chrome-linux*/chrome"))]
        found.append(str(root / "chromium"))
    return [c for c in found if Path(c).exists()]


def launch_chromium(playwright):
    try:
        return playwright.chromium.launch(args=LAUNCH_ARGS)
    except Exception as error:  # noqa: BLE001 - Playwright's own browser is not installed: look for another one
        for path in chromium_candidates():
            try:
                return playwright.chromium.launch(executable_path=path, args=LAUNCH_ARGS)
            except Exception:  # noqa: BLE001, S112
                continue
        pytest.skip(f"no Chromium for Playwright ({str(error).splitlines()[0]}); run `playwright install chromium`")


@contextmanager
def browser() -> Iterator:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        chromium = launch_chromium(playwright)
        try:
            yield chromium
        finally:
            chromium.close()


class Console:
    """What the page said that should never be there: errors, uncaught exceptions, failed requests to the app."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.problems: list[str] = []

    def attach(self, page) -> None:
        page.on(
            "console",
            lambda m: (
                m.type == "error"
                and "Failed to load resource" not in m.text  # the response listener below names the URL
                and self.problems.append(f"console error: {m.text[:300]}")
            ),
        )
        page.on(
            "response",
            lambda r: (
                r.status >= 400
                and r.url.startswith(self.base_url)
                and self.problems.append(f"HTTP {r.status}: {r.url}")
            ),
        )
        page.on("pageerror", lambda e: self.problems.append(f"uncaught: {e}"))
        page.on(
            "requestfailed",
            lambda r: (
                r.url.startswith(self.base_url)
                and "/api/live/stream" not in r.url
                and "ERR_ABORTED" not in str(r.failure)  # the map drops tile requests it no longer needs
                and self.problems.append(f"request failed: {r.url} {r.failure}")
            ),
        )


def new_context(chromium, base_url: str, *, theme: str = "dark", size=(1280, 800), touch: bool = False):
    tile = base_tile()
    context = chromium.new_context(
        viewport={"width": size[0], "height": size[1]}, locale="en-GB", color_scheme=theme, has_touch=touch
    )
    context.route(re.compile(r"^(?!http://127\.0\.0\.1)"), lambda route: route.abort())  # nothing leaves the machine
    context.route("**/wmts.geo.admin.ch/**", lambda route: route.fulfill(body=tile, content_type="image/jpeg"))
    return context


@contextmanager
def live_app(tmp_path: Path, *, terrain: bool = True, grounded: bool = True, link: str = "connected") -> Iterator[str]:
    """The app with four paragliders in the air (and one on the ground), as the live feed would have left it, and
    closed flights on the two days before: two yesterday, three the day before."""
    transport = None
    if terrain:
        import httpx

        png = terrarium_png(hills)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=png))
    runtime = make_runtime(tmp_path, terrain_transport=transport, ddb=False)
    for back, addresses in PAST_DAYS.items():  # earlier days: closed flights with their previews
        day = datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=back)
        pilots = [
            PilotSpec(a, ("flarm",), launch=(46.60 + 0.05 * i, 7.60 + 0.1 * i), delay_s=90 * i, **SHORT)
            for i, a in enumerate(addresses)
        ]
        fly_day(runtime, day, pilots)
        runtime.tracker.close_all()
        runtime.finalizer.drain()
    runtime.tracker.sweep(datetime.now(UTC))  # those pilots went home: nothing of them stays on the live map
    start = datetime.now(UTC) - timedelta(minutes=14)
    launch = (46.6453, 7.6511)
    fly_day(
        runtime, start,
        [
            PilotSpec("D00001", ("flarm",), launch=launch, **LONG),
            PilotSpec("D00002", ("flarm", "fanet"), name="Mia", launch=(46.70, 7.80), delay_s=60, **LONG),
            PilotSpec("D00003", ("fanet",), name="Jonas", launch=(46.55, 7.40), delay_s=120, **LONG),
            PilotSpec("D00004", ("ogn",), launch=(46.60, 7.95), delay_s=180, **LONG),
        ],
        until=600,
    )  # fmt: skip
    if grounded:
        when = start + timedelta(seconds=600)
        runtime.tracker.process_beacon(beacon(int(when.timestamp()), 46.62, 7.70, 800.0, 0.0, address="D00099"), when)
    runtime.tracker.flush(force=True)  # the flights table knows about the flights in the air (no writer thread here)
    runtime.link = LinkStatus(state=link, server="test")
    app = create_app(runtime, stream_interval=0.4, full_every=4.0)
    with serve(app) as (url, _):
        yield url


def wait_for_aircraft(page, n: int, timeout: float = 60_000) -> None:
    page.wait_for_function(f"window.prack && window.prack.store.size >= {n}", timeout=timeout)


def shot(page, name: str) -> Path:
    SHOTS.mkdir(exist_ok=True)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path))
    return path


def screen_position(page, address: str) -> tuple[float, float]:
    x, y = page.evaluate(
        "(a) => { const p = window.prack; const e = p.store.find(a);"
        " return p.view.project(e.lon, e.lat, p.view.mode3d ? e.alt : 0); }",
        address,
    )
    return x, y


def count_pixels(png: bytes, around: tuple[float, float], colour=ACCENT, radius: int = 40, tolerance: int = 40) -> int:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    cx, cy = int(around[0]), int(around[1])
    n = 0
    for y in range(max(0, cy - radius), min(image.height, cy + radius)):
        for x in range(max(0, cx - radius), min(image.width, cx + radius)):
            r, g, b = image.getpixel((x, y))
            if abs(r - colour[0]) < tolerance and abs(g - colour[1]) < tolerance and abs(b - colour[2]) < tolerance:
                n += 1
    return n


def count_saturated(png: bytes, around: tuple[float, float], radius: int = 14, chroma: int = 70) -> int:
    """Pixels near a point that are clearly coloured (the map itself is grey): the altitude colours of a path."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    cx, cy = int(around[0]), int(around[1])
    n = 0
    for y in range(max(0, cy - radius), min(image.height, cy + radius)):
        for x in range(max(0, cx - radius), min(image.width, cx + radius)):
            r, g, b = image.getpixel((x, y))
            if max(r, g, b) - min(r, g, b) > chroma:
                n += 1
    return n


def camera_still(page, quiet: float = 0.6) -> None:
    """Wait until the camera has stopped (an animation to fit the day may start a moment after the day has loaded)."""
    page.wait_for_function(
        f"""(() => {{
          const m = window.prack.view.map; const c = m.getCenter();
          const key = [c.lng, c.lat, m.getZoom(), m.getPitch()].join();
          const now = performance.now();
          if (window.__still?.key !== key || m.isMoving()) window.__still = {{ key, since: now }};
          return now - window.__still.since > {quiet * 1000};
        }})()""",
        timeout=30_000,
        polling=100,
    )


def local_day(back: int):
    """The calendar day ``back`` days ago in Switzerland, which is where the app's days are counted."""
    return datetime.now(ZoneInfo("Europe/Zurich")).date() - timedelta(days=back)


def day_label(day) -> str:
    """How the date button writes a day: "Mon 5 Oct" (no strftime flag that Windows does not know)."""
    return f"{day:%a} {day.day} {day:%b}"


def jump(page, address: str, zoom: float) -> None:
    page.evaluate(
        "([a, z]) => { const p = window.prack; const e = p.store.find(a);"
        " p.view.map.jumpTo({center: [e.lon, e.lat], zoom: z}); }",
        [address, zoom],
    )
    page.wait_for_function("window.prack.layer('markers') !== null")
    page.wait_for_timeout(600)


def label_texts(page) -> list[str]:
    return page.evaluate(
        "(() => { const l = window.prack.layer('labels');"
        " return l ? l.props.data.map((d) => l.props.getText(d)) : []; })()"
    )


def shown(page, layer: str) -> int:
    return page.evaluate("(id) => { const l = window.prack.layer(id); return l ? l.props.data.length : 0; }", layer)
