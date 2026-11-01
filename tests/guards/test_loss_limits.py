"""Grupo 3 de CLAUDE.md §1.8: límite de pérdida diaria (y semanal)."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from risk.breakers import loss_limits
from strategies.base import StrategyBase
from tests.guards.helpers import Events, FixedLossReference, setup, write_config

pytestmark = pytest.mark.guards


class Sleeve(StrategyBase):
    strategy_id = "trend"


def test_daily_loss_at_the_floor_stops_trading() -> None:
    # 100k -> 97k = −3.0%: el límite es ">=", así que exactamente el floor ya para.
    v = loss_limits(equity=D(97_000), day_start_equity=D(100_000), week_start_equity=D(100_000))
    assert not v.ok and v.code == "daily_loss"
    assert loss_limits(equity=D("97000.01"), day_start_equity=D(100_000),
                       week_start_equity=D(100_000)).ok


def test_config_can_tighten_but_not_loosen_the_daily_limit() -> None:
    tight = loss_limits(equity=D(97_500), day_start_equity=D(100_000),
                        week_start_equity=D(100_000), daily_limit=0.02)
    assert not tight.ok and tight.code == "daily_loss"
    # 0.10 en config no afloja nada: sigue mandando el 3% del floor.
    loose = loss_limits(equity=D(96_000), day_start_equity=D(100_000),
                        week_start_equity=D(100_000), daily_limit=0.10)
    assert not loose.ok and loose.code == "daily_loss"


def test_weekly_loss() -> None:
    # Día −1% pero semana 107k -> 99k = −7.5%.
    v = loss_limits(equity=D(99_000), day_start_equity=D(100_000), week_start_equity=D(107_000))
    assert not v.ok and v.code == "weekly_loss"


@pytest.mark.parametrize(
    ("day", "week"),
    [(None, D(100_000)), (D(100_000), None), (D(0), D(100_000)), (D(100_000), D(-1))],
)
def test_unknown_reference_fails_closed(day: D | None, week: D | None) -> None:
    # El bug del Wheel viejo: sin equity de inicio de sesión devolvía "no superado".
    v = loss_limits(equity=D(100_000), day_start_equity=day, week_start_equity=week)
    assert not v.ok and v.code == "loss_limit_unknown"


def test_gate_uses_persisted_reference(tmp_path: Path) -> None:
    events = Events()
    s = setup(Sleeve(), write_config(tmp_path), loss=FixedLossReference(day=D(100_000)))
    assert not s.trading_gate(equity=D(96_000)).ok
    assert [e["reason"] for e in events.rejections()] == ["daily_loss"]
    assert s.trading_gate(equity=D(99_000)).ok


def test_gate_uses_config_daily_limit(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, **{"risk_guards.daily_loss_limit_percentage": 0.01})
    s = setup(Sleeve(), cfg)
    assert not s.trading_gate(equity=D(98_900)).ok
