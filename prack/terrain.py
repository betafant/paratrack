"""Terrain height from Terrarium DEM tiles (global, no API key).

Elevation in metres = R * 256 + G + B / 256 - 32768. Tiles are downloaded on demand, cached on disk and sampled
bilinearly at a fixed zoom (12 is about 30 m per pixel in the Alps). The same tiles feed the 3D terrain in the
browser, so ground height in the data and in the 3D view agree.

https://registry.opendata.aws/terrain-tiles/
"""

from __future__ import annotations

import io
import logging
import math
import os
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

import httpx
from PIL import Image

log = logging.getLogger(__name__)

TILE_SIZE = 256
FAILED_RETRY_SECONDS = 600.0
Key = tuple[int, int, int]  # zoom, x, y


class TerrainService:
    def __init__(
        self,
        url_template: str,
        zoom: int,
        cache_dir: Path,
        *,
        enabled: bool = True,
        memory_tiles: int = 64,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url_template = url_template
        self.zoom = zoom
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.memory_tiles = memory_tiles
        self._transport = transport
        self._tiles: OrderedDict[Key, bytes] = OrderedDict()  # decoded RGB, 196 kB each
        self._failed: dict[Key, float] = {}
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._queue: queue.Queue[Key] = queue.Queue()
        self._queued: set[Key] = set()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------ tile maths

    def _locate(self, lat: float, lon: float) -> tuple[Key, float, float]:
        """Tile and pixel position of a coordinate (Web Mercator)."""
        n = 2**self.zoom
        lat = max(-85.0511, min(85.0511, lat))
        x = (lon + 180.0) / 360.0 * n
        y = (1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n
        tx, ty = int(x) % n, min(n - 1, int(y))
        return (self.zoom, tx, ty), (x - int(x)) * TILE_SIZE, (y - int(y)) * TILE_SIZE

    @staticmethod
    def sample(rgb: bytes, px: float, py: float) -> float:
        """Bilinear interpolation between pixel centres, clamped to the tile."""
        fx = min(max(px - 0.5, 0.0), TILE_SIZE - 1.001)
        fy = min(max(py - 0.5, 0.0), TILE_SIZE - 1.001)
        x0, y0 = int(fx), int(fy)
        dx, dy = fx - x0, fy - y0

        def height(x: int, y: int) -> float:
            i = (y * TILE_SIZE + x) * 3
            return rgb[i] * 256.0 + rgb[i + 1] + rgb[i + 2] / 256.0 - 32768.0

        top = height(x0, y0) * (1 - dx) + height(x0 + 1, y0) * dx
        bottom = height(x0, y0 + 1) * (1 - dx) + height(x0 + 1, y0 + 1) * dx
        return top * (1 - dy) + bottom * dy

    # ------------------------------------------------------------------ tile access

    def _path(self, key: Key) -> Path:
        z, x, y = key
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    @staticmethod
    def _decode(png: bytes) -> bytes:
        with Image.open(io.BytesIO(png)) as image:
            if image.size != (TILE_SIZE, TILE_SIZE):
                raise ValueError(f"unexpected tile size {image.size}")
            return image.convert("RGB").tobytes()

    def _http(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=20, follow_redirects=True, transport=self._transport)
            return self._client

    def _recently_failed(self, key: Key) -> bool:
        failed_at = self._failed.get(key)
        return failed_at is not None and time.monotonic() - failed_at < FAILED_RETRY_SECONDS

    def _download(self, key: Key) -> bytes | None:
        """The PNG of a tile from the cache or the network; None when unavailable."""
        path = self._path(key)
        if path.is_file():
            return path.read_bytes()
        if not self.enabled or self._recently_failed(key):
            return None
        z, x, y = key
        try:
            response = self._http().get(self.url_template.format(z=z, x=x, y=y))
            response.raise_for_status()
            self._decode(response.content)  # never cache something that is not a tile
        except Exception as exc:  # noqa: BLE001 - network and image errors alike mean "no height"
            self._failed[key] = time.monotonic()
            log.warning("Terrain tile %s unavailable: %s", key, exc)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.part")
        partial.write_bytes(response.content)
        partial.replace(path)  # atomic: a crash cannot leave half a tile behind
        return response.content

    def _remember(self, key: Key, rgb: bytes) -> None:
        with self._lock:
            self._tiles[key] = rgb
            self._tiles.move_to_end(key)
            while len(self._tiles) > self.memory_tiles:
                self._tiles.popitem(last=False)

    def _recall(self, key: Key) -> bytes | None:
        with self._lock:
            rgb = self._tiles.get(key)
            if rgb is not None:
                self._tiles.move_to_end(key)
            return rgb

    def _load(self, key: Key, *, network: bool) -> bytes | None:
        rgb = self._recall(key)
        if rgb is not None:
            return rgb
        path = self._path(key)
        png = path.read_bytes() if path.is_file() else (self._download(key) if network else None)
        if png is None:
            return None
        try:
            rgb = self._decode(png)
        except Exception:  # noqa: BLE001 - corrupt cache file: remove it and fetch again next time
            path.unlink(missing_ok=True)
            return None
        self._remember(key, rgb)
        return rgb

    # ------------------------------------------------------------------ public API

    def tile_png(self, z: int, x: int, y: int) -> bytes | None:
        """The raw Terrarium PNG, for the browser's 3D terrain."""
        return self._download((z, x, y))

    def height(self, lat: float, lon: float) -> float | None:
        """Terrain height in metres; downloads the tile if needed (blocks)."""
        key, px, py = self._locate(lat, lon)
        rgb = self._load(key, network=True)
        return None if rgb is None else round(self.sample(rgb, px, py), 1)

    def height_cached(self, lat: float, lon: float) -> float | None:
        """Terrain height without blocking; a missing tile is fetched in the background."""
        key, px, py = self._locate(lat, lon)
        rgb = self._load(key, network=False)
        if rgb is None:
            self._schedule(key)
            return None
        return round(self.sample(rgb, px, py), 1)

    def heights(self, points: list[tuple[float, float]]) -> list[float | None]:
        """Terrain heights for many points (blocks), loading each tile once."""
        out: list[float | None] = [None] * len(points)
        by_tile: dict[Key, list[tuple[int, float, float]]] = {}
        for i, (lat, lon) in enumerate(points):
            key, px, py = self._locate(lat, lon)
            by_tile.setdefault(key, []).append((i, px, py))
        for key, items in by_tile.items():
            rgb = self._load(key, network=True)
            if rgb is not None:
                for i, px, py in items:
                    out[i] = round(self.sample(rgb, px, py), 1)
        return out

    # ------------------------------------------------------------------ background fetching

    def _schedule(self, key: Key) -> None:
        if not self.enabled or self._recently_failed(key):
            return
        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="terrain-fetch", daemon=True)
                self._worker.start()
        self._queue.put(key)

    def _work(self) -> None:
        while True:  # lives as long as the process: an idle daemon thread costs nothing and cannot race
            key = self._queue.get()
            try:
                self._load(key, network=True)
            finally:
                with self._lock:
                    self._queued.discard(key)
