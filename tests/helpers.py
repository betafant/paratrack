"""Shared test helpers: synthetic terrain tiles and a tiny flight scenario runner."""

from __future__ import annotations

import io
from collections.abc import Callable

from PIL import Image

TILE = 256


def terrarium_png(height: Callable[[int, int], float]) -> bytes:
    """A Terrarium tile whose pixel (x, y) has the height given by ``height(x, y)`` in metres."""
    image = Image.new("RGB", (TILE, TILE))
    pixels = image.load()
    for y in range(TILE):
        for x in range(TILE):
            v = height(x, y) + 32768.0
            r = int(v // 256)
            g = int(v % 256)
            b = int((v - int(v)) * 256)
            pixels[x, y] = (r, g, b)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
