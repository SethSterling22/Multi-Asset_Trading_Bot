"""Contratos con los venues. La lógica de ejecución no conoce ningún SDK.

`SpotVenue` es lo mínimo que la capa de ejecución necesita de un venue spot: enviar
una orden con `client_order_id`, consultarla por ese id, posiciones, caja, precios
y reglas de tamaño. El adaptador real (p.ej. brokers/alpaca_crypto.py) traduce un
SDK a esto; los tests usan un venue falso con el mismo Protocol.

Lo que el router necesita saber de un error es si la orden pudo llegar o no:
- `VenueUnavailableError`: timeout, 5xx, red caída. La orden PUEDE haber llegado:
  nunca se reenvía a ciegas, se consulta por `client_order_id` (CLAUDE.md §1.4).
- `VenueRejectedError`: el venue la rechazó explícitamente. No llegó a existir.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class OrderStatus(StrEnum):
    """Espejo del enum `order_status` de la migración 0001."""

    PENDING_NEW = "PENDING_NEW"     # write-ahead: escrita en la base, aún no confirmada
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"             # no se sabe si llegó: requiere reconciliación

    @property
    def is_open(self) -> bool:
        return self in OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED,
                        OrderStatus.EXPIRED)


OPEN_STATUSES = frozenset({OrderStatus.PENDING_NEW, OrderStatus.NEW,
                           OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN})


class VenueUnavailableError(RuntimeError):
    """El venue no respondió con claridad: la orden puede haber llegado o no."""


class VenueRejectedError(RuntimeError):
    """El venue rechazó la orden de forma explícita."""


@dataclass(frozen=True, slots=True)
class VenueOrder:
    client_order_id: str
    venue_order_id: str
    status: OrderStatus
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    # Del venue, no del reloj local: reaplicar la misma orden (reconciliación) tiene
    # que producir el mismo fill, o el ledger lo rechaza como conflicto.
    filled_at: datetime | None = None
    # Comisiones. `fee_qty` se cobra en el activo recibido (lo que llega a la cuenta es
    # filled_qty − fee_qty); `fee_usd`, en dólares. Ver DECISIONS.md D-015.
    fee_qty: Decimal = Decimal(0)
    fee_usd: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class Execution:
    """Una ejecución (parcial o total) de una orden, como la reporta el venue."""

    qty: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class AssetRules:
    min_order_qty: Decimal
    qty_increment: Decimal


@dataclass(frozen=True, slots=True)
class Quote:
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


class SpotVenue(Protocol):
    name: str

    def submit_market_order(self, *, client_order_id: str, symbol: str, side: str,
                            qty: Decimal) -> VenueOrder: ...
    def get_order(self, client_order_id: str) -> VenueOrder | None: ...
    def positions(self) -> dict[str, Decimal]: ...
    def cash(self) -> Decimal: ...
    def quote(self, symbol: str) -> Quote: ...
    def rules(self, symbol: str) -> AssetRules: ...
    def executions(self, venue_order_id: str) -> list[Execution]: ...


def normalize_symbol(symbol: str) -> str:
    """BTC/USD y BTCUSD son el mismo par: los venues no siempre usan el mismo formato."""
    return symbol.replace("/", "").replace("-", "").upper()
