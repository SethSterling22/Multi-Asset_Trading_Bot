"""Diario: el calendario de P&L y sus estadísticas, sobre el ledger o sobre una corrida
de backtest (DECISIONS.md D-017).

Dos fuentes con la misma forma:
- **real**: el ledger (paper, y las operaciones manuales del sleeve `manual`), todos los
  sleeves o uno. El P&L de un día es el cambio de equity SIN el cambio de capital
  asignado (Historial), o en modo "realizado" el neto de las operaciones cerradas ese día.
- **backtest**: una corrida guardada (backtest/runs.py), con su curva diaria y sus
  operaciones, y SIEMPRE con su N y su DSR a la vista: una corrida suelta, elegida a
  ojo entre veinte, es exactamente lo que el DSR deflacta (CLAUDE.md §1.11).

No hay "puntuación" compuesta: sus pesos serían números mágicos (CLAUDE.md §1.9). Se
enseñan sus componentes, cada uno con su n.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from api import history, trades
from core.ledger.attribution import utc_day
from core.ledger.fills import AssetType, Fill, Side, fetch_fills
from core.metrics import max_drawdown

ZERO = Decimal(0)
BREAKEVEN_USD = Decimal("0.005")       # por debajo de medio centavo, ni gana ni pierde
MODES = ("equity", "realizado")


@dataclass
class DayPnl:
    day: date
    pnl: float = 0.0
    n_closed: int = 0
    n_wins: int = 0

    @property
    def win_rate(self) -> float | None:
        return None if not self.n_closed else self.n_wins / self.n_closed


@dataclass
class Source:
    key: str                                   # 'real' | 'bt:<id>'
    label: str
    trades: list[trades.TradeRow]
    daily: dict[date, DayPnl]
    equity: list[tuple[datetime, float]]       # cierre de cada día
    allocated: list[tuple[datetime, float]]    # capital asignado (los "depósitos")
    backtest: Any | None = None                # la fila de backtest_runs, si lo es


def _closed_by_day(rows: list[trades.TradeRow]) -> dict[date, DayPnl]:
    """Neto, número y ganadoras de las operaciones cerradas cada día (por día de salida)."""
    out: dict[date, DayPnl] = {}
    for t in rows:
        if t.is_open or t.exit_ts is None or t.net_pnl is None:
            continue
        d = utc_day(t.exit_ts)
        acc = out.setdefault(d, DayPnl(d))
        acc.pnl += float(t.net_pnl)
        acc.n_closed += 1
        acc.n_wins += int(t.net_pnl > BREAKEVEN_USD)
    return out


def _daily(rows: list[trades.TradeRow], equity_pnl: dict[date, float], mode: str
           ) -> dict[date, DayPnl]:
    closed = _closed_by_day(rows)
    days = set(closed) | (set(equity_pnl) if mode == "equity" else set())
    out = {}
    for d in days:
        c = closed.get(d, DayPnl(d))
        out[d] = DayPnl(d, equity_pnl.get(d, 0.0) if mode == "equity" else c.pnl,
                        c.n_closed, c.n_wins)
    return out


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------
def real_source(conn: Connection, *, sleeve: str | None, until: date, mode: str) -> Source:
    rows = [t for t in trades.trades(conn) if sleeve is None or t.strategy_id == sleeve]
    hist = [r for r in history.daily_history(conn, until=until, days=3650)
            if sleeve is None or r.strategy_id == sleeve]
    equity_pnl: dict[date, float] = defaultdict(float)
    closes: dict[str, dict[date, tuple[float, float]]] = defaultdict(dict)
    for r in hist:
        if r.change is not None:
            equity_pnl[r.day] += r.change - r.allocated_change
        if r.close_equity is not None:
            closes[r.strategy_id][r.day] = (r.close_equity, r.close_allocated)
    # Un sleeve sin snapshots de equity (el manual: no hay cuenta que valorar) aporta lo
    # realizado de sus operaciones cerradas, sin capital asignado conocido.
    for sid in {t.strategy_id for t in rows} - set(closes):
        own = _closed_by_day([t for t in rows if t.strategy_id == sid])
        if not own:
            continue
        first = min(own)
        cumulative = 0.0
        closes[sid][date.fromordinal(first.toordinal() - 1)] = (0.0, 0.0)
        for d in sorted(own):
            cumulative += own[d].pnl
            equity_pnl[d] += own[d].pnl
            closes[sid][d] = (cumulative, 0.0)
    # Balance: la suma de los cierres de cada sleeve, arrastrando el último conocido.
    days = sorted({d for c in closes.values() for d in c})
    last: dict[str, tuple[float, float]] = {}
    equity, allocated = [], []
    for d in days:
        for sid, c in closes.items():
            if d in c:
                last[sid] = c[d]
        ts = datetime(d.year, d.month, d.day, tzinfo=UTC)
        equity.append((ts, sum(v[0] for v in last.values())))
        allocated.append((ts, sum(v[1] for v in last.values())))
    label = "Real · todos los sleeves" if sleeve is None else f"Real · {sleeve}"
    return Source("real", label, rows, _daily(rows, dict(equity_pnl), mode), equity, allocated)


def backtest_runs(conn: Connection) -> list[Any]:
    return list(conn.execute(text("""
        SELECT id, strategy_id, params_hash, params, window_start, window_end,
               initial_capital, final_equity, fees_usd, n_orders, discarded, n_trials, dsr,
               is_candidate, ts
        FROM backtest_runs ORDER BY is_candidate DESC, dsr DESC NULLS LAST, id
    """)))


def run_label(r: Any) -> str:
    p = r.params.get("crypto_parameters", {})
    return (f"#{r.id} · umbral {p.get('rebalance_threshold', '?')} · USDC "
            f"{p.get('stable_target_allocation', '?')} · {r.window_start}→{r.window_end}"
            f"{' · candidato' if r.is_candidate else ''}")


def backtest_source(conn: Connection, run_id: int, *, mode: str) -> Source | None:
    run = conn.execute(text("SELECT * FROM backtest_runs WHERE id = :i"), {"i": run_id}).first()
    if run is None:
        return None
    curve = list(conn.execute(text(
        "SELECT day, equity FROM backtest_equity WHERE run_id = :i ORDER BY day"), {"i": run_id}))
    fills, reasons = [], {}
    for f in conn.execute(text(
            "SELECT * FROM backtest_fills WHERE run_id = :i ORDER BY seq"), {"i": run_id}):
        coid = f"bt{run_id}-{f.seq}"
        reasons[coid] = f.reason
        fills.append(Fill(ts=f.ts, venue="backtest", symbol=f.symbol,
                          asset_type=AssetType.CRYPTO_SPOT, side=Side(f.side),
                          qty=Decimal(str(f.qty)), price=Decimal(str(f.price)),
                          commission=Decimal(str(f.commission)), strategy_id=run.strategy_id,
                          venue_fill_id=coid, client_order_id=coid))
    end = datetime(run.window_end.year, run.window_end.month, run.window_end.day, tzinfo=UTC)
    marks = {(run.strategy_id, "backtest", s): (end, Decimal(str(m)))
             for s, m in (run.final_marks or {}).items()}
    rows = trades.pair_trades(fills, reasons=reasons, marks=marks)
    equity = [(datetime(c.day.year, c.day.month, c.day.day, tzinfo=UTC), float(c.equity))
              for c in curve]
    equity_pnl = {curve[i].day: float(curve[i].equity) - float(curve[i - 1].equity)
                  for i in range(1, len(curve))}
    allocated = [(t, float(run.initial_capital)) for t, _ in equity]
    return Source(f"bt:{run_id}", f"Backtest {run_label(run)}", rows,
                  _daily(rows, equity_pnl, mode), equity, allocated, backtest=run)


# ---------------------------------------------------------------------------
# Estadísticas
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Stats:
    n_closed: int
    n_wins: int
    n_losses: int
    n_breakeven: int
    gross_win: Decimal
    gross_loss: Decimal                 # <= 0
    largest_win: Decimal | None
    largest_loss: Decimal | None
    net_pnl: float | None               # cambio de equity sin el de asignado, toda la ventana
    balance: float | None
    max_drawdown_pct: float | None      # fracción negativa
    max_drawdown_usd: float | None      # <= 0

    @property
    def win_rate(self) -> float | None:
        return None if not self.n_closed else self.n_wins / self.n_closed

    @property
    def profit_factor(self) -> float | None:
        """Σ ganancias / |Σ pérdidas| de las cerradas. Sin pérdidas no está definido."""
        return None if self.gross_loss == 0 else float(self.gross_win / -self.gross_loss)

    @property
    def avg_win(self) -> Decimal | None:
        return None if not self.n_wins else self.gross_win / self.n_wins

    @property
    def avg_loss(self) -> Decimal | None:
        return None if not self.n_losses else self.gross_loss / self.n_losses

    @property
    def win_loss_ratio(self) -> float | None:
        w, lo = self.avg_win, self.avg_loss
        return None if w is None or lo is None or lo == 0 else float(w / -lo)

    @property
    def recovery_factor(self) -> float | None:
        """P&L neto / |drawdown máximo en $|."""
        if self.net_pnl is None or not self.max_drawdown_usd:
            return None
        return self.net_pnl / -self.max_drawdown_usd


def stats(src: Source) -> Stats:
    closed = [t for t in src.trades if not t.is_open and t.net_pnl is not None]
    nets = [t.net_pnl for t in closed if t.net_pnl is not None]
    wins = [n for n in nets if n > BREAKEVEN_USD]
    losses = [n for n in nets if n < -BREAKEVEN_USD]
    pnl_curve = [e - a for (_, e), (_, a) in zip(src.equity, src.allocated, strict=True)]
    capital = src.allocated[-1][1] if src.allocated else 0.0
    dd_pct = dd_usd = None
    if len(pnl_curve) > 1:
        # En % solo con capital conocido: sin él (operaciones manuales) no hay base.
        dd_pct = max_drawdown([capital + p for p in pnl_curve]) if capital > 0 else None
        peak, dd_usd = float("-inf"), 0.0
        for p in pnl_curve:
            peak = max(peak, p)
            dd_usd = min(dd_usd, p - peak)
    return Stats(
        n_closed=len(nets), n_wins=len(wins), n_losses=len(losses),
        n_breakeven=len(nets) - len(wins) - len(losses),
        gross_win=sum(wins, ZERO), gross_loss=sum(losses, ZERO),
        largest_win=max(wins) if wins else None, largest_loss=min(losses) if losses else None,
        net_pnl=pnl_curve[-1] - pnl_curve[0] if pnl_curve else None,
        balance=src.equity[-1][1] if src.equity and capital > 0 else None,
        max_drawdown_pct=dd_pct, max_drawdown_usd=dd_usd,
    )


# ---------------------------------------------------------------------------
# El calendario
# ---------------------------------------------------------------------------
@dataclass
class Week:
    # Domingo..sábado; None = un día de otro mes.
    days: list[DayPnl | None] = field(default_factory=list)
    pnl: float = 0.0
    active: int = 0


def month_weeks(year: int, month: int, daily: dict[date, DayPnl]) -> list[Week]:
    weeks = []
    for row in calendar.Calendar(firstweekday=6).monthdatescalendar(year, month):
        w = Week()
        for d in row:
            if d.month != month:
                w.days.append(None)
                continue
            cell = daily.get(d, DayPnl(d))
            w.days.append(cell)
            if d in daily:
                w.pnl += cell.pnl
                w.active += 1
        weeks.append(w)
    return weeks


def month_total(daily: dict[date, DayPnl], year: int, month: int) -> tuple[float, int]:
    days = [v for d, v in daily.items() if d.year == year and d.month == month]
    return sum(v.pnl for v in days), len(days)


def pnl_by_hour(rows: list[trades.TradeRow]) -> list[tuple[str, float]]:
    """Neto de las cerradas por hora de ENTRADA (UTC), las 24 horas: cuándo entran las
    que ganan. Sin ninguna cerrada, vacío."""
    acc: dict[int, float] = defaultdict(float)
    for t in rows:
        if not t.is_open and t.net_pnl is not None:
            acc[t.entry_ts.astimezone(UTC).hour] += float(t.net_pnl)
    return [(f"{h:02d}h", acc.get(h, 0.0)) for h in range(24)] if acc else []


def sleeves_with_activity(conn: Connection) -> list[str]:
    return sorted({f.strategy_id for f in fetch_fills(conn)} | {
        r.strategy_id for r in conn.execute(text("SELECT DISTINCT strategy_id FROM equity_curve"))})
