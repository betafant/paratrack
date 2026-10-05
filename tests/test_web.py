"""Terrain tiles, authentication, static files and the life cycle of the app."""

from __future__ import annotations

import math
import mimetypes

import httpx
import pytest

from prack.api import Guard, _near_regions, _tile_bounds, create_app, register_mime_types
from prack.regions import load_regions

from .helpers import terrarium_png
from .webrig import make_runtime, serve

# ---------------------------------------------------------------- terrain tiles

SWISS_TILE = (12, 2134, 1431)  # Basel, z/x/y


class TileServer:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[str] = []
        self.status = status
        self.png = terrarium_png(lambda x, y: 1500.0)
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        return httpx.Response(self.status, content=self.png if self.status == 200 else b"")


@pytest.fixture
def tiles(tmp_path):
    server = TileServer()
    runtime = make_runtime(tmp_path, terrain_transport=server.transport, ddb=False)
    with serve(create_app(runtime)) as (url, _), httpx.Client(base_url=url, timeout=20) as client:
        yield client, server


def test_a_terrain_tile_is_proxied_and_cached(tiles):
    client, server = tiles
    z, x, y = SWISS_TILE
    r = client.get(f"/api/dem/{z}/{x}/{y}.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert "immutable" in r.headers["cache-control"] and "max-age=604800" in r.headers["cache-control"]
    assert client.get(f"/api/dem/{z}/{x}/{y}.png").content == r.content
    assert len(server.requests) == 1  # the second one came from the disk cache


@pytest.mark.parametrize(
    "path",
    [
        "/api/dem/13/4268/2862.png",  # finer than the configured zoom
        "/api/dem/12/9999/1431.png",  # not a tile
        "/api/dem/12/2134/-1.png",
        "/api/dem/12/0/0.png",  # the Gulf of Guinea: nowhere near a region, and not ours to fetch
        "/api/dem/6/0/0.png",
        "/api/dem/1/0/0.png",  # a coarse tile that does not touch Switzerland
    ],
)
def test_tiles_outside_what_we_serve_are_refused_before_any_download(tiles, path):
    client, server = tiles
    assert client.get(path).status_code == 404 and server.requests == []


def test_coarse_tiles_that_cover_the_region_are_served(tiles):
    client, _ = tiles
    assert client.get("/api/dem/4/8/5.png").status_code == 200  # z4 tile over central Europe
    assert client.get("/api/dem/12/abc/1431.png").status_code == 422


def test_an_unavailable_tile_is_a_404_not_an_error(tmp_path):
    runtime = make_runtime(tmp_path, terrain_transport=TileServer(status=503).transport, ddb=False)
    with serve(create_app(runtime)) as (url, _):
        r = httpx.get(f"{url}/api/dem/12/2134/1431.png")
    assert r.status_code == 404 and r.json() == {"detail": "tile unavailable"}


def test_tile_bounds_and_region_test():
    west, south, east, north = _tile_bounds(*SWISS_TILE)
    assert west < 7.5886 < east and south < 47.5596 < north  # Basel (Marktplatz) lies in this tile
    assert 0.08 < east - west < 0.09 and 0.05 < north - south < 0.07  # about 6 km
    (ch,) = load_regions(["ch"])
    assert _near_regions([ch], *SWISS_TILE) and not _near_regions([ch], 12, 0, 0)
    assert _near_regions([ch], 0, 0, 0)  # the whole-world tile covers everything
    assert not _near_regions([ch], 6, 40, 20)  # Asia


# ---------------------------------------------------------------- authentication


@pytest.fixture(scope="module")
def locked(tmp_path_factory):
    runtime = make_runtime(tmp_path_factory.mktemp("auth"), ddb=False, auth=("pilot", "s3cret:with colon"))
    with serve(create_app(runtime, stream_interval=0.05)) as (url, _):
        yield url


def test_everything_but_health_needs_the_password(locked):
    assert httpx.get(f"{locked}/api/health").status_code == 200  # for uptime checks and docker
    for path in ("/", "/api/config", "/api/status", "/api/live", "/api/days", "/api/live/stream?limit=1",
                 "/api/dem/12/2134/1431.png", "/api/openapi.json", "/index.html"):  # fmt: skip
        r = httpx.get(f"{locked}{path}")
        assert r.status_code == 401, path
        assert (
            r.headers["www-authenticate"].startswith('Basic realm="prack"')
            and r.headers["x-content-type-options"] == "nosniff"
        )


def test_the_right_user_and_password_get_in_wrong_ones_do_not(locked):
    assert httpx.get(f"{locked}/api/config", auth=("pilot", "s3cret:with colon")).status_code == 200
    assert httpx.get(f"{locked}/", auth=("pilot", "s3cret:with colon")).status_code == 200
    for credentials in [
        ("pilot", "wrong"),
        ("someone", "s3cret:with colon"),
        ("pilot", ""),
        ("", ""),
        ("pilot", "s3cret"),
    ]:
        assert httpx.get(f"{locked}/api/config", auth=credentials).status_code == 401, credentials


@pytest.mark.parametrize(
    "header",
    ["Basic", "Basic !!!not base64!!!", "Bearer abc", "basic", "Basic cGlsb3Q=", "Digest username=pilot", ""],
)  # "cGlsb3Q=" is "pilot" without any colon
def test_malformed_authorization_headers_are_just_401(locked, header):
    assert httpx.get(f"{locked}/api/config", headers={"Authorization": header}).status_code == 401


def test_the_stream_is_protected_too(locked):
    with httpx.stream("GET", f"{locked}/api/live/stream?limit=1", auth=("pilot", "s3cret:with colon")) as r:
        assert r.status_code == 200 and "data: " in "".join(r.iter_text())


def test_without_a_password_configured_nothing_is_asked(tmp_path):
    guard = Guard(app=None, user="pilot", password="")  # half a configuration is no configuration
    assert guard.enabled is False


def test_failed_attempts_are_slowed_down(locked):
    import time

    started = time.monotonic()
    httpx.get(f"{locked}/api/config", auth=("pilot", "nope"))
    assert time.monotonic() - started >= 0.35


# ---------------------------------------------------------------- static files and MIME types


@pytest.fixture
def static_site(tmp_path):
    static = tmp_path / "static"
    (static / "js").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    for name, body in {
        "js/app.js": "export const a = 1;", "js/lib.mjs": "export default 2;", "style.css": "body{}",
        "icon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>", "font.woff2": "wOF2", "data.json": "{}",
    }.items():  # fmt: skip
        (static / name).write_text(body, encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    runtime = make_runtime(tmp_path, ddb=False)
    with serve(create_app(runtime, static_dir=static)) as (url, _):
        yield url


def test_the_windows_registry_cannot_break_javascript_modules(static_site, monkeypatch):
    # Windows takes types from the registry, where .js is often text/plain; browsers refuse such modules.
    for suffix in (".js", ".mjs", ".css", ".svg"):
        monkeypatch.setitem(mimetypes.types_map, suffix, "text/plain")
    assert mimetypes.guess_type("app.js")[0] == "text/plain"
    register_mime_types()  # what create_app does at start-up
    assert mimetypes.guess_type("app.js")[0] == "text/javascript"
    expected = {
        "/js/app.js": "text/javascript", "/js/lib.mjs": "text/javascript", "/style.css": "text/css",
        "/icon.svg": "image/svg+xml", "/font.woff2": "font/woff2", "/data.json": "application/json",
    }  # fmt: skip
    for path, mime in expected.items():
        r = httpx.get(static_site + path)
        assert r.status_code == 200 and r.headers["content-type"].split(";")[0] == mime, path


def test_create_app_registers_the_types_itself(tmp_path, monkeypatch):
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    create_app(make_runtime(tmp_path, ddb=False))
    assert mimetypes.guess_type("x.js")[0] == "text/javascript"


def test_the_index_is_served_and_unknown_paths_are_404(static_site):
    assert httpx.get(static_site + "/").text.startswith("<!doctype html>")
    assert httpx.get(static_site + "/nothing-here.js").status_code == 404
    assert httpx.get(static_site + "/api/nothing").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/js/../../secret.txt",
        "/..%2fsecret.txt",
        "/js/%2e%2e%2f%2e%2e%2fsecret.txt",
    ],
)
def test_files_outside_the_static_folder_are_unreachable(static_site, path):
    r = httpx.get(static_site + path)
    assert r.status_code in (400, 404) and "top secret" not in r.text


