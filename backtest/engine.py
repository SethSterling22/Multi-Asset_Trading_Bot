"""Motor de backtest: una estrategia, un rango de fechas, barras diarias.

Ejecuta el MISMO código que en vivo (la clase de la estrategia, con sus guards de
caja y holdings) contra un broker simulado con comisión y slippage, para que
backtest y live sean comparables. Las métricas salen de core/metrics.py.

Sin look-ahead: la decisión usa el cierre del día t y se ejecuta a la apertura
del día t+1. La equity se marca a cada cierre.

No se simulan los controles operacionales (kill switch, pérdida diaria): miden
el estado del sistema, no la lógica de decisión, y en barras diarias no hay
intradía con el que evaluarlos. Lo que se mide es la estrategia.

Cada ejecución se registra en el SearchLog (N): también las descartadas.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from backtest.registry import SearchLog
from core import log
from core.config.models import TradingConfig
from core.ledger.equity import EquitySnapshot
from core.ledger.fills import Fill
from core.metrics import DAILY_CRYPTO, cagr, max_drawdown, returns
from core.stats.deflated_sharpe import moments
from risk.breakers import KillSwitch
from strategies.base import StaticParams
from strategies.crypto_rebalance.rebalancer import (
    STABLE_SYMBOL,
    CryptoRebalancer,
    PaperSpotBroker,
)


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float
    close: float


Bars = Mapping[str, Sequence[Bar]]


@dataclass(frozen=True, slots=True)
class BacktestFill:
    """Una operación simulada: precio con slippage, comisión en USD y el motivo del plan.
    En una compra, `qty` es lo recibido: (nocional − comisión) / precio."""

    ts: datetime
    symbol: str
    side: str
    qty: float
    price: float
    commission: float
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    strategy_id: str
    params: dict[str, Any]
    params_hash: str
    days: list[date]
    equity: list[float]
    returns: list[float]
    fees_usd: float
    traded_notional_usd: float
    n_orders: int
    discarded: bool
    discard_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    fills: tuple[BacktestFill, ...] = ()
    final_marks: dict[str, float] = field(default_factory=dict)

    @property
    def sharpe_per_period(self) -> float | None:
        """Solo para el registro de N y el DSR. El informe no lo imprime."""
        if self.discarded:
            return None
        return moments(self.returns).sharpe


class CostedSpotBroker(PaperSpotBroker):
    """PaperSpotBroker con comisión y slippage adverso, ambos en bps del nocional."""

    def __init__(self, balances: dict[str, float], prices: dict[str, float], *,
                 fee_bps: float, slippage_bps: float) -> None:
        super().__init__(balances, prices)
        if fee_bps < 0 or slippage_bps < 0:
            raise ValueError("fee_bps y slippage_bps >= 0")
        self.fee = fee_bps / 10_000
        self.slip = slippage_bps / 10_000
        self.fees_usd = 0.0
        self.traded_usd = 0.0
        self.n_orders = 0
        self.fills: list[BacktestFill] = []
        self.now = datetime(1970, 1, 1, tzinfo=UTC)     # el motor lo fija en cada ejecución

    def set_prices(self, prices: Mapping[str, float]) -> None:
        self._prices = dict(prices)

    def market_buy(self, symbol: str, notional_usd: float, reason: str = "") -> str:
        # La comisión sale del nocional: con 100 USDC se compran 100·(1−fee) de activo.
        price = self._prices[symbol] * (1 + self.slip)
        if notional_usd > self._balances.get(STABLE_SYMBOL, 0.0) + 1e-9:
            raise ValueError(f"insufficient {STABLE_SYMBOL} for {notional_usd}")
        fee = notional_usd * self.fee
        qty = (notional_usd - fee) / price
        self._balances[symbol] = self._balances.get(symbol, 0.0) + qty
        self._balances[STABLE_SYMBOL] -= notional_usd
        self._account(notional_usd, fee)
        self.fills.append(BacktestFill(self.now, symbol, "buy", qty, price, fee, reason))
        return f"bt-buy-{self.n_orders}"

    def market_sell(self, symbol: str, quantity: float, reason: str = "") -> str:
        if quantity > self._balances.get(symbol, 0.0) + 1e-12:
            raise ValueError(f"insufficient {symbol} for {quantity}")
        price = self._prices[symbol] * (1 - self.slip)
        proceeds = quantity * price
        fee = proceeds * self.fee
        self._balances[symbol] -= quantity
        self._balances[STABLE_SYMBOL] = self._balances.get(STABLE_SYMBOL, 0.0) + proceeds - fee
        self._account(proceeds, fee)
        self.fills.append(BacktestFill(self.now, symbol, "sell", quantity, price, fee, reason))
        return f"bt-sell-{self.n_orders}"

    def _account(self, notional: float, fee: float) -> None:
        self.fees_usd += fee
        self.traded_usd += notional
        self.n_orders += 1

    def value(self, prices: Mapping[str, float]) -> float:
        return sum(q * (1.0 if s == STABLE_SYMBOL else prices[s])
                   for s, q in self._balances.items())


class _NoLedger:
    def record_fill(self, fill: Fill) -> bool:
        return True

    def record_equity(self, snapshot: EquitySnapshot) -> bool:
        return True


class _NoLossStop:
    """La equity de referencia es la actual: la pérdida diaria no se simula (ver arriba)."""

    def __init__(self, broker: CostedSpotBroker) -> None:
        self._broker = broker

    def day_start_equity(self, strategy_id: str, now: datetime) -> Decimal:
        return Decimal(str(self._broker.value(self._broker._prices)))

    week_start_equity = day_start_equity


def aligned_days(bars: Bars, symbols: Sequence[str]) -> list[date]:
    """Los días comunes a todos los símbolos; un hueco en uno es un error, no se rellena."""
    day_sets = [[b.day for b in bars[s]] for s in symbols]
    first = day_sets[0]
    for s, days in zip(symbols, day_sets, strict=True):
        if days != first:
            missing = sorted(set(first) ^ set(days))[:5]
            raise ValueError(f"{s}: días distintos del resto (p.ej. {missing})")
    return first


def run_crypto_rebalance(
    config: TradingConfig,
    bars: Bars,
    *,
    initial_usdc: float,
    fee_bps: float,
    slippage_bps: float,
    registry: SearchLog,
) -> BacktestResult:
    symbols = list(config.crypto_parameters.monitored_assets)
    days = aligned_days(bars, symbols)
    if len(days) < 3:
        raise ValueError("hacen falta al menos 3 barras")
    closes = [{s: bars[s][i].close for s in symbols} for i in range(len(days))]
    opens = [{s: bars[s][i].open for s in symbols} for i in range(len(days))]

    params: dict[str, Any] = {
        "strategy": "crypto_rebalance",
        "crypto_parameters": config.crypto_parameters.model_dump(),
        "window": [days[0].isoformat(), days[-1].isoformat()],
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "initial_usdc": initial_usdc,
        "execution": "decide_at_close_t_execute_at_open_t+1",
    }

    broker = CostedSpotBroker({STABLE_SYMBOL: initial_usdc}, closes[0],
                              fee_bps=fee_bps, slippage_bps=slippage_bps)
    bot = CryptoRebalancer(broker)
    with open(os.devnull, "w") as quiet:
        log.configure("backtest", stream=quiet)
        bot.setup_base(
            params=StaticParams(config),
            kill_switch=KillSwitch(risk_events=lambda _: False, redis=lambda _: False,
                                   halt_file=lambda _: False),
            ledger=_NoLedger(),
            loss_reference=_NoLossStop(broker),
            clock=lambda: datetime.now(UTC),
        )
        equity = [broker.value(closes[0])]
        for t in range(len(days) - 1):
            broker.set_prices(closes[t])
            orders = bot.plan()                       # decisión al cierre de t
            broker.set_prices(opens[t + 1])           # ejecución a la apertura de t+1
            broker.now = datetime(days[t + 1].year, days[t + 1].month, days[t + 1].day,
                                  tzinfo=UTC)
            for order in orders:
                if order.side == "sell":
                    broker.market_sell(order.symbol, order.quantity, reason=order.reason)
            for order in orders:
                if order.side == "buy":
                    cash = broker.get_balances().get(STABLE_SYMBOL, 0.0)
                    notional = min(order.notional_usd, cash)
                    if notional > 0:
                        broker.market_buy(order.symbol, notional, reason=order.reason)
            equity.append(broker.value(closes[t + 1]))

    rets = returns(equity)
    discarded = max(rets) == min(rets)
    result = BacktestResult(
        strategy_id="crypto_rebalance",
        params=params,
        params_hash="",
        days=days,
        equity=equity,
        returns=rets,
        fees_usd=broker.fees_usd,
        traded_notional_usd=broker.traded_usd,
        n_orders=broker.n_orders,
        discarded=discarded,
        discard_reason="retornos constantes: sin Sharpe definido" if discarded else None,
        fills=tuple(broker.fills),
        final_marks=dict(closes[-1]),
    )
    outcome = None if discarded else {
        "sharpe_per_period": result.sharpe_per_period,
        "n_obs": len(rets),
        "final_equity": equity[-1],
        "max_drawdown": max_drawdown(equity),
        "cagr": cagr(equity, DAILY_CRYPTO),
    }
    h = registry.record("crypto_rebalance", params, outcome, discarded)
    return BacktestResult(**{**result.__dict__, "params_hash": h})


def buy_and_hold(bars: Sequence[Bar], *, initial_usdc: float, fee_bps: float,
                 slippage_bps: float) -> list[float]:
    """Benchmark: comprar a la apertura del segundo día (como cualquier decisión) y mantener."""
    entry = bars[1].open * (1 + slippage_bps / 10_000)
    qty = initial_usdc * (1 - fee_bps / 10_000) / entry
    return [initial_usdc] + [qty * b.close for b in bars[1:]]


def static_weights(bars: Bars, weights: Mapping[str, float], *, initial_usdc: float,
                   fee_bps: float, slippage_bps: float) -> list[float]:
    """Benchmark: los pesos objetivo comprados el segundo día y nunca rebalanceados.

    Aísla lo que aporta el rebalanceo frente a la simple asignación. El peso que no
    va a ningún activo se queda en caja, sin rendimiento.
    """
    days = len(next(iter(bars.values())))
    qty = {
        s: initial_usdc * w * (1 - fee_bps / 10_000) / (bars[s][1].open * (1 + slippage_bps
                                                                            / 10_000))
        for s, w in weights.items()
    }
    cash = initial_usdc * (1 - sum(weights.values()))
    return [initial_usdc] + [cash + sum(q * bars[s][i].close for s, q in qty.items())
                             for i in range(1, days)]


def is_finite_curve(curve: Sequence[float]) -> bool:
    return all(math.isfinite(x) and x > 0 for x in curve)
