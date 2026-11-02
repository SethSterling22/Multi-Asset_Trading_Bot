"""ops/power/battery-watch.sh con un sysfs y un `qm` falsos: sin hardware ni Proxmox."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "power" / "battery-watch.sh"
T0 = 1_800_000_000


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Path]:
    ps = tmp_path / "power_supply"
    (ps / "ADP1").mkdir(parents=True)
    (ps / "ADP1" / "type").write_text("Mains\n")
    (ps / "AP21A7T").mkdir()
    (ps / "AP21A7T" / "type").write_text("Battery\n")
    docker_log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" >> {docker_log}\n')
    docker.chmod(0o755)
    conf = tmp_path / "battery-watch.conf"
    conf.write_text("COMPOSE_FILE=/opt/trading/docker/compose.runtime.yml\n")
    return {"ps": ps, "docker": docker, "docker_log": docker_log, "halt": tmp_path / "run",
            "state": tmp_path / "state", "conf": conf}


def power(host: dict[str, Path], *, mains: bool, pct: int) -> None:
    (host["ps"] / "ADP1" / "online").write_text("1\n" if mains else "0\n")
    (host["ps"] / "AP21A7T" / "capacity").write_text(f"{pct}\n")
    (host["ps"] / "AP21A7T" / "status").write_text("Full\n" if mains else "Discharging\n")


def tick(host: dict[str, Path], now: int) -> str:
    env = {**os.environ, "CONF": str(host["conf"]), "POWER_SUPPLY_DIR": str(host["ps"]),
           "DOCKER": str(host["docker"]), "HALT_DIR": str(host["halt"]),
           "STATE_DIR": str(host["state"]), "NOW": str(now)}
    out = subprocess.run(["bash", str(SCRIPT), "--once"], env=env, capture_output=True,  # noqa: S603, S607
                         text=True, check=True)
    return out.stdout


def docker_calls(host: dict[str, Path]) -> list[str]:
    log = host["docker_log"]
    return log.read_text().splitlines() if log.exists() else []


def halted(host: dict[str, Path]) -> bool:
    return (host["halt"] / "HALT").exists()


def test_on_mains_does_nothing(host: dict[str, Path]) -> None:
    power(host, mains=True, pct=100)
    tick(host, T0)
    assert docker_calls(host) == [] and not halted(host)


def test_a_short_blip_is_ignored(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=99)
    tick(host, T0)
    tick(host, T0 + 60)                      # < 120 s
    power(host, mains=True, pct=99)
    assert "restablecida" in tick(host, T0 + 70)
    assert docker_calls(host) == [] and not halted(host)


def test_two_minutes_on_battery_activates_the_kill_switch_once(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=90)
    tick(host, T0)
    assert not halted(host)
    out = tick(host, T0 + 120)
    assert halted(host) and "kill switch activado" in out
    assert "kill switch activado" not in tick(host, T0 + 180)       # una sola vez
    assert docker_calls(host) == []                                  # parar no es cerrar


def test_low_battery_stops_the_containers_cleanly_once(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=24)
    tick(host, T0)
    tick(host, T0 + 30)
    stops = [c for c in docker_calls(host) if c.endswith(" stop")]
    assert stops == ["compose -f /opt/trading/docker/compose.runtime.yml"
                     " --env-file /etc/trading/compose.env stop"]


def test_power_back_does_not_lift_the_kill_switch(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=80)
    tick(host, T0)
    tick(host, T0 + 200)
    power(host, mains=True, pct=80)
    out = tick(host, T0 + 260)
    assert "NO se levanta" in out and halted(host)


def test_without_compose_file_it_refuses_to_run(host: dict[str, Path]) -> None:
    host["conf"].write_text("")
    with pytest.raises(subprocess.CalledProcessError):
        tick(host, T0)
