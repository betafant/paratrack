"""The documentation matches the program: every setting is in the README and in .env.example."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = (ROOT / "prack" / "config.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")


def settings_read_by_the_program() -> set[str]:
    names = set(re.findall(r'\bget\("([A-Z_]+)"', CONFIG))
    names |= set(re.findall(r'"PRACK_([A-Z_]+)"', CONFIG))
    return {f"PRACK_{n}" for n in names}


def test_the_program_reads_the_settings_this_test_expects():
    found = settings_read_by_the_program()
    assert len(found) >= 20 and {"PRACK_REGIONS", "PRACK_AUTH_USER", "PRACK_DATABASE_URL", "PRACK_PORT"} <= found


def test_every_setting_is_in_the_readme_table():
    missing = sorted(n for n in settings_read_by_the_program() if f"`{n}`" not in README)
    assert missing == [], f"not in the README configuration table: {missing}"


def test_every_setting_is_in_env_example():
    missing = sorted(n for n in settings_read_by_the_program() if n not in ENV_EXAMPLE)
    assert missing == [], f"not in .env.example: {missing}"


def test_the_compose_variables_are_documented():
    for name in ("PRACK_PUBLISH_PORT", "PRACK_DOMAIN"):
        assert name in ENV_EXAMPLE and name in README, name


def test_the_readme_links_point_at_files_that_exist():
    for target in re.findall(r"\]\(((?!https?:|#)[^)\s]+)\)", README):
        assert (ROOT / target).exists(), f"README links to {target}, which does not exist"