def test_the_bundled_front_end_page_is_served(tmp_path):
    runtime = make_runtime(tmp_path, ddb=False)
    with serve(create_app(runtime)) as (url, _):
        r = httpx.get(url + "/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "<title>prack</title>" in r.text


# ---------------------------------------------------------------- life cycle


def test_the_app_starts_and_stops_the_runtime_and_calls_its_hooks(tmp_path):
    runtime = make_runtime(tmp_path, ddb=False)
    calls = []
    app = create_app(
        runtime, manage_runtime=True, on_start=lambda: calls.append("start"), on_stop=lambda: calls.append("stop")
    )
    with serve(app) as (url, _):
        assert httpx.get(url + "/api/health").status_code == 200
        assert {t.name for t in runtime._threads} == {"finalizer", "writer"}
        assert all(t.is_alive() for t in runtime._threads) and calls == ["start"]
    assert calls == ["start", "stop"] and not any(t.is_alive() for t in runtime._threads)


def test_days_work_with_the_real_clock(tmp_path):
    runtime = make_runtime(tmp_path, ddb=False)
    with serve(create_app(runtime)) as (url, _):
        assert httpx.get(url + "/api/days").json() == []


def tile_of(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


@pytest.mark.parametrize("z", [4, 7, 10, 12])
def test_the_browser_is_only_told_about_tiles_the_server_serves(tiles, z):
    """``terrain.bounds`` in /api/config keeps MapLibre from asking for tiles that would only be answered with 404."""
    client, _ = tiles
    west, south, east, north = client.get("/api/config").json()["terrain"]["bounds"]
    for lon, lat in (
        (west + 0.01, south + 0.01),
        (east - 0.01, north - 0.01),
        ((west + east) / 2, (south + north) / 2),
    ):
        x, y = tile_of(lon, lat, z)
        assert client.get(f"/api/dem/{z}/{x}/{y}.png").status_code == 200, (lon, lat)
    if z >= 7:  # a coarser tile around Switzerland covers half of Europe: there is nothing outside to refuse
        x, y = tile_of(east + 3.0, north + 3.0, z)
        assert client.get(f"/api/dem/{z}/{x}/{y}.png").status_code == 404
