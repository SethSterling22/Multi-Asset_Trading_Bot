"""La cuenta propia del Wheel (D-016): su caja es la que alimenta el check de put
cubierta con caja, así que no puede incluir dinero de otro sleeve ni dinero que la
cuenta del broker no tiene."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from core.greeks import Right
from core.ledger.equity import MissingMarkError
from core.ledger.fills import AssetType
from strategies.wheel.accounting import wheel_book
from strategies.wheel.logic import OptionContract
from tests.ledger.fixtures import OPTION, TRACED

pytestmark = pytest.mark.guards
NOW = datetime(2026, 9, 2, tzinfo=UTC)
BUDGET = D(90000)
PUT = OPTION[0]                 # sell_to_open 1 SPY put a 2.50, multiplier 100, comisión 0.65


def no_marks(symbol: str, asset_type: AssetType) -> D | None:
    return None


def test_cash_is_budget_plus_its_own_fills_capped_by_the_account() -> None:
    # Cobra la prima: 90 000 + 250 − 0.65. Los fills de 'trend' (otro sleeve) no cuentan.
    book = wheel_book([PUT, *TRACED], budget=BUDGET, account_cash=D(10**6),
                      mark=lambda s, t: D("1.20"), ts=NOW)
    assert book.cash == BUDGET + 250 - D("0.65")
    # La cuenta tiene menos que el presupuesto (la cripto se llevó parte): manda la cuenta.
    capped = wheel_book([PUT], budget=BUDGET, account_cash=D(5000),
                        mark=lambda s, t: D("1.20"), ts=NOW)
    assert capped.cash == D(5000)


def test_equity_marks_the_short_put() -> None:
    book = wheel_book([PUT], budget=BUDGET, account_cash=D(10**6),
                      mark=lambda s, t: D("1.20"), ts=NOW)
    # Corta 1 a 2.50, marca 1.20: no realizado = −1·(1.20 − 2.50)·100 = +130.
    assert book.snapshot.unrealized_pnl == 130
    assert book.snapshot.equity_usd == BUDGET + 130 - D("0.65")
    assert book.snapshot.allocated_usd == BUDGET and book.snapshot.strategy_id == "wheel"
    ((venue, symbol), (qty, mark)), = book.positions.items()
    assert (venue, symbol, qty, mark) == ("alpaca", PUT.symbol, -1, D("1.20"))


def test_flat_wheel_is_its_budget() -> None:
    book = wheel_book([], budget=BUDGET, account_cash=D(10**6), mark=no_marks, ts=NOW)
    assert book.snapshot.equity_usd == BUDGET == book.cash
    # Cerrada (put vendida y recomprada): +200 − 1.30 sin necesitar precio de nada.
    closed = wheel_book(OPTION, budget=BUDGET, account_cash=D(10**6), mark=no_marks, ts=NOW)
    assert closed.snapshot.equity_usd == BUDGET + 200 - D("1.30") and closed.positions == {}


def test_open_position_without_a_price_is_not_estimated() -> None:
    with pytest.raises(MissingMarkError, match=PUT.symbol):
        wheel_book([PUT], budget=BUDGET, account_cash=D(10**6), mark=no_marks, ts=NOW)


def test_fills_after_the_snapshot_time_do_not_count() -> None:
    later = replace(PUT, ts=datetime(2026, 9, 3, tzinfo=UTC))
    book = wheel_book([later], budget=BUDGET, account_cash=D(10**6), mark=no_marks, ts=NOW)
    assert book.cash == BUDGET


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="WHEEL_BUDGET_USD"):
        wheel_book([], budget=D(0), account_cash=D(1), mark=no_marks, ts=NOW)


@pytest.mark.parametrize("symbol", ["SPY261218P00550000", "AAPL270115C00187500",
                                    "BRK.B261218P00400000"])
def test_occ_symbol_round_trip(symbol: str) -> None:
    assert OptionContract.from_occ(symbol).occ_symbol == symbol


def test_occ_symbol_parts_and_garbage() -> None:
    c = OptionContract.from_occ("AAPL270115C00187500")
    assert (c.underlying, c.expiration, c.strike, c.right) == (
        "AAPL", date(2027, 1, 15), D("187.5"), Right.CALL)
    with pytest.raises(ValueError, match="OCC"):
        OptionContract.from_occ("BTC/USD")
