"""The front-end's pure logic (store, track, trails, URL state, label placement, formatting) under `node --test`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = sorted((Path(__file__).parent / "js").glob("*.test.mjs"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_unit_tests_pass():
    result = subprocess.run(
        ["node", "--test", *map(str, JS_TESTS)], capture_output=True, text=True, timeout=120, encoding="utf-8"
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
