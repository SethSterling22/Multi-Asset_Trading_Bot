"""El motor de backtest: costes, sin look-ahead, registro de N y el informe."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from backtest.cscv import probability_of_backtest_overfitting
from backtest.data import check_continuous
from backtest.engine import (
    Bar,
    CostedSpotBroker,
    aligned_days,
    buy_and_hold,
    run_crypto_rebalance,
)
from backtest.registry import MemorySearchLog, params_hash
from backtest.report import NNotRegisteredError, numbers, render
from core.config.models import CONFIG_PATH, TradingConfig

D0 = date(2024, 1, 1)


def walk(seed: int, n: int, start: float, vol: float) -> list[Bar]:
    rng = random.Random(seed)
    bars, price = [], start
    for i in range(n):
        open_ = price
        price *= math.exp(rng.gauss(0, vol))
        bars.append(Bar(D0 + timedelta(days=i), open_, price))
    return bars


BARS = {"BTC": walk(1, 400, 40_000, 0.03), "ETH": walk(2, 400, 2_500, 0.04)}


def config(threshold: float = 0.05, stable: float = 0.6) -> TradingConfig:
    base = TradingConfig.load(CONFIG_PATH)
    return base.model_copy(update={"crypto_parameters": base.crypto_parameters.model_copy(
        update={"rebalance_threshold": threshold, "stable_target_allocation": stable,
                "crypto_target_allocation": round(1 - stable, 10),
                "monitored_assets": ["BTC", "ETH"]})})


def run(registry: MemorySearchLog, **kw: float) -> object:
    return run_crypto_rebalance(config(**kw), BARS, initial_usdc=10_000, fee_bps=60,
                                slippage_bps=5, registry=registry)


def test_costs_are_charged_on_both_sides() -> None:
    # Compra de 1,000 con 60 bps: llegan 994 de activo, a precio con +5 bps.
    b = CostedSpotBroker({"USDC": 1000.0, "BTC": 0.0}, {"BTC": 100.0},
                         fee_bps=60, slippage_bps=5)
    b.market_buy("BTC", 1000.0)
    assert b.get_balances()["BTC"] == pytest.approx(994 / 100.05)
    assert b.fees_usd == pytest.approx(6.0)
    qty = b.get_balances()["BTC"]
    b.market_sell("BTC", qty)
    proceeds = qty * 100 * (1 - 0.0005)
    assert b.get_balances()["USDC"] == pytest.approx(proceeds * (1 - 0.006))
    with pytest.raises(ValueError):
        b.market_buy("BTC", 2000.0)            # quedan ~993 USDC: no hay margen
    with pytest.raises(ValueError):
        b.market_sell("BTC", 1.0)              # ya no queda BTC
    with pytest.raises(ValueError):
        CostedSpotBroker({}, {}, fee_bps=-1, slippage_bps=0)


def test_rebalancer_runs_the_live_code_and_is_registered() -> None:
    registry = MemorySearchLog()
    result = run(registry)
    assert len(result.equity) == len(BARS["BTC"])           # type: ignore[attr-defined]
    assert result.n_orders > 0 and result.fees_usd > 0      # type: ignore[attr-defined]
    (trial,) = registry.trials("crypto_rebalance")
    assert trial.params_hash == result.params_hash          # type: ignore[attr-defined]
    assert trial.sharpe == pytest.approx(result.sharpe_per_period)  # type: ignore[attr-defined]


def test_same_config_does_not_count_twice_but_any_change_does() -> None:
    registry = MemorySearchLog()
    run(registry)
    run(registry)
    assert len(registry.trials("crypto_rebalance")) == 1
    run(registry, threshold=0.10)
    assert len(registry.trials("crypto_rebalance")) == 2
    # Los supuestos de coste también son parte de la configuración.
    run_crypto_rebalance(config(), BARS, initial_usdc=10_000, fee_bps=10, slippage_bps=5,
                         registry=registry)
    assert len(registry.trials("crypto_rebalance")) == 3


def test_no_look_ahead_decision_at_close_execution_next_open() -> None:
    # Si el último día sube un 50% en el cierre, una estrategia con look-ahead lo
    # aprovecharía; esta no puede, porque decide al cierre y ejecuta al día siguiente.
    bars = {k: list(v) for k, v in BARS.items()}
    last = bars["BTC"][-1]
    bars["BTC"][-1] = Bar(last.day, last.open, last.open * 1.5)
    a = run_crypto_rebalance(config(), BARS, initial_usdc=10_000, fee_bps=60,
                             slippage_bps=5, registry=MemorySearchLog())
    b = run_crypto_rebalance(config(), bars, initial_usdc=10_000, fee_bps=60,
                             slippage_bps=5, registry=MemorySearchLog())
    assert a.n_orders == b.n_orders and a.fees_usd == pytest.approx(b.fees_usd)


def test_misaligned_or_gapped_data_is_an_error() -> None:
    gapped = {"BTC": BARS["BTC"][:10] + BARS["BTC"][11:20], "ETH": BARS["ETH"][:19]}
    with pytest.raises(ValueError):
        aligned_days(gapped, ["BTC", "ETH"])
    with pytest.raises(ValueError):
        check_continuous(BARS["BTC"][:10] + BARS["BTC"][11:20], D0, D0 + timedelta(days=19))
    check_continuous(BARS["BTC"][:20], D0, D0 + timedelta(days=19))


def test_buy_and_hold_benchmark() -> None:
    curve = buy_and_hold(BARS["BTC"], initial_usdc=10_000, fee_bps=0, slippage_bps=0)
    assert curve[-1] == pytest.approx(10_000 * BARS["BTC"][-1].close / BARS["BTC"][1].open)


def test_params_hash_is_order_independent() -> None:
    assert params_hash({"a": 1, "b": [2, 3]}) == params_hash({"b": [2, 3], "a": 1})


# --- El informe ----------------------------------------------------------------
def _sweep(registry: MemorySearchLog) -> list:  # type: ignore[type-arg]
    return [run(registry, threshold=t, stable=s) for t in (0.05, 0.10) for s in (0.5, 0.6)]


def test_report_refuses_without_registered_n() -> None:
    sweep = _sweep(MemorySearchLog())
    with pytest.raises(NNotRegisteredError, match="no está registrado"):
        numbers(sweep[0], MemorySearchLog(), None)
    other = MemorySearchLog()
    run(other, threshold=0.2)
    with pytest.raises(NNotRegisteredError, match="candidato"):
        numbers(sweep[0], other, None)


def test_report_prints_dsr_pbo_and_n_but_never_sharpe() -> None:
    registry = MemorySearchLog()
    sweep = _sweep(registry)
    pbo = probability_of_backtest_overfitting([r.returns for r in sweep], n_blocks=8)
    text = render(sweep[0], registry, sweep=sweep, pbo=pbo, benchmark_name="BTC HODL",
                  benchmark_equity=buy_and_hold(BARS["BTC"], initial_usdc=10_000,
                                                fee_bps=60, slippage_bps=5))
    assert "N registrado: 4" in text
    assert "Deflated Sharpe Ratio:" in text and "PBO:" in text
    # "Sharpe" solo puede aparecer como parte de "Deflated Sharpe" o en la nota final.
    for line in text.splitlines():
        if "Sharpe" in line:
            assert "Deflated Sharpe" in line or line.startswith("Sin Sharpe")


def test_dsr_is_deflated_by_every_registered_trial() -> None:
    registry = MemorySearchLog()
    sweep = _sweep(registry)
    alone = MemorySearchLog()
    run(alone)
    few = numbers(run(alone), alone, None)          # N = 1
    many = numbers(sweep[0], registry, None)        # N = 4, mismo candidato
    assert few.n_registered == 1 and many.n_registered == 4
    assert many.dsr.threshold > few.dsr.threshold == 0.0


def test_static_weights_benchmark_keeps_the_rest_in_cash() -> None:
    from backtest.engine import static_weights
    curve = static_weights(BARS, {"BTC": 0.2, "ETH": 0.2}, initial_usdc=10_000,
                           fee_bps=0, slippage_bps=0)
    btc = 2_000 * BARS["BTC"][-1].close / BARS["BTC"][1].open
    eth = 2_000 * BARS["ETH"][-1].close / BARS["ETH"][1].open
    assert curve[0] == 10_000
    assert curve[-1] == pytest.approx(6_000 + btc + eth)
