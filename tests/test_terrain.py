from __future__ import annotations

import io
import math
import threading
import time

import httpx
import pytest
from PIL import Image

from prack.terrain import TILE_SIZE, TerrainService

from .helpers import terrarium_png

URL = "https://tiles.example/terrarium/{z}/{x}/{y}.png"


def slippy(lat: float, lon: float, zoom: int) -> tuple[int, int, float, float]:
    """Independent tile maths (inverse Gudermannian form) to check the service against."""
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return int(x), int(y), (x - int(x)) * TILE_SIZE, (y - int(y)) * TILE_SIZE


class Server:
    """A fake tile server that counts requests."""

    def __init__(self, height=lambda x, y: 1234.5, status: int = 200, body: bytes | None = None) -> None:
        self.requests: list[str] = []
        self.status = status
        self.body = body if body is not None else terrarium_png(height)
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        return httpx.Response(self.status, content=self.body if self.status == 200 else b"")


def service(tmp_path, server: Server, **kw) -> TerrainService:
    return TerrainService(URL, 12, tmp_path / "dem", transport=server.transport, **kw)


def test_tile_maths_matches_an_independent_implementation(tmp_path):
    svc = service(tmp_path, Server())
    for lat, lon in [(46.948, 7.447), (47.376, 8.541), (45.832, 6.865), (-33.9, 18.4), (0.0, 0.0), (60.1, -150.0)]:
        (z, x, y), px, py = svc._locate(lat, lon)
        ex, ey, epx, epy = slippy(lat, lon, 12)
        assert (z, x, y) == (12, ex, ey)
        assert (px, py) == pytest.approx((epx, epy), abs=1e-6)


def test_decoding_follows_the_terrarium_formula(tmp_path):
    svc = service(tmp_path, Server(lambda x, y: 4808.25))  # Mont Blanc; the tile holds 1/256 m, heights are 0.1 m
    assert svc.height(45.8326, 6.8652) == pytest.approx(4808.25, abs=0.1)
    below_sea = service(tmp_path / "b", Server(lambda x, y: -12.5))
    assert below_sea.height(46.0, 8.0) == pytest.approx(-12.5, abs=0.05)


def test_bilinear_interpolation_between_pixel_centres(tmp_path):
    svc = service(tmp_path, Server(lambda x, y: 1000.0 + 10.0 * x + 100.0 * y))
    rgb = svc._load((12, 2134, 1431), network=True)
    assert svc.sample(rgb, 100.5, 50.5) == pytest.approx(1000 + 1000 + 5000, abs=0.05)  # on a pixel centre
    assert svc.sample(rgb, 101.0, 50.5) == pytest.approx(1000 + 1005 + 5000, abs=0.05)  # halfway between two
    assert svc.sample(rgb, 100.5, 51.0) == pytest.approx(1000 + 1000 + 5050, abs=0.05)
    assert svc.sample(rgb, 0.0, 0.0) == pytest.approx(1000.0, abs=0.05)  # clamped at the tile edge
    assert svc.sample(rgb, 256.0, 256.0) == pytest.approx(1000 + 2550 + 25500, abs=1.0)


def test_tiles_are_downloaded_once_and_cached_on_disk(tmp_path):
    server = Server()
    svc = service(tmp_path, server)
    pts = [(46.948, 7.447), (46.949, 7.448), (46.950, 7.449)]
    assert svc.heights(pts) == [1234.5] * 3
    assert len(server.requests) == 1
    again = service(tmp_path, server)  # a new process: the PNG is on disk
    assert again.height(46.948, 7.447) == 1234.5
    assert len(server.requests) == 1
    path = next((tmp_path / "dem").rglob("*.png"))
    assert path.parent.parent.name == "12" and not list((tmp_path / "dem").rglob("*.part"))


def test_heights_returns_none_where_no_tile_is_available(tmp_path):
    server = Server(status=404)
    svc = service(tmp_path, server)
    assert svc.heights([(46.9, 7.4), (46.9, 7.4)]) == [None, None]
    assert svc.height(46.9, 7.4) is None
    assert len(server.requests) == 1  # a failed tile is not hammered
    assert TerrainService(URL, 12, tmp_path / "x", enabled=False).height(46.9, 7.4) is None


def test_garbage_is_never_cached(tmp_path):
    server = Server(body=b"<html>rate limited</html>")
    svc = service(tmp_path, server)
    assert svc.height(46.9, 7.4) is None
    assert not list((tmp_path / "dem").rglob("*"))
    small = io.BytesIO()
    Image.new("RGB", (64, 64)).save(small, format="PNG")  # a valid PNG, but not a 256 x 256 tile
    assert service(tmp_path / "w", Server(body=small.getvalue())).height(46.9, 7.4) is None


def test_a_corrupt_cache_file_is_replaced(tmp_path):
    server = Server()
    svc = service(tmp_path, server)
    svc.height(46.9, 7.4)
    path = next((tmp_path / "dem").rglob("*.png"))
    path.write_bytes(b"corrupt")
    fresh = service(tmp_path, server)
    assert fresh.height(46.9, 7.4) is None  # this call finds the corrupt file and removes it
    assert fresh.height(46.9, 7.4) == 1234.5  # the next call downloads again
    assert len(server.requests) == 2


def test_the_non_blocking_lookup_fetches_in_the_background(tmp_path):
    server = Server()
    svc = service(tmp_path, server)
    assert svc.height_cached(46.9, 7.4) is None  # miss: schedules a download
    deadline = time.monotonic() + 3
    while svc.height_cached(46.9, 7.4) is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert svc.height_cached(46.9, 7.4) == 1234.5 and len(server.requests) == 1


def test_the_memory_cache_is_bounded(tmp_path):
    svc = service(tmp_path, Server(), memory_tiles=2)
    for lon in (7.0, 7.5, 8.0, 8.5):
        svc.height(46.9, lon)
    assert len(svc._tiles) == 2


def test_tile_png_for_the_browser(tmp_path):
    server = Server()
    svc = service(tmp_path, server)
    png = svc.tile_png(12, 2134, 1431)
    assert png is not None and png.startswith(b"\x89PNG")
    assert svc.tile_png(12, 2134, 1431) == png and len(server.requests) == 1


def test_concurrent_lookups_are_safe(tmp_path):
    svc = service(tmp_path, Server())
    results: list[float | None] = []

    def work() -> None:
        for i in range(30):
            results.append(svc.height(46.9 + i * 1e-4, 7.4))

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [1234.5] * 120
