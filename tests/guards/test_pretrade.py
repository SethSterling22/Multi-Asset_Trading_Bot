"""Grupos 9 y 10 de CLAUDE.md §1.8 (distancia a liquidación, cap ajustado por
correlación) y el resto de checks pre-trade contra el risk floor."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from core.ledger.fills import AssetType
from risk.pretrade import (
    ProposedState,
    check_correlation_adjusted_risk,
    check_liquidation_distance,
    check_order,
)

pytestmark = pytest.mark.guards

# Un largo de BTC perp a 1.5x sobre 100k: dentro de todo.
OK = ProposedState(
    symbol="BTC-PERP", asset_type=AssetType.CRYPTO_PERP, equity=D(100_000),
    position_qty=D(1), position_notional=D(20_000), leverage=D("1.5"),
    gross_notional=D(60_000), net_notional=D(40_000), open_positions=3,
    risk_usd=D(800), maintenance_margin=D("0.004"), daily_sigma=D("0.024"),
    position_risks_pct=(D("0.008"), D("0.008"), D("0.008")), mean_correlation=D("0.8"),
)


def test_baseline_is_allowed() -> None:
    assert check_order(OK).ok


# --- Grupo 9: distancia mínima a liquidación ---------------------------------
def test_liquidation_distance_by_hand() -> None:
    # 1.5x: 1/1.5 − 0.004 = 0.6627 >= 8·0.024 = 0.192 -> pasa.
    assert check_liquidation_distance(OK).ok
    # 10x: 1/10 − 0.004 = 0.096 < 0.192 -> rechazo (el apalancamiento lo rechazaría
    # antes; este check es la red independiente por si algún día sube).
    v = check_liquidation_distance(replace(OK, leverage=D(10)))
    assert not v.ok and v.code == "liq_distance"
    # 5x en un alt con sigma 5%: 1/5 − 0.01 = 0.19 < 8·0.05 = 0.40.
    v = check_liquidation_distance(replace(OK, leverage=D(5), maintenance_margin=D("0.01"),
                                           daily_sigma=D("0.05")))
    assert not v.ok


@pytest.mark.parametrize(
    "change",
    [{"maintenance_margin": None}, {"daily_sigma": None}, {"daily_sigma": D(0)},
     {"leverage": D(0)}],
)
def test_liquidation_distance_without_data_fails_closed(change: dict[str, object]) -> None:
    v = check_liquidation_distance(replace(OK, **change))
    assert not v.ok and v.code == "liq_distance_unknown"


def test_spot_has_no_liquidation() -> None:
    spot = replace(OK, asset_type=AssetType.CRYPTO_SPOT, leverage=D(1),
                   maintenance_margin=None, daily_sigma=None)
    assert check_liquidation_distance(spot).ok


# --- Grupo 10: cap de exposición ajustado por correlación ---------------------
def test_correlation_adjusted_risk_by_hand() -> None:
    # 8 posiciones al 1% con rho 0.8: 0.01·√(1 + 7·0.8) = 2.57% <= 4%.
    eight = replace(OK, position_risks_pct=(D("0.01"),) * 8)
    assert check_correlation_adjusted_risk(eight).ok
    # 8 al 1.6% con rho 0.95: 0.016·√(1 + 7·0.95) = 4.43% > 4%.
    hot = replace(eight, position_risks_pct=(D("0.016"),) * 8, mean_correlation=D("0.95"))
    v = check_correlation_adjusted_risk(hot)
    assert not v.ok and v.code == "correlation_adjusted_risk"


def test_unknown_correlation_assumes_stress() -> None:
    # 8 al 1.5%: con rho medido 0.5 -> 0.015·√4.5 = 3.18% (pasa); sin medir se
    # asume 0.95 -> 0.015·√7.65 = 4.15% (no pasa). No saber no sale más barato.
    measured = replace(OK, position_risks_pct=(D("0.015"),) * 8, mean_correlation=D("0.5"))
    assert check_correlation_adjusted_risk(measured).ok
    assert not check_correlation_adjusted_risk(replace(measured, mean_correlation=None)).ok


def test_no_positions_no_portfolio_risk() -> None:
    assert check_correlation_adjusted_risk(replace(OK, position_risks_pct=())).ok


# --- El resto del floor --------------------------------------------------------
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"equity": D(0)}, "no_equity"),
        ({"leverage": D("1.6")}, "leverage"),
        ({"symbol": "SOL-PERP", "leverage": D("1.1")}, "leverage"),
        ({"symbol": "SOL-USD", "asset_type": AssetType.CRYPTO_SPOT, "leverage": D(1),
          "position_qty": D(-1), "risk_usd": None}, "alt_short"),
        ({"position_notional": D("25000.01")}, "position_notional"),
        ({"gross_notional": D(160_001)}, "gross_exposure"),
        ({"net_notional": D(-100_001)}, "net_exposure"),
        ({"open_positions": 9}, "open_positions"),
        ({"risk_usd": D(1_001)}, "risk_per_trade"),
        ({"risk_usd": None}, "no_stop"),
    ],
)
def test_each_floor_limit_rejects(change: dict[str, object], code: str) -> None:
    v = check_order(replace(OK, **change))
    assert not v.ok and v.code == code  # type: ignore[union-attr]


def test_limits_are_inclusive() -> None:
    edge = replace(OK, leverage=D("1.5"), position_notional=D(25_000), gross_notional=D(160_000),
                   net_notional=D(100_000), open_positions=8, risk_usd=D(1_000))
    assert check_order(edge).ok


def test_btc_short_is_allowed_alt_short_is_not() -> None:
    assert check_order(replace(OK, position_qty=D(-1))).ok


def test_stopless_spot_is_allowed() -> None:
    spot = replace(OK, symbol="ETH-USD", asset_type=AssetType.CRYPTO_SPOT, leverage=D(1),
                   risk_usd=None, maintenance_margin=None, daily_sigma=None)
    assert check_order(spot).ok
