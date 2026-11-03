"""Historial por día: qué pasó el día X y por qué cambió la equity de cada sleeve.

La equity que escribe un sleeve con ledger propio es
    equity = asignado + Σ(realizado − comisiones + funding) + no realizado
así que, entre dos snapshots,
    Δequity = realizado neto de los fills entre ambos + Δno realizado + Δasignado.

Cada día se mide desde el último snapshot ANTERIOR al día (el cierre de ayer) hasta el
último del día, y la columna "fuera del ledger" es lo que queda: Δequity − realizado −
Δno realizado. En un sleeve que deriva su equity de sus fills es ~0. En el Wheel, que
hoy toma la equity de la cuenta entera del broker, es TODO el cambio: ahí se ve que
no lo explican sus operaciones.

Los días son UTC, como en el resto del ledger.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from core.ledger.attribution import slippage, utc_day
from core.ledger.fills import fetch_fills
from core.ledger.positions import ZERO, replay

DEFAULT_DAYS = 90


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------------------
# Una fila por (día, sleeve)
# ---------------------------------------------------------------------------
@dataclass
class DayRow:
    day: date
    strategy_id: str
    open_equity: float | None = None       # último snapshot antes del día
    close_equity: float | None = None      # último snapshot del día
    open_unrealized: float = 0.0
    close_unrealized: float = 0.0
    realized_net: Decimal = ZERO           # realizado − comisiones + funding, entre snapshots
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    slippage: Decimal = ZERO
    n_fills: int = 0
    n_orders: int = 0
    n_rejected: int = 0
    n_snapshots: int = 0
    mismatches: int = 0

    @property
    def change(self) -> float | None:
        if self.close_equity is None or self.open_equity is None:
            return None
        return self.close_equity - self.open_equity

    @property
    def change_pct(self) -> float | None:
        c = self.change
        return None if c is None or not self.open_equity else c / self.open_equity

    @property
    def unrealized_change(self) -> float:
        return self.close_unrealized - self.open_unrealized

    @property
    def outside_ledger(self) -> float | None:
        c = self.change
        return None if c is None else c - float(self.realized_net) - self.unrealized_change


@dataclass(frozen=True, slots=True)
class _Snap:
    ts: datetime
    equity: float
    unrealized: float


def _snapshots(conn: Connection, since: datetime) -> dict[str, list[_Snap]]:
    """Por sleeve, en orden; incluye el último anterior a `since` (el cierre previo)."""
    out: dict[str, list[_Snap]] = defaultdict(list)
    for r in conn.execute(text("""
        SELECT strategy_id, ts, equity_usd, unrealized_pnl FROM (
          SELECT DISTINCT ON (strategy_id) * FROM equity_curve
          WHERE ts < :since ORDER BY strategy_id, ts DESC) prev
        UNION ALL
        SELECT strategy_id, ts, equity_usd, unrealized_pnl FROM equity_curve WHERE ts >= :since
        ORDER BY strategy_id, ts
    """), {"since": since}):
        out[r.strategy_id].append(_Snap(r.ts, float(r.equity_usd), float(r.unrealized_pnl)))
    return dict(out)


def daily_history(conn: Connection, *, until: date, days: int = DEFAULT_DAYS) -> list[DayRow]:
    """Días con actividad (snapshots, fills u órdenes), del más reciente al más antiguo."""
    since, _ = day_bounds(until - timedelta(days=days - 1))
    _, end = day_bounds(until)
    rows: dict[tuple[date, str], DayRow] = {}

    def row(day: date, sid: str) -> DayRow:
        return rows.setdefault((day, sid), DayRow(day, sid))

    snaps = _snapshots(conn, since)
    for sid, pts in snaps.items():
        prev: _Snap | None = None
        for s in pts:
            if s.ts < since:
                prev = s
                continue
            if s.ts >= end:
                break
            r = row(utc_day(s.ts), sid)
            if r.n_snapshots == 0:
                # Abre en el cierre anterior; sin historia previa, en el primer snapshot.
                base = prev or s
                r.open_equity, r.open_unrealized = base.equity, base.unrealized
            r.close_equity, r.close_unrealized = s.equity, s.unrealized
            r.n_snapshots += 1
            prev = s

    # Fills: cuentan en su día UTC. Un fill posterior al último snapshot del día aparece
    # como "fuera del ledger" ese día y se compensa al siguiente; los sleeves escriben un
    # snapshot justo después de operar para que eso no pase.
    _, per_fill = replay(fetch_fills(conn))
    for fp in per_fill:
        f = fp.fill
        if not since <= f.ts < end:
            continue
        r = row(utc_day(f.ts), f.strategy_id)
        if f.qty != 0:
            r.n_fills += 1
        r.fees += f.commission
        r.funding += f.funding_payment
        r.slippage += slippage(f)
        r.realized_net += fp.realized_pnl - f.commission + f.funding_payment

    for o in conn.execute(text("""
        SELECT strategy_id, (ts_intent AT TIME ZONE 'UTC')::date AS day, count(*) AS n,
               count(*) FILTER (WHERE status = 'REJECTED') AS rejected
        FROM orders WHERE ts_intent >= :since AND ts_intent < :end
        GROUP BY 1, 2
    """), {"since": since, "end": end}):
        r = row(o.day, o.strategy_id)
        r.n_orders, r.n_rejected = o.n, o.rejected

    for m in conn.execute(text("""
        SELECT detail->>'strategy_id' AS sid, (ts AT TIME ZONE 'UTC')::date AS day,
               sum(mismatches) AS n
        FROM reconciliation_runs
        WHERE ts >= :since AND ts < :end AND detail ? 'strategy_id'
        GROUP BY 1, 2
    """), {"since": since, "end": end}):
        row(m.day, m.sid).mismatches = int(m.n)

    return sorted(rows.values(), key=lambda r: (r.day, r.strategy_id), reverse=True)


# ---------------------------------------------------------------------------
# El detalle de un día
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FillRow:
    ts: datetime
    strategy_id: str
    venue: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    notional: Decimal
    commission: Decimal
    slippage: Decimal
    funding: Decimal
    realized: Decimal
    client_order_id: str | None
    kind: str = "fill"              # 'fee_in_kind': corrección de comisión en especie (D-015)


@dataclass
class DayDetail:
    day: date
    fills: list[FillRow] = field(default_factory=list)
    orders: list[Any] = field(default_factory=list)
    snapshots: list[Any] = field(default_factory=list)
    risk_events: list[Any] = field(default_factory=list)
    reconciliations: list[Any] = field(default_factory=list)
    equity: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)


def _fill_rows(conn: Connection, start: datetime | None = None,
               end: datetime | None = None) -> list[FillRow]:
    _, per_fill = replay(fetch_fills(conn))
    return [
        FillRow(ts=f.ts, strategy_id=f.strategy_id, venue=f.venue, symbol=f.symbol,
                side=f.side.value, qty=f.qty, price=f.price,
                notional=f.qty * f.price * f.multiplier, commission=f.commission,
                slippage=slippage(f), funding=f.funding_payment, realized=fp.realized_pnl,
                client_order_id=f.client_order_id,
                kind="fee_in_kind" if f.venue_fill_id.endswith(":fee") else "fill")
        for fp in per_fill
        if (f := fp.fill) and (start is None or f.ts >= start) and (end is None or f.ts < end)
    ]


def all_fills(conn: Connection) -> list[FillRow]:
    return _fill_rows(conn)


def day_detail(conn: Connection, day: date) -> DayDetail:
    start, end = day_bounds(day)
    d = DayDetail(day, fills=_fill_rows(conn, start, end))
    window = {"start": start, "end": end}
    d.orders = list(conn.execute(text("""
        SELECT ts_intent, strategy_id, client_order_id, symbol, side, qty, order_type, status,
               reject_reason, venue_order_id FROM orders
        WHERE ts_intent >= :start AND ts_intent < :end ORDER BY ts_intent
    """), window))
    d.snapshots = list(conn.execute(text("""
        SELECT ts, strategy_id, equity_usd, allocated_usd, unrealized_pnl, gross_notional
        FROM equity_curve WHERE ts >= :start AND ts < :end ORDER BY ts, strategy_id
    """), window))
    d.risk_events = list(conn.execute(text("""
        SELECT ts, kind, scope, reason, actor FROM risk_events
        WHERE ts >= :start AND ts < :end ORDER BY ts
    """), window))
    d.reconciliations = list(conn.execute(text("""
        SELECT ts, venue, detail->>'strategy_id' AS strategy_id, mismatches, action_taken,
               detail->'mismatches' AS detail FROM reconciliation_runs
        WHERE ts >= :start AND ts < :end ORDER BY ts
    """), window))
    for s in d.snapshots:
        d.equity.setdefault(s.strategy_id, []).append((s.ts, float(s.equity_usd)))
    return d


def activity_days(conn: Connection) -> tuple[date | None, date | None]:
    """El primer y el último día con algo registrado, para navegar."""
    r = conn.execute(text("""
        SELECT min(ts), max(ts) FROM (
          SELECT min(ts) AS ts FROM equity_curve UNION ALL SELECT max(ts) FROM equity_curve
          UNION ALL SELECT min(ts) FROM fills UNION ALL SELECT max(ts) FROM fills) x
    """)).one()
    return (None if r[0] is None else utc_day(r[0]), None if r[1] is None else utc_day(r[1]))


# ---------------------------------------------------------------------------
# CSV: el mismo historial, para comparar con un backtest o abrir en una hoja
# ---------------------------------------------------------------------------
HISTORY_COLUMNS = ("day", "strategy_id", "open_equity", "close_equity", "change", "change_pct",
                   "realized_net", "unrealized_change", "outside_ledger", "fees", "funding",
                   "slippage", "n_fills", "n_orders", "n_rejected", "n_snapshots", "mismatches")
FILL_COLUMNS = ("ts", "strategy_id", "venue", "symbol", "kind", "side", "qty", "price",
                "notional", "commission", "slippage", "funding", "realized", "client_order_id")


def _csv(header: tuple[str, ...], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def history_csv(rows: list[DayRow]) -> str:
    return _csv(HISTORY_COLUMNS, [[getattr(r, c) if getattr(r, c) is not None else ""
                                   for c in HISTORY_COLUMNS] for r in reversed(rows)])


def fills_csv(fills: list[FillRow]) -> str:
    return _csv(FILL_COLUMNS, [[getattr(f, c) if getattr(f, c) is not None else ""
                                for c in FILL_COLUMNS] for f in fills])
