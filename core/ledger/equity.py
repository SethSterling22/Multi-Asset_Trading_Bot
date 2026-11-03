"""Snapshot de equity por sleeve, para `equity_curve`.

equity = capital asignado + P&L realizado − fees + funding + P&L no realizado.
Es la cifra NETA (CLAUDE.md §3.4); el coste de IA se resta en la atribución,
no aquí, porque se reparte entre decisiones y no entre posiciones.

Sin precio de marca para una posición abierta no hay snapshot: se lanza
MissingMarkError en lugar de suponer un precio.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, text

from core.ledger.fills import Fill
from core.ledger.positions import ZERO, replay


class MissingMarkError(LookupError):
    """Falta el precio de marca de una posición abierta."""


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    ts: datetime
    strategy_id: str
    equity_usd: Decimal
    allocated_usd: Decimal
    gross_notional: Decimal
    unrealized_pnl: Decimal


def snapshot(
    fills: Iterable[Fill],
    *,
    strategy_id: str,
    allocated_usd: Decimal,
    marks: Mapping[tuple[str, str], Decimal],
    ts: datetime,
) -> EquitySnapshot:
    """Equity del sleeve `strategy_id` con los fills hasta `ts` inclusive.

    `marks` va indexado por (venue, symbol): el mismo símbolo puede cotizar
    distinto en dos venues.
    """
    if ts.tzinfo is None:
        raise ValueError("ts debe llevar zona horaria")
    positions, _ = replay(f for f in fills if f.strategy_id == strategy_id and f.ts <= ts)

    realized_net = ZERO
    unrealized = ZERO
    gross_notional = ZERO
    for pos in positions.values():
        realized_net += pos.realized_pnl - pos.total_commission + pos.total_funding
        if pos.net_qty == 0:
            continue
        venue_symbol = (pos.key.venue, pos.key.symbol)
        if venue_symbol not in marks:
            raise MissingMarkError(f"sin precio de marca para {venue_symbol}")
        mark = marks[venue_symbol]
        unrealized += pos.unrealized_pnl(mark)
        gross_notional += abs(pos.net_qty * mark * pos.multiplier)

    return EquitySnapshot(
        ts=ts,
        strategy_id=strategy_id,
        equity_usd=allocated_usd + realized_net + unrealized,
        allocated_usd=allocated_usd,
        gross_notional=gross_notional,
        unrealized_pnl=unrealized,
    )


def write_snapshot(conn: Connection, snap: EquitySnapshot) -> bool:
    """Escribe el snapshot; reescribir el mismo (strategy_id, ts) es un no-op."""
    row = conn.execute(
        text("""
            INSERT INTO equity_curve
              (ts, strategy_id, equity_usd, allocated_usd, gross_notional, unrealized_pnl)
            VALUES (:ts, :strategy_id, :equity_usd, :allocated_usd, :gross_notional,
                    :unrealized_pnl)
            ON CONFLICT (strategy_id, ts) DO NOTHING
            RETURNING ts
        """),
        {
            "ts": snap.ts,
            "strategy_id": snap.strategy_id,
            # equity_curve es double precision (serie temporal); la verdad al
            # centavo está en `fills`, que es numeric.
            "equity_usd": float(snap.equity_usd),
            "allocated_usd": float(snap.allocated_usd),
            "gross_notional": float(snap.gross_notional),
            "unrealized_pnl": float(snap.unrealized_pnl),
        },
    ).first()
    return row is not None


def write_marks(conn: Connection, *, ts: datetime, strategy_id: str,
                positions: Mapping[tuple[str, str], tuple[Decimal, Decimal]]) -> int:
    """Cantidad y precio de marca de cada posición abierta, junto al snapshot de `ts`.

    `positions` va indexado por (venue, symbol) -> (qty, mark). Reescribir el mismo
    instante es un no-op, como el snapshot.
    """
    written = 0
    for (venue, symbol), (qty, mark) in positions.items():
        row = conn.execute(text("""
            INSERT INTO position_marks (ts, strategy_id, venue, symbol, qty, mark)
            VALUES (:ts, :s, :v, :sym, :q, :m)
            ON CONFLICT DO NOTHING RETURNING ts
        """), {"ts": ts, "s": strategy_id, "v": venue, "sym": symbol, "q": qty, "m": mark}).first()
        written += row is not None
    return written
