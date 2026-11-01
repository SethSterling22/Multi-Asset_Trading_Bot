"""Métricas de rendimiento. El mismo código para backtest y live.

Rescatado de `compute_metrics` en app_dashboard.py, con dos correcciones:
- La anualización es explícita (`periods_per_year`). El original asumía 252
  filas diarias para una curva de frecuencia desconocida; cripto opera 365 días
  y una curva de 15 min tiene otra frecuencia.
- Una métrica que no se puede calcular (desviación cero, muy pocos puntos) es
  `None`, no 0.0: un cero se confunde con un resultado real.

El Sharpe de aquí es el de UNA serie. El informe de backtest no lo imprime sin
`N` registrado; ahí va el Deflated Sharpe (CLAUDE.md §1.11).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean, stdev

DAILY_EQUITIES = 252
DAILY_CRYPTO = 365


def returns(equity: Sequence[float]) -> list[float]:
    """Retornos simples período a período."""
    if any(e <= 0 for e in equity):
        raise ValueError("la equity debe ser > 0 para calcular retornos")
    return [b / a - 1.0 for a, b in itertools.pairwise(equity)]


def sharpe(rets: Sequence[float], periods_per_year: int) -> float | None:
    if len(rets) < 2:
        return None
    sd = stdev(rets)
    if sd == 0:
        return None
    return fmean(rets) / sd * math.sqrt(periods_per_year)


def sortino(rets: Sequence[float], periods_per_year: int) -> float | None:
    """Como el Sharpe, pero con la desviación de los retornos negativos (objetivo 0)."""
    if len(rets) < 2:
        return None
    downside = math.sqrt(fmean([min(r, 0.0) ** 2 for r in rets]))
    if downside == 0:
        return None
    return fmean(rets) / downside * math.sqrt(periods_per_year)


def max_drawdown(equity: Sequence[float]) -> float:
    """Máximo drawdown como fracción negativa (−0.25 = −25%). 0.0 si nunca cae."""
    peak = -math.inf
    worst = 0.0
    for e in equity:
        peak = max(peak, e)
        worst = min(worst, e / peak - 1.0)
    return worst


def cagr(equity: Sequence[float], periods_per_year: int) -> float | None:
    if len(equity) < 2 or equity[0] <= 0:
        return None
    years = (len(equity) - 1) / periods_per_year
    return float((equity[-1] / equity[0]) ** (1.0 / years)) - 1.0


def calmar(equity: Sequence[float], periods_per_year: int) -> float | None:
    growth = cagr(equity, periods_per_year)
    dd = max_drawdown(equity)
    if growth is None or dd == 0:
        return None
    return growth / abs(dd)


@dataclass(frozen=True, slots=True)
class EquityMetrics:
    balance: float
    pnl_last_period: float
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    calmar: float | None


def equity_metrics(equity: Sequence[float], periods_per_year: int) -> EquityMetrics:
    if not equity:
        raise ValueError("curva de equity vacía")
    rets = returns(equity)
    return EquityMetrics(
        balance=equity[-1],
        pnl_last_period=equity[-1] - equity[-2] if len(equity) > 1 else 0.0,
        sharpe=sharpe(rets, periods_per_year),
        sortino=sortino(rets, periods_per_year),
        max_drawdown=max_drawdown(equity),
        calmar=calmar(equity, periods_per_year),
    )


@dataclass(frozen=True, slots=True)
class TradeStats:
    n: int
    win_rate: float
    avg_r: float              # media de los múltiplos de R
    expectancy_usd: float     # P&L medio por operación
    avg_win_usd: float | None
    avg_loss_usd: float | None


def trade_stats(pnl: Sequence[float], risk: Sequence[float]) -> TradeStats:
    """Estadísticas por operación cerrada.

    `pnl` es el P&L neto de cada operación y `risk` lo que se arriesgaba en ella
    (distancia al stop × tamaño), así que R = pnl / risk.
    """
    if len(pnl) != len(risk):
        raise ValueError("pnl y risk deben tener la misma longitud")
    if not pnl:
        raise ValueError("sin operaciones")
    if any(r <= 0 for r in risk):
        raise ValueError("el riesgo de cada operación debe ser > 0")
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    return TradeStats(
        n=len(pnl),
        win_rate=len(wins) / len(pnl),
        avg_r=fmean([p / r for p, r in zip(pnl, risk, strict=True)]),
        expectancy_usd=fmean(pnl),
        avg_win_usd=fmean(wins) if wins else None,
        avg_loss_usd=fmean(losses) if losses else None,
    )
