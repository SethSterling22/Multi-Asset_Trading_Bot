"""Los 8 circuit breakers del informe §12.4 (patrón 6)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest

from core.risk_floor import MAX_CONSECUTIVE_LOSSES, MAX_ORDERS_PER_DAY, MAX_ORDERS_PER_MINUTE
from risk.breakers import (
    consecutive_losses,
    extreme_spread,
    order_rate,
    reconciliation_mismatch,
    rejections,
    stale_data,
    venue_error_rate,
)
from tests.guards.helpers import NOW

pytestmark = pytest.mark.guards
S = timedelta(seconds=1)


def test_venue_error_rate() -> None:
    assert venue_error_rate([], NOW).ok
    # 1 de 5 = 20%: no supera; 2 de 5 = 40%: abre.
    assert venue_error_rate([(NOW - i * S, i != 0) for i in range(5)], NOW).ok
    v = venue_error_rate([(NOW - i * S, i > 1) for i in range(5)], NOW)
    assert not v.ok and v.code == "venue_error_rate"
    # Los errores de hace más de 60 s no cuentan.
    assert venue_error_rate([(NOW - 120 * S, False), (NOW, True)], NOW).ok


def test_stale_data_by_age_of_last_message() -> None:
    ttl = 30 * S
    assert stale_data(NOW - 29 * S, ttl, NOW).ok
    assert not stale_data(NOW - 31 * S, ttl, NOW).ok
    v = stale_data(None, ttl, NOW)
    assert not v.ok and "nunca" in v.detail


def test_consecutive_losses() -> None:
    n = MAX_CONSECUTIVE_LOSSES
    assert consecutive_losses([D(-1)] * (n - 1)).ok
    assert not consecutive_losses([D(5)] + [D(-1)] * n).ok
    assert consecutive_losses([D(-1)] * n + [D(0)]).ok      # un cierre a 0 corta la racha
    assert consecutive_losses([]).ok


def test_order_rate_counts_the_next_order() -> None:
    per_min = [NOW - i * S for i in range(MAX_ORDERS_PER_MINUTE - 1)]
    assert order_rate(per_min, NOW).ok
    v = order_rate([*per_min, NOW - 30 * S], NOW)
    assert not v.ok and "1 min" in v.detail
    spread_day = [NOW - timedelta(minutes=10 * i) for i in range(MAX_ORDERS_PER_DAY)]
    v = order_rate(spread_day, NOW + timedelta(minutes=5))
    assert not v.ok and "24 h" in v.detail
    old = [NOW - timedelta(days=2)] * 500
    assert order_rate(old, NOW).ok


def test_rejections() -> None:
    assert rejections(["FILLED", "REJECTED", "REJECTED"], 3).ok
    assert not rejections(["REJECTED"] * 3, 3).ok
    assert rejections(["REJECTED", "REJECTED", "FILLED"], 2).ok
    with pytest.raises(ValueError):
        rejections([], 0)


def test_extreme_spread() -> None:
    assert extreme_spread(D(5), D(50), max_spread_to_atr=D("0.2")).ok
    assert not extreme_spread(D(15), D(50), max_spread_to_atr=D("0.2")).ok
    for spread, atr in ((None, D(50)), (D(5), None), (D(5), D(0))):
        assert not extreme_spread(spread, atr, max_spread_to_atr=D("0.2")).ok


def test_reconciliation_mismatch() -> None:
    assert reconciliation_mismatch(0).ok
    assert not reconciliation_mismatch(1).ok
