"""Diario de operaciones: cada entrada con su salida, a qué precio y por qué.

Se reconstruye de `fills` emparejando en FIFO por (sleeve, venue, símbolo): un fill
que abre posición crea un lote; uno en sentido contrario cierra lotes empezando por el
más antiguo. Cada fila es un tramo de lote, abierto o cerrado:

    entrada:  hora, precio, lo pagado (qty · precio + comisión) y motivo de la orden
    salida:   hora, precio, lo cobrado (qty · precio − comisión) y motivo de la orden que
              lo cerró; si sigue abierto, el último precio de marca
    P&L:      bruto = (salida − entrada) · qty · multiplier (con signo para cortos)
              neto  = bruto − comisiones del tramo (entrada a prorrata + salida)

Los fills de corrección de comisión en especie (venue_fill_id "…:fee", D-015) no son
salidas: reducen el lote de la orden a la que pertenecen y su valor es comisión.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from core.ledger.fills import Fill, fetch_fills

ZERO = Decimal(0)
FEE_SUFFIX = ":fee"
NO_REASON = "no registrado (orden anterior a D-015)"


@dataclass
class TradeRow:
    strategy_id: str
    venue: str
    symbol: str
    direction: str                      # 'largo' | 'corto'
    qty: Decimal
    multiplier: Decimal
    entry_ts: datetime
    entry_price: Decimal
    entry_reason: str
    entry_order: str | None
    entry_fees: Decimal = ZERO
    exit_fees: Decimal = ZERO
    exit_ts: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str | None = None
    exit_order: str | None = None
    mark: Decimal | None = None         # abiertas: último precio de marca registrado
    mark_ts: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None

    @property
    def fees(self) -> Decimal:
        return self.entry_fees + self.exit_fees

    @property
    def entry_notional(self) -> Decimal:
        return self.qty * self.entry_price * self.multiplier

    @property
    def entry_amount(self) -> Decimal:
        """Lo pagado al entrar, comisión incluida (en un corto, lo cobrado neto)."""
        sign = 1 if self.direction == "largo" else -1
        return self.entry_notional + sign * self.entry_fees

    @property
    def exit_amount(self) -> Decimal | None:
        """Lo cobrado al salir, neto de comisión (en un corto, lo pagado). En una abierta,
        lo que valdría a precio de marca, sin la comisión de cerrarla."""
        n = self.exit_notional
        sign = 1 if self.direction == "largo" else -1
        return None if n is None else n - sign * self.exit_fees

    @property
    def exit_price_or_mark(self) -> Decimal | None:
        return self.mark if self.is_open else self.exit_price

    @property
    def exit_notional(self) -> Decimal | None:
        p = self.exit_price_or_mark
        return None if p is None else self.qty * p * self.multiplier

    @property
    def gross_pnl(self) -> Decimal | None:
        p = self.exit_price_or_mark
        if p is None:
            return None
        sign = 1 if self.direction == "largo" else -1
        return (p - self.entry_price) * self.qty * self.multiplier * sign

    @property
    def net_pnl(self) -> Decimal | None:
        g = self.gross_pnl
        return None if g is None else g - self.fees

    @property
    def return_pct(self) -> Decimal | None:
        """Neto sobre lo pagado al entrar."""
        n = self.net_pnl
        return None if n is None or not self.entry_amount else n / self.entry_amount

    @property
    def held_for(self) -> timedelta | None:
        end = self.mark_ts if self.is_open else self.exit_ts
        return None if end is None else end - self.entry_ts


@dataclass
class _Lot:
    row: TradeRow
    remaining: Decimal                  # con signo


def _orders(conn: Connection) -> dict[str, str | None]:
    return {r.client_order_id: r.reason for r in conn.execute(
        text("SELECT client_order_id, reason FROM orders"))}


# (sleeve, venue, símbolo) -> (hora, precio de marca)
Marks = Mapping[tuple[str, str, str], tuple[datetime, Decimal]]


def _latest_marks(conn: Connection) -> dict[tuple[str, str, str], tuple[datetime, Decimal]]:
    return {(r.strategy_id, r.venue, r.symbol): (r.ts, Decimal(str(r.mark)))
            for r in conn.execute(text("""
        SELECT DISTINCT ON (strategy_id, venue, symbol) strategy_id, venue, symbol, ts, mark
        FROM position_marks ORDER BY strategy_id, venue, symbol, ts DESC
    """))}


def trades(conn: Connection, fills: list[Fill] | None = None) -> list[TradeRow]:
    """Las operaciones del ledger, de la más reciente a la más antigua."""
    return pair_trades(fills if fills is not None else fetch_fills(conn),
                       reasons=_orders(conn), marks=_latest_marks(conn))


def pair_trades(fills: Sequence[Fill], *, reasons: Mapping[str, str | None],
                marks: Marks) -> list[TradeRow]:
    """El emparejamiento FIFO, sin base de datos: sirve igual para el ledger y para las
    operaciones de una corrida de backtest. `reasons` va por client_order_id."""
    fills = sorted(fills, key=lambda f: f.ts)

    def reason(coid: str | None) -> str:
        return (reasons.get(coid) if coid else None) or NO_REASON

    books: dict[tuple[str, str, str], list[_Lot]] = defaultdict(list)
    closed: list[TradeRow] = []
    for f in fills:
        key = (f.strategy_id, f.venue, f.symbol)
        lots = books[key]
        if f.venue_fill_id.endswith(FEE_SUFFIX):
            # Comisión en especie de una entrada ya registrada: menos cantidad, más coste.
            lot = next((x for x in lots if x.row.entry_order == f.client_order_id), None)
            if lot is not None:
                lot.remaining -= f.qty if lot.remaining > 0 else -f.qty
                lot.row.qty = abs(lot.remaining)
                lot.row.entry_fees += f.commission
                continue
        d = f.signed_qty
        fee_left = f.commission
        # Cierra lotes en sentido contrario, del más antiguo al más nuevo.
        while d != 0 and lots and (lots[0].remaining > 0) != (d > 0):
            lot = lots[0]
            take = min(abs(d), abs(lot.remaining))
            share_entry = lot.row.entry_fees * take / abs(lot.remaining)
            share_exit = f.commission * take / f.qty if f.qty else ZERO
            fee_left -= share_exit
            closed.append(TradeRow(
                strategy_id=f.strategy_id, venue=f.venue, symbol=f.symbol,
                direction=lot.row.direction, qty=take, multiplier=f.multiplier,
                entry_ts=lot.row.entry_ts, entry_price=lot.row.entry_price,
                entry_reason=lot.row.entry_reason, entry_order=lot.row.entry_order,
                entry_fees=share_entry, exit_fees=share_exit, exit_ts=f.ts, exit_price=f.price,
                exit_reason=reason(f.client_order_id), exit_order=f.client_order_id))
            lot.row.entry_fees -= share_entry
            sign = 1 if lot.remaining > 0 else -1
            lot.remaining -= take * sign
            lot.row.qty = abs(lot.remaining)
            d += take * sign
            if lot.remaining == 0:
                lots.pop(0)
        if d != 0:
            # Lo que queda abre (o amplía) posición: un lote nuevo con la comisión restante.
            lots.append(_Lot(TradeRow(
                strategy_id=f.strategy_id, venue=f.venue, symbol=f.symbol,
                direction="largo" if d > 0 else "corto", qty=abs(d), multiplier=f.multiplier,
                entry_ts=f.ts, entry_price=f.price, entry_reason=reason(f.client_order_id),
                entry_order=f.client_order_id, entry_fees=fee_left), d))

    still_open = []
    for key, lots in books.items():
        m = marks.get(key)
        for lot in lots:
            if m is not None:
                lot.row.mark_ts, lot.row.mark = m
            still_open.append(lot.row)
    return sorted(still_open + closed, key=lambda t: (t.exit_ts or t.entry_ts, t.entry_ts),
                  reverse=True)


@dataclass(frozen=True, slots=True)
class TradeTotals:
    n_open: int
    n_closed: int
    paid_open: Decimal                  # lo pagado por las abiertas, comisión incluida
    value_open: Decimal | None
    net_closed: Decimal
    net_open: Decimal | None
    fees: Decimal


def _sum_or_none(values: list[Decimal | None]) -> Decimal | None:
    """La suma, o None si falta alguna (una abierta sin precio de marca)."""
    total = ZERO
    for v in values:
        if v is None:
            return None
        total += v
    return total


def totals(rows: list[TradeRow]) -> TradeTotals:
    open_rows = [r for r in rows if r.is_open]
    closed_rows = [r for r in rows if not r.is_open]
    return TradeTotals(
        n_open=len(open_rows), n_closed=len(closed_rows),
        paid_open=sum((r.entry_amount for r in open_rows), ZERO),
        value_open=_sum_or_none([r.exit_notional for r in open_rows]),
        net_closed=sum((r.net_pnl or ZERO for r in closed_rows), ZERO),
        net_open=_sum_or_none([r.net_pnl for r in open_rows]),
        fees=sum((r.fees for r in rows), ZERO),
    )


# ---------------------------------------------------------------------------
# Por qué se movió el no realizado: el precio de cada posición entre dos instantes
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PriceMove:
    strategy_id: str
    symbol: str
    qty: Decimal
    mark_from: Decimal
    mark_to: Decimal
    ts_from: datetime
    ts_to: datetime

    @property
    def change_pct(self) -> Decimal:
        return self.mark_to / self.mark_from - 1 if self.mark_from else ZERO

    @property
    def pnl(self) -> Decimal:
        """Lo que ese movimiento de precio sumó o restó a la posición (qty al final)."""
        return self.qty * (self.mark_to - self.mark_from)


def price_moves(conn: Connection, start: datetime, end: datetime) -> list[PriceMove]:
    """Por símbolo: del último precio anterior a `start` (o el primero del tramo, si no
    hay) al último anterior a `end`. Solo símbolos con algún precio dentro del tramo."""
    by_key: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for r in conn.execute(text("""
        SELECT strategy_id, venue, symbol, ts, qty, mark FROM position_marks
        WHERE ts >= :since AND ts < :end ORDER BY ts
    """), {"since": start - timedelta(days=7), "end": end}):
        by_key[(r.strategy_id, r.venue, r.symbol)].append(r)
    out = []
    for (sid, _, symbol), rows in sorted(by_key.items()):
        inside = [r for r in rows if r.ts >= start]
        if not inside:
            continue
        before = [r for r in rows if r.ts < start]
        first, last = (before[-1] if before else inside[0]), inside[-1]
        out.append(PriceMove(sid, symbol, Decimal(str(last.qty)), Decimal(str(first.mark)),
                             Decimal(str(last.mark)), first.ts, last.ts))
    return out


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
TRADE_COLUMNS = ("strategy_id", "venue", "symbol", "direction", "status", "qty",
                 "entry_ts", "entry_price", "entry_fees", "entry_amount", "entry_reason",
                 "entry_order", "exit_ts", "exit_price", "exit_fees", "exit_amount",
                 "exit_reason", "exit_order", "gross_pnl", "net_pnl", "return_pct", "held_hours")


def trades_csv(rows: list[TradeRow]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TRADE_COLUMNS)
    for t in reversed(rows):
        held = t.held_for
        exit_ts = t.exit_ts or t.mark_ts
        w.writerow([
            t.strategy_id, t.venue, t.symbol, t.direction, "abierta" if t.is_open else "cerrada",
            t.qty, t.entry_ts.isoformat(), t.entry_price, t.entry_fees, t.entry_amount,
            t.entry_reason, t.entry_order or "", "" if exit_ts is None else exit_ts.isoformat(),
            t.exit_price_or_mark if t.exit_price_or_mark is not None else "", t.exit_fees,
            t.exit_amount if t.exit_amount is not None else "",
            t.exit_reason or ("abierta: precio de marca" if t.mark is not None else "abierta"),
            t.exit_order or "",
            "" if t.gross_pnl is None else t.gross_pnl, "" if t.net_pnl is None else t.net_pnl,
            "" if t.return_pct is None else t.return_pct,
            "" if held is None else round(held.total_seconds() / 3600, 2),
        ])
    return buf.getvalue()
