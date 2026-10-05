from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from prack.config import Settings


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# Midday on the day the golden lines were recorded: close enough to every golden time stamp
# that the parser picks the same calendar day.
REF = utc(2026, 7, 15, 12, 0, 0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.ddb_enabled = False
    return s


# ---------------------------------------------------------------- browser tests (tests/test_ui*.py)


@pytest.fixture(scope="session")
def chromium():
    from .uirig import browser

    with browser() as b:
        yield b


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """The app served on a port, with a made-up feed; one for all browser tests."""
    from .uirig import live_app

    with live_app(tmp_path_factory.mktemp("ui")) as url:
        yield url


@pytest.fixture
def make_page(chromium, app):
    """make(path, theme=, size=, touch=) opens a page in a fresh browser context; problems fail the test at the end."""
    from .uirig import Console, new_context

    contexts, consoles = [], []

    def make(url_path: str = "/", **options):
        context = new_context(chromium, app, **options)
        contexts.append(context)
        console = Console(app)
        consoles.append(console)
        page = context.new_page()
        console.attach(page)
        page.goto(app + url_path)
        return page

    yield make
    for context in contexts:
        context.close()
    problems = [p for c in consoles for p in c.problems]
    assert not problems, "the page reported problems:\n" + "\n".join(problems)
