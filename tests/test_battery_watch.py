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
    qm_log = tmp_path / "qm.log"
    qm = tmp_path / "qm"
    qm.write_text(f'#!/bin/sh\necho "$@" >> {qm_log}\n')
    qm.chmod(0o755)
    conf = tmp_path / "battery-watch.conf"
    conf.write_text("VMID=101\n")
    return {"ps": ps, "qm": qm, "qm_log": qm_log, "state": tmp_path / "state", "conf": conf}


def power(host: dict[str, Path], *, mains: bool, pct: int) -> None:
    (host["ps"] / "ADP1" / "online").write_text("1\n" if mains else "0\n")
    (host["ps"] / "AP21A7T" / "capacity").write_text(f"{pct}\n")
    (host["ps"] / "AP21A7T" / "status").write_text("Full\n" if mains else "Discharging\n")


def tick(host: dict[str, Path], now: int) -> str:
    env = {**os.environ, "CONF": str(host["conf"]), "POWER_SUPPLY_DIR": str(host["ps"]),
           "QM": str(host["qm"]), "STATE_DIR": str(host["state"]), "NOW": str(now)}
    out = subprocess.run(["bash", str(SCRIPT), "--once"], env=env, capture_output=True,  # noqa: S603, S607
                         text=True, check=True)
    return out.stdout


def qm_calls(host: dict[str, Path]) -> list[str]:
    log = host["qm_log"]
    return log.read_text().splitlines() if log.exists() else []


def test_on_mains_does_nothing(host: dict[str, Path]) -> None:
    power(host, mains=True, pct=100)
    tick(host, T0)
    assert qm_calls(host) == []


def test_a_short_blip_is_ignored(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=99)
    tick(host, T0)
    tick(host, T0 + 60)                      # < 120 s
    power(host, mains=True, pct=99)
    assert "restablecida" in tick(host, T0 + 70)
    assert qm_calls(host) == []


def test_two_minutes_on_battery_activates_the_kill_switch_once(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=90)
    tick(host, T0)
    tick(host, T0 + 120)
    tick(host, T0 + 180)
    calls = qm_calls(host)
    assert len(calls) == 1 and calls[0].startswith("guest exec 101 --")
    assert "/run/trading/HALT" in calls[0]


def test_low_battery_shuts_the_vm_down_cleanly_once(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=24)
    tick(host, T0)
    tick(host, T0 + 30)
    assert qm_calls(host).count("shutdown 101 --timeout 180") == 1


def test_power_back_does_not_lift_the_kill_switch(host: dict[str, Path]) -> None:
    power(host, mains=False, pct=80)
    tick(host, T0)
    tick(host, T0 + 200)
    power(host, mains=True, pct=80)
    out = tick(host, T0 + 260)
    assert "NO se levanta" in out
    assert all("rm" not in c for c in qm_calls(host))


def test_without_vmid_it_refuses_to_run(host: dict[str, Path]) -> None:
    host["conf"].write_text("")
    with pytest.raises(subprocess.CalledProcessError):
        tick(host, T0)
