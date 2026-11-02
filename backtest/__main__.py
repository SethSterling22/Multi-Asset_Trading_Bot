"""Backtest de crypto_rebalance con barrido de parámetros, DSR, PBO y N.

    uv run python -m backtest --start 2023-11-01 --end 2026-10-31 \\
        --fee-bps 60 --slippage-bps 5 --database-url "$DATABASE_URL"

Comisión y slippage son obligatorios y no tienen valor por defecto: son supuestos
sobre el venue, y el informe los imprime. N se registra en `search_log` de la base
de datos (obligatoria): un registro en memoria olvidaría las pruebas de la sesión
anterior y deflactaría de menos.
"""

from __future__ import annotations

import argparse
import itertools
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine

from backtest.cscv import probability_of_backtest_overfitting
from backtest.data import load_daily
from backtest.engine import BacktestResult, buy_and_hold, run_crypto_rebalance, static_weights
from backtest.registry import PostgresSearchLog
from backtest.report import render
from core.config.models import CONFIG_PATH, TradingConfig

# Barrido: 5 umbrales × 4 asignaciones a USDC = 20 configuraciones. Todas cuentan en N.
THRESHOLDS = (0.02, 0.05, 0.10, 0.15, 0.20)
STABLE_ALLOCATIONS = (0.4, 0.5, 0.6, 0.7)
CACHE = Path(".cache/backtest")


def crypto_weights(result: BacktestResult) -> dict[str, float]:
    """Pesos objetivo de los activos cripto del candidato (el resto queda en USDC)."""
    p = result.params["crypto_parameters"]
    per_asset = p["crypto_target_allocation"] / len(p["monitored_assets"])
    return {s: per_asset for s in p["monitored_assets"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--fee-bps", type=float, required=True)
    ap.add_argument("--slippage-bps", type=float, required=True)
    ap.add_argument("--initial-usdc", type=float, default=10_000.0)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    base = TradingConfig.load(CONFIG_PATH)
    symbols = base.crypto_parameters.monitored_assets
    bars = {s: load_daily(f"{s}-USD", args.start, args.end, CACHE) for s in symbols}
    registry = PostgresSearchLog(create_engine(args.database_url))

    sweep = []
    for threshold, stable in itertools.product(THRESHOLDS, STABLE_ALLOCATIONS):
        cfg = base.model_copy(update={"crypto_parameters": base.crypto_parameters.model_copy(
            update={"rebalance_threshold": threshold, "stable_target_allocation": stable,
                    "crypto_target_allocation": round(1 - stable, 10)})})
        sweep.append(run_crypto_rebalance(cfg, bars, initial_usdc=args.initial_usdc,
                                          fee_bps=args.fee_bps,
                                          slippage_bps=args.slippage_bps, registry=registry))

    valid = [r for r in sweep if not r.discarded]
    # El candidato es el mejor in-sample: exactamente la elección que el DSR deflacta.
    candidate = max(valid, key=lambda r: r.sharpe_per_period or float("-inf"))
    pbo = probability_of_backtest_overfitting([r.returns for r in valid])
    report = render(
        candidate, registry, sweep=sweep, pbo=pbo,
        benchmark_name="BTC buy-and-hold",
        benchmark_equity=buy_and_hold(bars["BTC"], initial_usdc=args.initial_usdc,
                                      fee_bps=args.fee_bps, slippage_bps=args.slippage_bps),
        static_equity=static_weights(bars, crypto_weights(candidate),
                                     initial_usdc=args.initial_usdc, fee_bps=args.fee_bps,
                                     slippage_bps=args.slippage_bps),
    )
    print(report)
    if args.out:
        args.out.write_text(report + "\n")


main()
