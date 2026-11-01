"""Posiciones y P&L realizado, derivados SOLO de `fills` (CLAUDE.md §1.3).

Nada aquí guarda estado entre llamadas: cada función recibe la secuencia de
fills y la replaya. Si el proceso muere no se pierde nada, porque la posición
nunca estuvo en memoria.

Método de coste: coste medio ponderado por posición
(venue, symbol, asset_type, strategy_id). Al reducir, el P&L realizado es
`cerrado × (precio − coste_medio) × signo(posición) × multiplier`; al cruzar de
largo a corto, el resto abre una posición nueva al precio del fill.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, text

from core.ledger.fills import AssetType, Fill

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True, order=True)
class PositionKey:
    venue: str
    symbol: str
    asset_type: AssetType
    strategy_id: str

    @classmethod
    def of(cls, fill: Fill) -> PositionKey:
        return cls(fill.venue, fill.symbol, fill.asset_type, fill.strategy_id)


@dataclass(frozen=True, slots=True)
class Position:
    key: PositionKey
    net_qty: Decimal
    avg_cost: Decimal          # por unidad, en precio; 0 si net_qty == 0
    multiplier: Decimal
    realized_pnl: Decimal      # a precios de fill, antes de fees y funding
    net_cost: Decimal          # Σ qty·price·multiplier con signo (== vista `positions`)
    total_commission: Decimal
    total_funding: Decimal
    last_fill_ts: datetime

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        return self.net_qty * (mark - self.avg_cost) * self.multiplier


@dataclass(frozen=True, slots=True)
class FillPnl:
    fill: Fill
    realized_pnl: Decimal


@dataclass(slots=True)
class _Book:
    multiplier: Decimal
    qty: Decimal = ZERO
    avg_cost: Decimal = ZERO
    realized: Decimal = ZERO
    net_cost: Decimal = ZERO
    commission: Decimal = ZERO
    funding: Decimal = ZERO
    last_ts: datetime | None = None

    def apply(self, fill: Fill) -> Decimal:
        """Aplica un fill y devuelve el P&L que realiza."""
        if fill.multiplier != self.multiplier:
            raise ValueError(
                f"{fill.symbol}: multiplier {fill.multiplier} distinto del de la "
                f"posición ({self.multiplier})"
            )
        self.commission += fill.commission
        self.funding += fill.funding_payment
        self.net_cost += fill.signed_qty * fill.price * fill.multiplier
        self.last_ts = fill.ts

        d, q, p = fill.signed_qty, self.qty, fill.price
        if d == 0:
            return ZERO
        if q == 0 or (q > 0) == (d > 0):
            self.avg_cost = (abs(q) * self.avg_cost + abs(d) * p) / (abs(q) + abs(d))
            self.qty = q + d
            return ZERO

        closed = min(abs(d), abs(q))
        sign = Decimal(1) if q > 0 else Decimal(-1)
        realized = closed * (p - self.avg_cost) * sign * self.multiplier
        self.realized += realized
        self.qty = q + d
        if self.qty == 0:
            self.avg_cost = ZERO
        elif (self.qty > 0) != (q > 0):
            self.avg_cost = p          # cruzó: el resto abre al precio del fill
        return realized


def replay(fills: Iterable[Fill]) -> tuple[dict[PositionKey, Position], list[FillPnl]]:
    """Replaya los fills en orden de ts (estable a igual ts).

    Devuelve todas las posiciones que han existido, incluidas las cerradas, y el
    P&L realizado por cada fill.
    """
    books: dict[PositionKey, _Book] = {}
    per_fill: list[FillPnl] = []
    for fill in sorted(fills, key=lambda f: f.ts):
        key = PositionKey.of(fill)
        book = books.setdefault(key, _Book(multiplier=fill.multiplier))
        per_fill.append(FillPnl(fill, book.apply(fill)))
    positions = {
        key: Position(
            key=key,
            net_qty=b.qty,
            avg_cost=b.avg_cost,
            multiplier=b.multiplier,
            realized_pnl=b.realized,
            net_cost=b.net_cost,
            total_commission=b.commission,
            total_funding=b.funding,
            last_fill_ts=b.last_ts,  # type: ignore[arg-type]  # siempre hay ≥1 fill
        )
        for key, b in books.items()
    }
    return positions, per_fill


def reconstruct(fills: Iterable[Fill]) -> dict[PositionKey, Position]:
    """Posiciones abiertas. Mismo criterio que la vista `positions`."""
    positions, _ = replay(fills)
    return {k: p for k, p in positions.items() if p.net_qty != 0}


@dataclass(frozen=True, slots=True)
class ViewPosition:
    """Una fila de la vista SQL `positions`."""

    net_qty: Decimal
    net_cost: Decimal
    total_commission: Decimal
    total_funding: Decimal
    last_fill_ts: datetime


def load_positions_view(conn: Connection) -> dict[PositionKey, ViewPosition]:
    rows = conn.execute(text(
        "SELECT venue, symbol, asset_type, strategy_id, net_qty, net_cost,"
        " total_commission, total_funding, last_fill_ts FROM positions"
    ))
    return {
        PositionKey(r.venue, r.symbol, AssetType(r.asset_type), r.strategy_id): ViewPosition(
            r.net_qty, r.net_cost, r.total_commission, r.total_funding, r.last_fill_ts
        )
        for r in rows
    }
