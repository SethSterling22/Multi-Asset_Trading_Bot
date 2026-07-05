"""Periodic crypto portfolio rebalancer (spot only).

Target allocation (config.json):
  - stable_target_allocation  -> USDC (defensive cash cushion)
  - crypto_target_allocation  -> split equally across monitored_assets

Safety rules:
  - No leverage, no shorts: spot orders only; a sell never exceeds
    actual holdings and a buy never exceeds available USDC.
  - Orders are generated only when the absolute weight deviation
    exceeds `rebalance_threshold` (±5% by default).

Broker access is abstracted behind the `SpotBroker` protocol so Alpaca
Crypto or Coinbase Advanced (endpoint /api/v3/brokerage with CDP keys)
can be injected without touching the rebalancing logic. A simulated
broker is included for local testing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Protocol

from config_models import CONFIG_PATH, TradingConfig

logger = logging.getLogger("crypto_rebalancer")

STABLE_SYMBOL = "USDC"
MIN_ORDER_NOTIONAL_USD = 10.0  # avoids micro-orders caused by price noise


# ---------------------------------------------------------------------- #
# Spot broker abstraction                                                 #
# ---------------------------------------------------------------------- #
class SpotBroker(Protocol):
    def get_balances(self) -> Dict[str, float]:
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
    """In-memory simulated broker for development and quick backtests."""

    def __init__(self, balances: Dict[str, float], prices: Dict[str, float]):
        self._balances = dict(balances)
        self._prices = dict(prices)
        self._order_seq = 0

    def get_balances(self) -> Dict[str, float]:
        return dict(self._balances)

    def get_price_usd(self, symbol: str) -> float:
        return self._prices[symbol]

    def market_buy(self, symbol: str, notional_usd: float) -> str:
        qty = notional_usd / self._prices[symbol]
        self._balances[symbol] = self._balances.get(symbol, 0.0) + qty
        self._balances[STABLE_SYMBOL] -= notional_usd
        self._order_seq += 1
        return f"paper-buy-{self._order_seq}"

    def market_sell(self, symbol: str, quantity: float) -> str:
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


class CryptoRebalancer:
    def __init__(self, broker: SpotBroker, config: TradingConfig | None = None):
        self.broker = broker
        self.config = config or TradingConfig.load(CONFIG_PATH)

    # ------------------------- weight computation ---------------------- #
    def target_weights(self) -> Dict[str, float]:
        p = self.config.crypto_parameters
        per_asset = p.crypto_target_allocation / len(p.monitored_assets)
        weights = {sym: per_asset for sym in p.monitored_assets}
        weights[STABLE_SYMBOL] = p.stable_target_allocation
        return weights

    def current_state(self) -> tuple[Dict[str, float], float]:
        """Returns (usd_value_per_asset, total_value)."""
        balances = self.broker.get_balances()
        values: Dict[str, float] = {}
        for sym in self.target_weights():
            qty = balances.get(sym, 0.0)
            price = 1.0 if sym == STABLE_SYMBOL else self.broker.get_price_usd(sym)
            values[sym] = qty * price
        return values, sum(values.values())

    # ------------------------- order planning -------------------------- #
    def plan(self) -> List[RebalanceOrder]:
        if self.config.system_status.emergency_kill_switch:
            logger.warning("Kill switch engaged: rebalance aborted.")
            return []

        values, total = self.current_state()
        if total <= 0:
            logger.warning("Empty portfolio: nothing to rebalance.")
            return []

        threshold = self.config.crypto_parameters.rebalance_threshold
        orders: List[RebalanceOrder] = []
        balances = self.broker.get_balances()

        for sym, target_w in self.target_weights().items():
            if sym == STABLE_SYMBOL:
                continue  # the stablecoin absorbs the residual of other orders
            current_w = values[sym] / total
            deviation = current_w - target_w
            if abs(deviation) < threshold:
                continue

            delta_usd = abs(deviation) * total
            if delta_usd < MIN_ORDER_NOTIONAL_USD:
                continue
            price = self.broker.get_price_usd(sym)

            if deviation > 0:
                # Overweight -> sell. Never more than held (no shorts).
                qty = min(delta_usd / price, balances.get(sym, 0.0))
                if qty <= 0:
                    continue
                orders.append(RebalanceOrder(
                    sym, "sell", qty * price, qty,
                    f"weight {current_w:.2%} > target {target_w:.2%}",
                ))
            else:
                # Underweight -> buy. Capped at actual USDC (no margin).
                notional = min(delta_usd, balances.get(STABLE_SYMBOL, 0.0))
                if notional < MIN_ORDER_NOTIONAL_USD:
                    continue
                orders.append(RebalanceOrder(
                    sym, "buy", notional, notional / price,
                    f"weight {current_w:.2%} < target {target_w:.2%}",
                ))
        return orders

    def execute(self, dry_run: bool = True) -> List[str]:
        """Executes the plan. With dry_run=True, orders are only logged."""
        order_ids: List[str] = []
        for order in self.plan():
            logger.info("[%s] %s %s ~$%.2f (%s)",
                        "DRY-RUN" if dry_run else "LIVE",
                        order.side.upper(), order.symbol,
                        order.notional_usd, order.reason)
            if dry_run:
                continue
            if order.side == "sell":
                order_ids.append(self.broker.market_sell(order.symbol, order.quantity))
            else:
                order_ids.append(self.broker.market_buy(order.symbol, order.notional_usd))
        return order_ids


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Demo with a drifted portfolio: BTC overweight.
    demo = PaperSpotBroker(
        balances={"BTC": 0.30, "ETH": 2.0, "USDC": 5000.0},
        prices={"BTC": 68000.0, "ETH": 3500.0},
    )
    bot = CryptoRebalancer(demo)
    cfg_live = not bot.config.system_status.live_trading_mode
    ids = bot.execute(dry_run=cfg_live)  # dry-run unless live mode is explicit
    values, total = bot.current_state()
    print(f"Total value: ${total:,.2f} | Executed orders: {ids or 'none (dry-run)'}")
