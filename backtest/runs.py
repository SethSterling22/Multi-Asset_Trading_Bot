"""Guardar cada configuración del barrido para verla en el dashboard (D-017).

Por corrida: sus parámetros, su curva diaria, sus operaciones con el motivo del plan,
el N registrado en ese momento y su Deflated Sharpe Ratio (deflactado con TODAS las
pruebas registradas, no solo las de esta sesión). Una configuración ya guardada se
reemplaza: los mismos parámetros sobre los mismos datos dan la misma corrida, y si el
motor cambió, lo que vale es la última versión.

El dashboard nunca enseña el Sharpe de una corrida: enseña su DSR y su N (§1.11).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import Engine, text

from backtest.engine import BacktestResult
from backtest.registry import SearchLog
from core.stats.deflated_sharpe import deflated_sharpe


def save_runs(engine: Engine, results: Sequence[BacktestResult], *, registry: SearchLog,
              candidate_hash: str | None) -> int:
    if not results:
        return 0
    trials = registry.trials(results[0].strategy_id)
    sharpes = [t.sharpe for t in trials if t.sharpe is not None]
    with engine.begin() as conn:
        for r in results:
            dsr = None if r.discarded else deflated_sharpe(r.returns, sharpes,
                                                           n_trials=len(trials)).dsr
            conn.execute(text("DELETE FROM backtest_runs WHERE params_hash = :h"),
                         {"h": r.params_hash})
            run_id = conn.execute(text("""
                INSERT INTO backtest_runs (strategy_id, params_hash, params, window_start,
                    window_end, initial_capital, final_equity, fees_usd, n_orders, discarded,
                    n_trials, dsr, is_candidate, final_marks)
                VALUES (:s, :h, CAST(:p AS jsonb), :ws, :we, :ic, :fe, :fees, :n, :disc,
                        :nt, :dsr, :cand, CAST(:marks AS jsonb))
                RETURNING id
            """), {"s": r.strategy_id, "h": r.params_hash,
                   "p": json.dumps(r.params, sort_keys=True, default=str),
                   "ws": r.days[0], "we": r.days[-1], "ic": r.equity[0],
                   "fe": r.equity[-1], "fees": r.fees_usd, "n": r.n_orders,
                   "disc": r.discarded, "nt": len(trials), "dsr": dsr,
                   "cand": r.params_hash == candidate_hash,
                   "marks": json.dumps(r.final_marks)}).scalar_one()
            conn.execute(text(
                "INSERT INTO backtest_equity (run_id, day, equity) VALUES (:r, :d, :e)"),
                [{"r": run_id, "d": d, "e": e} for d, e in zip(r.days, r.equity, strict=True)])
            if r.fills:
                conn.execute(text("""
                    INSERT INTO backtest_fills (run_id, seq, ts, symbol, side, qty, price,
                                                commission, reason)
                    VALUES (:r, :i, :ts, :sym, :side, :q, :p, :c, :reason)
                """), [{"r": run_id, "i": i, "ts": f.ts, "sym": f.symbol, "side": f.side,
                        "q": f.qty, "p": f.price, "c": f.commission, "reason": f.reason}
                       for i, f in enumerate(r.fills)])
    return len(results)
