"""Periodic crypto portfolio rebalancer (spot only). Ex crypto_rebalancer.py.

Target allocation (config.json):
  - stable_target_allocation  -> USDC (defensive cash cushion)
  - crypto_target_allocation  -> split equally across monitored_assets

Safety rules, enforced by strategies/base.py:
  - No leverage, no shorts: a sell never exceeds actual holdings, and the SUM of
    the buys never exceeds the USDC available when the plan is made (the old code
    capped each buy separately, so two buys could add up to more than the cash).
  - Sells are executed before buys.
  - The common gate (valid params, active, kill switch, daily/weekly loss) runs
    before planning; a rejected gate produces an empty plan and a logged reason.
  - Orders are generated only when the absolute weight deviation exceeds
    `rebalance_threshold`.

Broker access is abstracted behind the `SpotBroker` protocol so Alpaca Crypto or
Coinbase Advanced can be injected without touching the rebalancing logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar, Protocol

from core.config.models import TradingConfig
from strategies.base import Rejected, StrategyBase, cap_spot_buys, cap_spot_sell

STABLE_SYMBOL = "USDC"
MIN_ORDER_NOTIONAL_USD = Decimal(10)   # avoids micro-orders caused by price noise


# ---------------------------------------------------------------------- #
# Spot broker abstraction                                                 #
# ---------------------------------------------------------------------- #
class SpotBroker(Protocol):
    def get_balances(self) -> dict[str, float]:
        """Units per symbol, e.g. {'BTC': 0.5, 'ETH': 4.0, 'USDC': 12000}."""
        ...

    def get_price_usd(self, symbol: str) -> float:
        """Spot price in USD for the symbol."""
        ...

    def market_buy(self, symbol: str, notional_usd: float) -> str:
        """Spot buy by USD notional. Returns the order id."""
        ...

    def market_sell(self, symbol: str, quantity: float) -> str:
        """Spot sell by unit quantity. Returns the order id."""
        ...


class PaperSpotBroker:
    """In-memory simulated broker for development and quick backtests.

    Refuses what a real spot venue would refuse: selling more than held or buying
    with more USDC than available. The old version let balances go negative, so a
    test against it could not notice an overspend.
    """

    def __init__(self, balances: dict[str, float], prices: dict[str, float]):
        self._balances = dict(balances)
        self._prices = dict(prices)
        self._order_seq = 0

    def get_balances(self) -> dict[str, float]:
        return dict(self._balances)

    def get_price_usd(self, symbol: str) -> float:
        return self._prices[symbol]

    def market_buy(self, symbol: str, notional_usd: float) -> str:
        if notional_usd > self._balances.get(STABLE_SYMBOL, 0.0) + 1e-9:
            raise ValueError(f"insufficient {STABLE_SYMBOL} for {notional_usd}")
        qty = notional_usd / self._prices[symbol]
        self._balances[symbol] = self._balances.get(symbol, 0.0) + qty
        self._balances[STABLE_SYMBOL] -= notional_usd
        self._order_seq += 1
        return f"paper-buy-{self._order_seq}"

    def market_sell(self, symbol: str, quantity: float) -> str:
        if quantity > self._balances.get(symbol, 0.0) + 1e-12:
            raise ValueError(f"insufficient {symbol} for {quantity}")
        self._balances[symbol] -= quantity
        self._balances[STABLE_SYMBOL] = (
            self._balances.get(STABLE_SYMBOL, 0.0) + quantity * self._prices[symbol]
        )
        self._order_seq += 1
        return f"paper-sell-{self._order_seq}"


# ---------------------------------------------------------------------- #
# Rebalancing logic                                                       #
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class RebalanceOrder:
    symbol: str
    side: str            # "buy" | "sell"
    notional_usd: float  # target order notional
    quantity: float      # units (relevant for sells)
    reason: str


class CryptoRebalancer(StrategyBase):
    strategy_id: ClassVar[str] = "crypto_rebalance"

    def __init__(self, broker: SpotBroker) -> None:
        self.broker = broker

    # ------------------------- weight computation ---------------------- #
    def target_weights(self, cfg: TradingConfig) -> dict[str, float]:
        p = cfg.crypto_parameters
        per_asset = p.crypto_target_allocation / len(p.monitored_assets)
        weights = {sym: per_asset for sym in p.monitored_assets}
        weights[STABLE_SYMBOL] = p.stable_target_allocation
        return weights

    def current_state(self, cfg: TradingConfig) -> tuple[dict[str, float], float]:
        """Returns (usd_value_per_asset, total_value)."""
        balances = self.broker.get_balances()
        values: dict[str, float] = {}
        for sym in self.target_weights(cfg):
            qty = balances.get(sym, 0.0)
            price = 1.0 if sym == STABLE_SYMBOL else self.broker.get_price_usd(sym)
            values[sym] = qty * price
        return values, sum(values.values())

    # ------------------------- order planning -------------------------- #
    def plan(self) -> list[RebalanceOrder]:
        cfg = self.config()
        if isinstance(cfg, Rejected):
            self.reject(cfg, action="plan")
            return []
        values, total = self.current_state(cfg)
        if total <= 0:
            self.reject(Rejected("empty_portfolio", f"total value {total}"), action="plan")
            return []
        if isinstance(self.trading_gate(equity=Decimal(str(total))), Rejected):
            return []

        threshold = cfg.crypto_parameters.rebalance_threshold
        balances = self.broker.get_balances()
        sells: list[RebalanceOrder] = []
        wanted_buys: list[tuple[str, Decimal]] = []
        reasons: dict[str, str] = {}

        for sym, target_w in self.target_weights(cfg).items():
            if sym == STABLE_SYMBOL:
                continue  # the stablecoin absorbs the residual of other orders
            current_w = values[sym] / total
            deviation = current_w - target_w
            if abs(deviation) < threshold:
                continue
            delta_usd = abs(deviation) * total
            if delta_usd < float(MIN_ORDER_NOTIONAL_USD):
                continue
            price = self.broker.get_price_usd(sym)
            if deviation > 0:
                # Overweight -> sell. Never more than held (no shorts).
                qty = cap_spot_sell(
                    quantity=Decimal(str(delta_usd / price)),
                    holdings=Decimal(str(balances.get(sym, 0.0))),
                )
                if qty <= 0:
                    continue
                sells.append(RebalanceOrder(
                    sym, "sell", float(qty) * price, float(qty),
                    f"weight {current_w:.2%} > target {target_w:.2%}",
                ))
            else:
                wanted_buys.append((sym, Decimal(str(delta_usd))))
                reasons[sym] = f"weight {current_w:.2%} < target {target_w:.2%}"

        # Underweight -> buy. The TOTAL is capped at the USDC held now (no margin).
        buys = [
            RebalanceOrder(sym, "buy", float(notional),
                           float(notional) / self.broker.get_price_usd(sym), reasons[sym])
            for sym, notional in cap_spot_buys(
                wanted_buys,
                Decimal(str(balances.get(STABLE_SYMBOL, 0.0))),
                min_notional=MIN_ORDER_NOTIONAL_USD,
            )
        ]
        return sells + buys

    def execute(self, dry_run: bool = True) -> list[str]:
        """Executes the plan, sells first. With dry_run=True, orders are only logged."""
        order_ids: list[str] = []
        for order in self.plan():
            self._log.info("rebalance_order", strategy_id=self.strategy_id,
                           symbol=order.symbol, action=order.side,
                           reason=order.reason, notional_usd=round(order.notional_usd, 2),
                           mode="dry_run" if dry_run else "live")
            if dry_run:
                continue
            if order.side == "sell":
                order_ids.append(self.broker.market_sell(order.symbol, order.quantity))
            else:
                order_ids.append(self.broker.market_buy(order.symbol, order.notional_usd))
        return order_ids
