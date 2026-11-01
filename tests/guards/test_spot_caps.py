"""Grupos 5 y 6 de CLAUDE.md §1.8: venta cripto > holdings, compra > USDC."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from strategies.base import cap_spot_buys, cap_spot_sell
from strategies.crypto_rebalance.rebalancer import CryptoRebalancer, PaperSpotBroker
from tests.guards.helpers import Events, FixedLossReference, setup, write_config

pytestmark = pytest.mark.guards


def test_sell_is_capped_at_holdings() -> None:
    assert cap_spot_sell(quantity=D(5), holdings=D(2)) == D(2)
    assert cap_spot_sell(quantity=D(1), holdings=D(2)) == D(1)
    assert cap_spot_sell(quantity=D(1), holdings=D(0)) == 0
    assert cap_spot_sell(quantity=D(1), holdings=D(-3)) == 0      # nunca corto


def test_buys_are_capped_in_aggregate() -> None:
    # Dos compras de 1003 con 1140 de USDC: la vieja daba 1003 + 1003 = 2006.
    out = cap_spot_buys([("BTC", D(1003)), ("ETH", D(1003))], D(1140), min_notional=D(10))
    assert out == [("BTC", D(1003)), ("ETH", D(137))]
    assert sum(n for _, n in out) <= D(1140)


def test_buys_below_minimum_are_dropped() -> None:
    out = cap_spot_buys([("BTC", D(1000)), ("ETH", D(500))], D(1005), min_notional=D(10))
    assert out == [("BTC", D(1000))]          # quedarían 5 para ETH: < 10, se descarta
    assert cap_spot_buys([("BTC", D(100))], D(-50), min_notional=D(10)) == []


def _rebalancer(tmp_path: Path, broker: PaperSpotBroker) -> CryptoRebalancer:
    cfg = write_config(tmp_path, **{
        "crypto_parameters.monitored_assets": ["BTC", "ETH", "SOL"],
        "crypto_parameters.stable_target_allocation": 0.0,
        "crypto_parameters.crypto_target_allocation": 1.0,
        "crypto_parameters.rebalance_threshold": 0.10,
    })
    bot = CryptoRebalancer(broker)
    setup(bot, cfg, loss=FixedLossReference(day=D(10_000), week=D(10_000)))
    return bot


def test_rebalancer_never_buys_more_than_the_usdc_it_has(tmp_path: Path) -> None:
    # Valor 10,000: SOL 4,200 (sobrepeso de 8.7%, bajo el umbral: no se vende),
    # BTC 2,330 y ETH 2,330 (−10.03% cada uno: hay que comprar ~1,003 de cada uno),
    # USDC 1,140. La suma de compras deseadas (2,006) no cabe en la caja.
    broker = PaperSpotBroker(
        balances={"BTC": 0.0233, "ETH": 0.5825, "SOL": 21.0, "USDC": 1140.0},
        prices={"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0},
    )
    bot = _rebalancer(tmp_path, broker)
    buys = [o for o in bot.plan() if o.side == "buy"]
    assert len(buys) == 2
    assert sum(o.notional_usd for o in buys) == pytest.approx(1140.0)
    bot.execute(dry_run=False)                 # el broker rechazaría un sobregasto
    assert broker.get_balances()["USDC"] == pytest.approx(0.0, abs=1e-6)


def test_rebalancer_never_sells_more_than_held(tmp_path: Path) -> None:
    broker = PaperSpotBroker(
        balances={"BTC": 0.09, "ETH": 0.0, "SOL": 0.0, "USDC": 1000.0},
        prices={"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0},
    )
    bot = _rebalancer(tmp_path, broker)
    sells = [o for o in bot.plan() if o.side == "sell"]
    assert sells and all(o.quantity <= 0.09 for o in sells)
    bot.execute(dry_run=False)
    assert all(v >= 0 for v in broker.get_balances().values())


def test_sells_execute_before_buys(tmp_path: Path) -> None:
    broker = PaperSpotBroker(
        balances={"BTC": 0.09, "ETH": 0.0, "SOL": 0.0, "USDC": 1000.0},
        prices={"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0},
    )
    sides = [o.side for o in _rebalancer(tmp_path, broker).plan()]
    assert sides == sorted(sides, key=lambda s: s != "sell")


def test_paper_broker_refuses_overspend_and_oversell() -> None:
    broker = PaperSpotBroker({"BTC": 0.1, "USDC": 100.0}, {"BTC": 100_000.0})
    with pytest.raises(ValueError):
        broker.market_buy("BTC", 101.0)
    with pytest.raises(ValueError):
        broker.market_sell("BTC", 0.2)


def test_rebalancer_respects_the_gate(tmp_path: Path) -> None:
    events = Events()
    broker = PaperSpotBroker({"BTC": 0.3, "ETH": 2.0, "SOL": 0.0, "USDC": 5000.0},
                             {"BTC": 68_000.0, "ETH": 3_500.0, "SOL": 200.0})
    bot = _rebalancer(tmp_path, broker)
    bot._loss_reference = FixedLossReference(day=None)
    assert bot.plan() == []
    assert [e["reason"] for e in events.rejections()] == ["loss_limit_unknown"]


def test_empty_portfolio_is_logged(tmp_path: Path) -> None:
    events = Events()
    broker = PaperSpotBroker({"USDC": 0.0}, {"BTC": 1.0, "ETH": 1.0, "SOL": 1.0})
    assert _rebalancer(tmp_path, broker).plan() == []
    assert [e["reason"] for e in events.rejections()] == ["empty_portfolio"]
