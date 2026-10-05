"""The deployment files: the Dockerfile, the compose file, the Caddyfile and the systemd unit are what they claim."""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
UNIT = ROOT / "deploy" / "prack.service"


def test_the_dockerfile_runs_prack_as_an_unprivileged_user_with_its_data_in_a_volume():
    assert re.search(r"^FROM python:3\.(1[1-9]|[2-9]\d)-slim AS build", DOCKERFILE, re.M)  # prack needs Python >= 3.11
    assert re.search(r"^USER prack$", DOCKERFILE, re.M) and "useradd --system" in DOCKERFILE
    assert "PRACK_DATA_DIR=/data" in DOCKERFILE and re.search(r"^VOLUME /data$", DOCKERFILE, re.M)
    assert "PRACK_HOST=0.0.0.0" in DOCKERFILE  # inside a container the port mapping decides who can connect
    assert re.search(r"^EXPOSE 8000$", DOCKERFILE, re.M)
    assert 'ENTRYPOINT ["prack"]' in DOCKERFILE and 'CMD ["run"]' in DOCKERFILE
    health = re.search(r"^HEALTHCHECK .*\n\s+CMD (.*)$", DOCKERFILE, re.M)
    assert health and "/api/health" in health.group(1)  # the one path that never asks for a password
    assert ".[postgres]" in DOCKERFILE


def test_the_docker_context_leaves_out_what_does_not_belong_in_an_image():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for name in (".git", ".venv", "data", ".env", "tests", "*.db"):
        assert name in ignored, name


def test_compose_publishes_only_on_this_machine_and_keeps_data_in_a_volume():
    assert re.search(r'"127\.0\.0\.1:\$\{PRACK_PUBLISH_PORT:-8000\}:8000"', COMPOSE)
    assert "prack-data:/data" in COMPOSE and "read_only: true" in COMPOSE and "cap_drop" in COMPOSE
    assert "restart: unless-stopped" in COMPOSE
    assert "profiles" in COMPOSE and "caddy" in COMPOSE  # HTTPS is opt-in
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert (
        "reverse_proxy prack:8000" in caddyfile and "flush_interval -1" in caddyfile
    )  # the live stream is not buffered


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
@pytest.mark.parametrize("profile", [[], ["--profile", "https"]])
def test_docker_compose_accepts_the_compose_file(profile):
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), *profile, "config", "-q"],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )  # fmt: skip
    if "docker compose" in result.stderr and "is not a docker command" in result.stderr:
        pytest.skip("the compose plugin is not installed")
    assert result.returncode == 0, result.stderr


def unit_sections() -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case sensitive
    parser.read(UNIT, encoding="utf-8")
    return {name: dict(parser[name]) for name in parser.sections()}


def test_the_systemd_unit_runs_prack_as_its_own_user_with_a_restart_and_a_hardened_sandbox():
    unit = unit_sections()
    service = unit["Service"]
    assert service["ExecStart"] == "/opt/prack/venv/bin/prack run"
    assert service["User"] == "prack" and service["Restart"] == "on-failure"
    assert service["EnvironmentFile"] == "-/etc/prack/prack.env"  # optional: the dash
    assert service["Environment"] == "PRACK_DATA_DIR=/var/lib/prack" and service["WorkingDirectory"] == "/var/lib/prack"
    assert service["ReadWritePaths"] == "/var/lib/prack" and service["ProtectSystem"] == "strict"
    for key in ("NoNewPrivileges", "PrivateTmp", "ProtectHome", "ProtectKernelTunables", "LockPersonality"):
        assert service[key] == "true", key
    assert unit["Install"]["WantedBy"] == "multi-user.target"
    assert "network-online.target" in unit["Unit"]["After"]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze is not installed")
def test_systemd_analyze_finds_nothing_to_complain_about(tmp_path):
    """Run the unit through ``systemd-analyze verify`` with the user, the program and the directory swapped for ones
    that exist here; every key and value of the real unit is still checked."""
    text = UNIT.read_text(encoding="utf-8")
    text = re.sub(r"^(User|Group)=prack$", r"\1=root", text, flags=re.M)
    text = re.sub(r"^ExecStart=.*$", "ExecStart=/bin/true", text, flags=re.M)
    text = text.replace("/var/lib/prack", str(tmp_path))
    copy = tmp_path / "prack.service"
    copy.write_text(text, encoding="utf-8")
    result = subprocess.run(["systemd-analyze", "verify", str(copy)], capture_output=True, text=True, timeout=60)
    assert (result.stdout + result.stderr).strip() == "", result.stdout + result.stderr
