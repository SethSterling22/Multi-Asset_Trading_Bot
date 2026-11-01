"""Atribución de P&L al centavo, con la convención de signo de CLAUDE.md §3.4."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

from core.ledger.attribution import (
    DecisionUse,
    LlmCallCost,
    allocate_ai_cost,
    pnl_by_strategy_day,
    slippage,
)
from core.ledger.fills import Side
from tests.ledger.fixtures import fill

DAY1 = date(2026, 9, 1)
DAY2 = date(2026, 9, 2)

# Día 1, trend, un round-trip con slippage adverso en las dos patas y funding cobrado:
#   compra 1 @ 101 (decisión 100): slippage (101−100)·1 = 1.00, comisión 0.10
#   venta  1 @ 109 (decisión 110): slippage (110−109)·1 = 1.00, comisión 0.11
#   funding +0.50 (cobrado)
#   realizado a precios de fill = 109 − 101 = 8
#   gross   = realizado + slippage = 8 + 2 = 10  (== 110 − 100, a precios de decisión)
#   neto    = 10 − 0.21 + 0.50 − 2.00 = 8.29     (== 8 − 0.21 + 0.50)
DAY1_FILLS = [
    fill(1, Side.BUY, "1", "101", decision_price=D(100), commission=D("0.10")),
    fill(2, Side.SELL, "1", "109", decision_price=D(110), commission=D("0.11")),
    fill(3, Side.SELL, "0", "109", funding_payment=D("0.50"), venue_fill_id="fund-1"),
]
# Día 2: compra MEJOR que la decisión -> slippage negativo, visible.
#   compra 1 @ 99 (decisión 100): slippage (99−100)·1 = −1.00, comisión 0.10
#   gross = 0 + (−1) = −1; neto = −1 − 0.10 + 0 − (−1) = −0.10
DAY2_FILLS = [
    fill(30, Side.BUY, "1", "99", decision_price=D(100), commission=D("0.10")),
]


def test_slippage_sign_convention() -> None:
    buy_worse, sell_worse, *_ = DAY1_FILLS
    assert slippage(buy_worse) == D("1")
    assert slippage(sell_worse) == D("1")
    assert slippage(DAY2_FILLS[0]) == D("-1")
    assert slippage(DAY1_FILLS[2]) == 0          # funding: qty 0
    assert slippage(fill(40, Side.BUY, "1", "100")) == 0   # sin decision_price


def test_daily_pnl_to_the_cent() -> None:
    day1, day2 = pnl_by_strategy_day([*DAY1_FILLS, *DAY2_FILLS])
    assert (day1.strategy_id, day1.day) == ("trend", DAY1)
    assert day1.gross_pnl == D("10")
    assert day1.fees == D("0.21")
    assert day1.funding == D("0.50")
    assert day1.slippage == D("2")
    assert day1.net_pnl == D("8.29")
    assert day1.n_fills == 2                     # la fila de funding no cuenta
    assert day2.day == DAY2
    assert day2.slippage == D("-1")
    assert day2.net_pnl == D("-0.10")


def test_net_equals_realized_minus_fees_plus_funding() -> None:
    # Si el slippage se restase dos veces, el neto del día 1 sería 6.29.
    (day1,) = pnl_by_strategy_day(DAY1_FILLS)
    realized = D(8)
    assert day1.net_pnl == realized - day1.fees + day1.funding


def test_day_is_utc() -> None:
    # 23:30 en Puerto Rico (UTC−4) es ya el día siguiente en UTC.
    pr = timezone(timedelta(hours=-4))
    late = fill(50, Side.BUY, "1", "100", ts=datetime(2026, 9, 1, 23, 30, tzinfo=pr))
    (row,) = pnl_by_strategy_day([late])
    assert row.day == DAY2


def test_ai_cost_split_equally_between_decisions() -> None:
    # Llamada A: $0.30 usada por 3 decisiones (2 trend, 1 wheel) -> 0.10 cada una.
    # Llamada B: $0.05 que ninguna decisión usó -> no atribuida, pero cuenta.
    a, b = uuid4(), uuid4()
    calls = {
        a: LlmCallCost(D("0.30"), datetime(2026, 9, 1, 10, tzinfo=UTC)),
        b: LlmCallCost(D("0.05"), datetime(2026, 9, 1, 11, tzinfo=UTC)),
    }
    uses = [
        DecisionUse(uuid4(), a, "trend"),
        DecisionUse(uuid4(), a, "trend"),
        DecisionUse(uuid4(), a, "wheel"),
    ]
    by_day, unattributed = allocate_ai_cost(calls, uses)
    assert by_day == {("trend", DAY1): D("0.20"), ("wheel", DAY1): D("0.10")}
    assert unattributed == D("0.05")
    assert sum(by_day.values()) + unattributed == D("0.35")

    (day1,) = pnl_by_strategy_day(DAY1_FILLS, ai_cost=by_day)
    assert day1.ai_cost == D("0.20")
    assert day1.net_pnl_after_ai == D("8.09")


def test_ai_split_of_indivisible_cost_still_sums_exactly() -> None:
    a = uuid4()
    calls = {a: LlmCallCost(D("0.01"), datetime(2026, 9, 1, tzinfo=UTC))}
    by_day, _ = allocate_ai_cost(calls, [DecisionUse(uuid4(), a, "trend") for _ in range(3)])
    assert by_day[("trend", DAY1)] == D("0.01")
