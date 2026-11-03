"""La cuenta propia del Wheel (DECISIONS.md D-016).

Hasta D-016 el Wheel tomaba como suya la equity y la caja de TODA la cuenta de Alpaca,
que en paper comparte con el sleeve cripto: la cripto contaba como equity del Wheel, y
la caja del presupuesto cripto podía acabar de colateral de una put. Ahora, como el
sleeve cripto:

    equity = presupuesto + realizado − comisiones + no realizado a precio de marca
    caja   = min(presupuesto + flujo de caja de SUS fills, caja real de la cuenta)

La caja real sigue siendo un tope: el presupuesto no puede prometer dinero que la
cuenta no tiene. Una posición abierta sin precio de marca no se estima: sin precio no
hay equity, y sin equity el Wheel no opera esa iteración (falla cerrado).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.ledger.equity import EquitySnapshot, MissingMarkError, sleeve_cash, snapshot
from core.ledger.fills import AssetType, Fill
from core.ledger.positions import reconstruct

STRATEGY_ID = "wheel"

# (símbolo del ledger, tipo) -> precio de marca por unidad, o None si no lo hay.
MarkFn = Callable[[str, AssetType], Decimal | None]


@dataclass(frozen=True, slots=True)
class WheelBook:
    snapshot: EquitySnapshot
    cash: Decimal
    # (venue, símbolo) -> (cantidad con signo, precio de marca), para `position_marks`.
    positions: dict[tuple[str, str], tuple[Decimal, Decimal]]


def wheel_book(fills: Sequence[Fill], *, budget: Decimal, account_cash: Decimal,
               mark: MarkFn, ts: datetime) -> WheelBook:
    """Equity, caja y precios del Wheel a partir de sus fills. MissingMarkError si una
    posición abierta no tiene precio."""
    if budget <= 0:
        raise ValueError("WHEEL_BUDGET_USD > 0")
    own = [f for f in fills if f.strategy_id == STRATEGY_ID and f.ts <= ts]
    marks: dict[tuple[str, str], Decimal] = {}
    positions: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for key, pos in reconstruct(own).items():
        price = mark(key.symbol, key.asset_type)
        if price is None:
            raise MissingMarkError(f"{key.symbol}: sin precio de marca")
        marks[(key.venue, key.symbol)] = price
        positions[(key.venue, key.symbol)] = (pos.net_qty, price)
    snap = snapshot(own, strategy_id=STRATEGY_ID, allocated_usd=budget, marks=marks, ts=ts)
    return WheelBook(snap, min(sleeve_cash(own, budget), account_cash), positions)
