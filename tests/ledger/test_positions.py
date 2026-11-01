"""La posición y el P&L realizado se derivan SOLO de fills (CLAUDE.md §1.3)."""

from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal as D

import pytest

from core.ledger.fills import Side
from core.ledger.positions import PositionKey, reconstruct, replay
from tests.ledger.fixtures import FUNDING, OPTION, TRACED, TRACED_REALIZED, at, fill


def test_hand_traced_sequence() -> None:
    positions, per_fill = replay(TRACED)
    assert [fp.realized_pnl for fp in per_fill] == TRACED_REALIZED
    (pos,) = positions.values()
    assert pos.net_qty == D(-1)
    assert pos.avg_cost == D(90)
    assert pos.realized_pnl == D(10)
    assert pos.net_cost == D(-100)
    assert pos.unrealized_pnl(D(85)) == D(5)


def test_total_pnl_matches_cash_identity() -> None:
    # Comprobación independiente del método de coste: P&L total =
    # −(caja neta invertida) + valor de la posición a mercado.
    (pos,) = reconstruct(TRACED).values()
    mark = D(85)
    assert pos.realized_pnl + pos.unrealized_pnl(mark) == -pos.net_cost + pos.net_qty * mark


def test_option_uses_multiplier_and_closes() -> None:
    positions, _ = replay(OPTION)
    (pos,) = positions.values()
    assert pos.net_qty == 0
    assert pos.realized_pnl == D(200)
    assert pos.total_commission == D("1.30")
    assert reconstruct(OPTION) == {}


def test_funding_row_does_not_move_position() -> None:
    without = reconstruct(TRACED)
    with_funding = reconstruct([*TRACED, FUNDING])
    (a,), (b,) = without.values(), with_funding.values()
    assert (b.net_qty, b.avg_cost, b.realized_pnl) == (a.net_qty, a.avg_cost, a.realized_pnl)
    assert b.total_funding == D("-3.25")


def test_input_order_does_not_matter() -> None:
    shuffled = [*TRACED, *OPTION, FUNDING]
    random.Random(7).shuffle(shuffled)
    assert reconstruct(shuffled) == reconstruct([*TRACED, *OPTION, FUNDING])


def test_positions_are_separated_by_strategy() -> None:
    other = [replace(f, strategy_id="xsec_momentum", venue_fill_id=f"x-{i}")
             for i, f in enumerate(TRACED)]
    positions = reconstruct([*TRACED, *other])
    assert {k.strategy_id for k in positions} == {"trend", "xsec_momentum"}
    assert all(p.net_qty == D(-1) for p in positions.values())


def test_mixed_multiplier_on_one_position_raises() -> None:
    bad = fill(9, Side.BUY, "1", "100", multiplier=D("0.01"), venue_fill_id="bad")
    with pytest.raises(ValueError, match="multiplier"):
        replay([TRACED[0], bad])


def test_key_matches_view_grouping() -> None:
    key = PositionKey.of(TRACED[0])
    assert (key.venue, key.symbol, key.asset_type, key.strategy_id) == (
        "coinbase", "BIP-20DEC30-CDE", "crypto_perp", "trend"
    )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"qty": 1.5}, TypeError),
        ({"price": 100.0}, TypeError),
        ({"commission": D("-0.01")}, ValueError),
        ({"qty": D(-1)}, ValueError),
        ({"ts": at(1).replace(tzinfo=None)}, ValueError),
        ({"venue_fill_id": ""}, ValueError),
    ],
)
def test_invalid_fill_is_rejected(change: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        replace(TRACED[0], **change)
