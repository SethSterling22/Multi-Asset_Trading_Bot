"""Informe de backtest. NO imprime Sharpe (CLAUDE.md §1.11, informe §2.4).

Imprime el Deflated Sharpe Ratio (deflactado por el N registrado, incluidas las
descartadas), el PBO del barrido y N. Si N no está registrado, o el candidato no
está entre las pruebas registradas, falla con NNotRegisteredError en lugar de
imprimir un número sin contexto.

Un Sharpe sin N es incalculable como evidencia: el mejor de 1,000 variantes sin
alpha da Sharpe 2.30 sobre 2 años de datos diarios por puro ruido.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backtest.cscv import PBO
from backtest.engine import BacktestResult
from backtest.registry import SearchLog
from core.metrics import DAILY_CRYPTO, cagr, max_drawdown
from core.stats.deflated_sharpe import DeflatedSharpe, deflated_sharpe

# Umbral convencional de significación para el DSR (probabilidad).
DSR_SIGNIFICANT = 0.95
PBO_GATE = 0.5           # informe §10, Fase 2.5


class NNotRegisteredError(RuntimeError):
    """No hay N registrado para la estrategia, o el candidato no está en él."""


@dataclass(frozen=True, slots=True)
class ReportNumbers:
    n_registered: int
    n_discarded: int
    dsr: DeflatedSharpe
    pbo: PBO | None


def numbers(candidate: BacktestResult, registry: SearchLog, pbo: PBO | None) -> ReportNumbers:
    trials = registry.trials(candidate.strategy_id)
    if not trials:
        raise NNotRegisteredError(
            f"{candidate.strategy_id}: N no está registrado; sin N no hay informe"
        )
    if candidate.params_hash not in {t.params_hash for t in trials}:
        raise NNotRegisteredError(
            f"{candidate.strategy_id}: el candidato {candidate.params_hash[:12]} no está "
            "entre las pruebas registradas"
        )
    if candidate.discarded:
        raise ValueError(f"el candidato está descartado: {candidate.discard_reason}")
    sharpes = [t.sharpe for t in trials if t.sharpe is not None]
    return ReportNumbers(
        n_registered=len(trials),
        n_discarded=sum(1 for t in trials if t.discarded),
        dsr=deflated_sharpe(candidate.returns, sharpes, n_trials=len(trials)),
        pbo=pbo,
    )


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def render(
    candidate: BacktestResult,
    registry: SearchLog,
    *,
    sweep: Sequence[BacktestResult],
    pbo: PBO | None,
    benchmark_name: str,
    benchmark_equity: Sequence[float],
    static_equity: Sequence[float] | None = None,
) -> str:
    n = numbers(candidate, registry, pbo)
    eq = candidate.equity
    years = (len(eq) - 1) / DAILY_CRYPTO
    turnover = candidate.traded_notional_usd / eq[0] / years if years > 0 else float("nan")
    bench_ret = benchmark_equity[-1] / benchmark_equity[0] - 1
    strat_ret = eq[-1] / eq[0] - 1
    p = candidate.params
    lines = [
        f"# Backtest · {candidate.strategy_id}",
        "",
        f"Ventana: {candidate.days[0]} → {candidate.days[-1]} ({len(candidate.days)} días)",
        f"Parámetros: {p.get('crypto_parameters')}",
        f"Supuestos de coste: comisión {p['fee_bps']} bps, slippage {p['slippage_bps']} bps"
        f" por lado · ejecución {p['execution']}",
        "",
        "## Evidencia (lo único que se puede leer como tal)",
        "",
        f"- **N registrado: {n.n_registered}** configuraciones para esta estrategia"
        f" ({n.n_discarded} descartadas), {len(sweep)} en este barrido",
        f"- **Deflated Sharpe Ratio: {n.dsr.dsr:.3f}**"
        f" (probabilidad de que el resultado supere el máximo esperado por azar con N ="
        f" {n.dsr.n_trials})",
    ]
    if n.pbo is not None:
        lines.append(
            f"- **PBO: {n.pbo.pbo:.3f}** sobre las {n.pbo.n_trials} configuraciones del"
            f" barrido ({n.pbo.n_combinations} combinaciones CSCV, {n.pbo.n_blocks} bloques)"
        )
    else:
        lines.append("- **PBO: no calculado** (hace falta un barrido de >= 2 configuraciones)")
    lines += [
        "",
        "## Lectura",
        "",
        f"- DSR {'>=' if n.dsr.dsr >= DSR_SIGNIFICANT else '<'} {DSR_SIGNIFICANT}: "
        + ("el resultado es distinguible del mejor de N por azar."
           if n.dsr.dsr >= DSR_SIGNIFICANT
           else "**no** es distinguible del mejor de N configuraciones por azar."),
    ]
    if n.pbo is not None:
        lines.append(
            f"- PBO {'<' if n.pbo.pbo < PBO_GATE else '>='} {PBO_GATE}: "
            + ("pasa el umbral del gate de la Fase 2. Cuanto más cerca de 0.5, más se"
               " parece a elegir la configuración al azar."
               if n.pbo.pbo < PBO_GATE
               else "**elegir el mejor in-sample no predice el out-of-sample**.")
        )
    lines += [
        "",
        "## Resultado (neto de comisión y slippage)",
        "",
        f"- Retorno total: {_pct(strat_ret)} · CAGR: {_pct(cagr(eq, DAILY_CRYPTO) or 0.0)}"
        f" · Max drawdown: {_pct(max_drawdown(eq))}",
        f"- Comisiones: ${candidate.fees_usd:,.2f} · Órdenes: {candidate.n_orders}"
        f" · Rotación: {turnover:.2f}× el capital inicial por año",
        f"- Benchmark {benchmark_name}: {_pct(bench_ret)}"
        f" · Max drawdown {_pct(max_drawdown(benchmark_equity))}"
        f" · Diferencia: {_pct(strat_ret - bench_ret)}",
    ]
    if static_equity is not None:
        static_ret = static_equity[-1] / static_equity[0] - 1
        lines.append(
            f"- Mismos pesos, comprados el primer día y SIN rebalancear: {_pct(static_ret)}"
            f" · Max drawdown {_pct(max_drawdown(static_equity))}"
            f" · Valor del rebalanceo: {_pct(strat_ret - static_ret)}"
        )
    lines += [
        "",
        "Sin Sharpe a propósito: sin deflactar por N no contiene información (CLAUDE.md §1.11).",
    ]
    return "\n".join(lines)
