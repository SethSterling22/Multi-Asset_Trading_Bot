"""core/metrics.py contra valores calculados a mano."""

from __future__ import annotations

import math

import pytest

from core.metrics import (
    DAILY_EQUITIES,
    equity_metrics,
    max_drawdown,
    returns,
    sharpe,
    sortino,
    trade_stats,
)

# Retornos: +10%, −10%, +10%.  media = 1/30;  varianza muestral = 1/75.
# Sharpe  = (1/30)·√75·√252  = √18900 / 30 = 4.58258
# Sortino = (1/30)/√(0.01/3)·√252 = √75600 / 30 = 9.16515
EQUITY = [100.0, 110.0, 99.0, 108.9]


def test_returns() -> None:
    assert returns(EQUITY) == pytest.approx([0.1, -0.1, 0.1])


def test_sharpe_and_sortino_by_hand() -> None:
    rets = returns(EQUITY)
    assert sharpe(rets, DAILY_EQUITIES) == pytest.approx(math.sqrt(18900) / 30)
    assert sortino(rets, DAILY_EQUITIES) == pytest.approx(math.sqrt(75600) / 30)


def test_annualisation_is_explicit() -> None:
    rets = returns(EQUITY)
    s252, s365 = sharpe(rets, 252), sharpe(rets, 365)
    assert s252 is not None and s365 is not None
    assert s365 / s252 == pytest.approx(math.sqrt(365 / 252))


def test_max_drawdown() -> None:
    assert max_drawdown(EQUITY) == pytest.approx(99 / 110 - 1)
    assert max_drawdown([1.0, 2.0, 3.0]) == 0.0


def test_undefined_metrics_are_none_not_zero() -> None:
    assert sharpe([0.01, 0.01, 0.01], 365) is None
    assert sharpe([0.01], 365) is None
    assert sortino([0.01, 0.02], 365) is None          # sin retornos negativos
    m = equity_metrics([100.0, 101.0, 102.0], 365)
    assert m.calmar is None                             # sin drawdown


def test_trade_stats_by_hand() -> None:
    # pnl 100, −50, 30, −20 con 50 arriesgados en cada una:
    # R = 2, −1, 0.6, −0.4 -> media 0.3;  win rate 0.5;  expectancy 15
    s = trade_stats([100, -50, 30, -20], [50, 50, 50, 50])
    assert s.n == 4
    assert s.win_rate == 0.5
    assert s.avg_r == pytest.approx(0.3)
    assert s.expectancy_usd == pytest.approx(15)
    assert s.avg_win_usd == pytest.approx(65)
    assert s.avg_loss_usd == pytest.approx(-35)


def test_trade_stats_rejects_non_positive_risk() -> None:
    with pytest.raises(ValueError):
        trade_stats([10], [0])
