"""Un venue spot falso con el Protocol `SpotVenue` y fallos inyectables.

Se comporta como Alpaca donde importa para el router: un `client_order_id` repetido
se rechaza como "ya existe" (y el adaptador real lo traduce a VenueUnavailableError),
y una orden market se llena entera y al momento, salvo que se pida lo contrario.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from brokers.base import (
    AssetRules,
    OrderStatus,
    Quote,
    VenueOrder,
    VenueRejectedError,
    VenueUnavailableError,
    normalize_symbol,
)

D = Decimal
SPREAD = D("0.0002")
CASH = D(100000)
Fault = Literal["timeout_before", "timeout_after", "reject"] | None


@dataclass
class FakeVenue:
    name: str = "fake"
    prices: dict[str, Decimal] = field(
        default_factory=lambda: {"BTCUSD": D(60000), "ETHUSD": D(3000)})
    spread: Decimal = SPREAD
    cash_usd: Decimal = CASH
    held: dict[str, Decimal] = field(default_factory=dict)
    orders: dict[str, VenueOrder] = field(default_factory=dict)
    submits: int = 0
    submit_fault: Fault = None
    lookup_down: bool = False
    positions_down: bool = False
    leave_open: bool = False            # la orden queda NEW y no se llena

    def _mid(self, symbol: str) -> Decimal:
        return self.prices[normalize_symbol(symbol)]

    def submit_market_order(self, *, client_order_id: str, symbol: str, side: str,
                            qty: Decimal) -> VenueOrder:
        self.submits += 1
        if client_order_id in self.orders:
            raise VenueUnavailableError("client_order_id must be unique")
        if self.submit_fault == "reject":
            raise VenueRejectedError("insufficient balance")
        if self.submit_fault == "timeout_before":
            raise VenueUnavailableError("ReadTimeout")
        q = self.quote(symbol)
        price = q.ask if side == "buy" else q.bid
        key = normalize_symbol(symbol)
        if self.leave_open:
            order = VenueOrder(client_order_id, f"v-{len(self.orders) + 1}", OrderStatus.NEW,
                               D(0), None)
        else:
            sign = 1 if side == "buy" else -1
            self.held[key] = self.held.get(key, D(0)) + sign * qty
            self.cash_usd -= sign * qty * price
            order = VenueOrder(client_order_id, f"v-{len(self.orders) + 1}", OrderStatus.FILLED,
                               qty, price, datetime.now(UTC))
        self.orders[client_order_id] = order
        if self.submit_fault == "timeout_after":
            raise VenueUnavailableError("ReadTimeout after the venue accepted it")
        return order

    def get_order(self, client_order_id: str) -> VenueOrder | None:
        if self.lookup_down:
            raise VenueUnavailableError("503")
        return self.orders.get(client_order_id)

    def positions(self) -> dict[str, Decimal]:
        if self.positions_down:
            raise VenueUnavailableError("503")
        return {k: v for k, v in self.held.items() if v != 0}

    def cash(self) -> Decimal:
        return self.cash_usd

    def quote(self, symbol: str) -> Quote:
        mid = self._mid(symbol)
        half = mid * self.spread / 2
        return Quote(bid=mid - half, ask=mid + half)

    def rules(self, symbol: str) -> AssetRules:
        return AssetRules(min_order_qty=D("0.0001"), qty_increment=D("0.000001"))


def never(_: str) -> bool:
    return False
