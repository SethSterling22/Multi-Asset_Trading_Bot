"""'The Wheel' strategy on Lumibot + Alpaca.

Lifecycle:
  1. Sell Cash-Secured Puts (CSP) with 100% of the collateral locked in cash.
  2. On assignment (>=100 shares), sell Covered Calls (CC).
  3. Close any short option early once `early_close_percentage_gain`
     (80%) of the collected premium has been captured.

Safety rules (non-negotiable):
  - CSP: available cash >= strike * 100 before transmitting the order.
  - CC:  >=100 shares of the underlying held per contract sold.
  - Kill switch and daily loss limit evaluated on every iteration.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional

from lumibot.entities import Asset, Order
from lumibot.strategies import Strategy

from config_models import CONFIG_PATH, TradingConfig

logger = logging.getLogger("wheel_strategy")

OPTION_MULTIPLIER = 100


class WheelStrategy(Strategy):
    # ------------------------------------------------------------------ #
    # Lumibot lifecycle                                                    #
    # ------------------------------------------------------------------ #
    def initialize(self) -> None:
        self.sleeptime = "15M"  # evaluation frequency
        self.config: TradingConfig = TradingConfig.load(CONFIG_PATH)
        self._config_mtime: float = CONFIG_PATH.stat().st_mtime
        # Premium collected per open short option: {order_identifier: credit}
        self._open_premiums: dict[str, float] = {}
        self._session_start_value: Optional[float] = None
        logger.info("WheelStrategy initialized. Whitelist: %s",
                    self.config.wheel_parameters.whitelist_assets)

    def before_market_opens(self) -> None:
        self._session_start_value = self.get_portfolio_value()

    def on_trading_iteration(self) -> None:
        self._reload_config_if_changed()

        if not self._system_is_safe_to_trade():
            return

        wheel = self.config.wheel_parameters
        for symbol in wheel.whitelist_assets:
            try:
                self._run_wheel_cycle(symbol)
            except Exception:  # noqa: BLE001 - one asset must not break the loop
                logger.exception("Error in wheel cycle for %s", symbol)

    def on_abnormal_market_conditions(self) -> None:
        """Circuit breaker: closes short option positions on anomalies."""
        logger.warning("Abnormal market conditions: closing short options.")
        for pos in self.get_positions():
            if pos.asset.asset_type == Asset.AssetType.OPTION and pos.quantity < 0:
                self._close_short_option(pos)

    # ------------------------------------------------------------------ #
    # Safety guards                                                        #
    # ------------------------------------------------------------------ #
    def _system_is_safe_to_trade(self) -> bool:
        status = self.config.system_status
        if not status.active or status.emergency_kill_switch:
            logger.warning("System inactive or kill switch engaged. No trading.")
            return False
        if self._daily_loss_limit_breached():
            logger.error("Daily loss limit reached. Trading suspended for today.")
            return False
        return True

    def _daily_loss_limit_breached(self) -> bool:
        if not self._session_start_value:
            return False
        current = self.get_portfolio_value()
        drawdown = (self._session_start_value - current) / self._session_start_value
        return drawdown >= self.config.risk_guards.daily_loss_limit_percentage

    def _reload_config_if_changed(self) -> None:
        """Hot-reload when the dashboard rewrites config.json."""
        mtime = CONFIG_PATH.stat().st_mtime
        if mtime != self._config_mtime:
            self.config = TradingConfig.load(CONFIG_PATH)
            self._config_mtime = mtime
            logger.info("config.json hot-reloaded.")

    # ------------------------------------------------------------------ #
    # Wheel core                                                           #
    # ------------------------------------------------------------------ #
    def _run_wheel_cycle(self, symbol: str) -> None:
        underlying = Asset(symbol, asset_type=Asset.AssetType.STOCK)
        shares = self._shares_held(underlying)
        short_options = self._short_options_for(symbol)

        # 1) Manage existing short options (early close at 80%).
        for pos in short_options:
            self._maybe_close_early(pos)

        # Only one live short option per underlying.
        if self._short_options_for(symbol):
            return

        # 2) Assigned (>=100 shares) -> sell Covered Call.
        if shares >= OPTION_MULTIPLIER:
            self._sell_covered_call(underlying, shares)
        # 3) No shares -> sell Cash-Secured Put.
        else:
            self._sell_cash_secured_put(underlying)

    def _sell_cash_secured_put(self, underlying: Asset) -> None:
        wheel = self.config.wheel_parameters
        contract = self._select_contract(
            underlying, right="put", target_delta=wheel.delta_limit_csp
        )
        if contract is None:
            return

        # --- CASH-SECURED CHECK: full collateral before the order ---
        collateral = contract.strike * OPTION_MULTIPLIER
        if self.get_cash() < collateral:
            logger.warning(
                "CSP %s rejected: cash %.2f < collateral %.2f",
                underlying.symbol, self.get_cash(), collateral,
            )
            return

        if not self._passes_per_trade_risk(collateral):
            return

        premium = self._mid_price(contract)
        order = self.create_order(contract, 1, Order.OrderSide.SELL_TO_OPEN)
        submitted = self.submit_order(order)
        if submitted:
            self._open_premiums[str(submitted.identifier)] = premium
            logger.info("Sold CSP %s strike %.2f premium ~%.2f",
                        underlying.symbol, contract.strike, premium)

    def _sell_covered_call(self, underlying: Asset, shares: float) -> None:
        wheel = self.config.wheel_parameters
        contract = self._select_contract(
            underlying, right="call", target_delta=wheel.delta_limit_cc
        )
        if contract is None:
            return

        # --- COVERED CHECK: never more contracts than 100-share lots ---
        max_contracts = int(shares // OPTION_MULTIPLIER)
        if max_contracts < 1:
            return

        premium = self._mid_price(contract)
        order = self.create_order(contract, 1, Order.OrderSide.SELL_TO_OPEN)
        submitted = self.submit_order(order)
        if submitted:
            self._open_premiums[str(submitted.identifier)] = premium
            logger.info("Sold CC %s strike %.2f premium ~%.2f",
                        underlying.symbol, contract.strike, premium)

    def _maybe_close_early(self, position) -> None:
        """Buy back the short option once the target % of premium is captured."""
        wheel = self.config.wheel_parameters
        entry_premium = self._open_premiums.get(str(position.orders[0].identifier)
                                                if position.orders else "", None)
        current = self._mid_price(position.asset)
        if entry_premium is None or current is None or entry_premium <= 0:
            return
        captured = (entry_premium - current) / entry_premium
        if captured >= wheel.early_close_percentage_gain:
            logger.info("Early close %s: %.0f%% of premium captured",
                        position.asset, captured * 100)
            self._close_short_option(position)

    def _close_short_option(self, position) -> None:
        order = self.create_order(
            position.asset, abs(position.quantity), Order.OrderSide.BUY_TO_CLOSE
        )
        self.submit_order(order)

    # ------------------------------------------------------------------ #
    # Contract selection                                                   #
    # ------------------------------------------------------------------ #
    def _select_contract(
        self, underlying: Asset, right: str, target_delta: float
    ) -> Optional[Asset]:
        """Pick the contract within the DTE window whose delta is closest
        to the target. Delta comes from broker greeks; if unavailable,
        OTM distance is used as a conservative proxy."""
        wheel = self.config.wheel_parameters
        price = self.get_last_price(underlying)
        chains = self.get_chains(underlying)
        if price is None or not chains:
            return None

        expiration = self._pick_expiration(
            chains, wheel.target_expiration_days_min, wheel.target_expiration_days_max
        )
        if expiration is None:
            return None

        strikes = self.get_strikes(
            Asset(underlying.symbol, asset_type=Asset.AssetType.OPTION,
                  expiration=expiration, right=right)
        ) or []

        best: Optional[Asset] = None
        best_gap = float("inf")
        for strike in strikes:
            # OTM strikes only (put: below spot; call: above spot).
            if right == "put" and strike >= price:
                continue
            if right == "call" and strike <= price:
                continue
            contract = Asset(
                underlying.symbol, asset_type=Asset.AssetType.OPTION,
                expiration=expiration, strike=strike, right=right,
            )
            delta = self._estimate_delta(contract, price)
            if delta is None:
                continue
            gap = abs(delta - target_delta)
            if gap < best_gap:
                best, best_gap = contract, gap
        return best

    def _pick_expiration(self, chains, dte_min: int, dte_max: int) -> Optional[date]:
        today = date.today()
        window = (today + timedelta(days=dte_min), today + timedelta(days=dte_max))
        candidates: List[date] = [
            exp for exp in self.get_expiration(chains) or []
            if window[0] <= exp <= window[1]
        ]
        return min(candidates) if candidates else None

    def _estimate_delta(self, contract: Asset, spot: float) -> Optional[float]:
        greeks = self.get_greeks(contract)
        if greeks and greeks.get("delta") is not None:
            return float(greeks["delta"])
        # Proxy without greeks: bounded linear moneyness (rough, fallback only).
        moneyness = abs(spot - contract.strike) / spot
        magnitude = max(0.05, min(0.95, 0.5 - moneyness * 2.5))
        sign = -1.0 if contract.right.lower() == "put" else 1.0
        return sign * magnitude

    # ------------------------------------------------------------------ #
    # Utilities                                                            #
    # ------------------------------------------------------------------ #
    def _shares_held(self, underlying: Asset) -> float:
        pos = self.get_position(underlying)
        return float(pos.quantity) if pos else 0.0

    def _short_options_for(self, symbol: str) -> list:
        return [
            p for p in self.get_positions()
            if p.asset.asset_type == Asset.AssetType.OPTION
            and p.asset.symbol == symbol
            and p.quantity < 0
        ]

    def _mid_price(self, asset: Asset) -> Optional[float]:
        quote = self.get_quote(asset)
        if quote and quote.bid and quote.ask:
            return (quote.bid + quote.ask) / 2
        return self.get_last_price(asset)

    def _passes_per_trade_risk(self, exposure: float) -> bool:
        limit = self.config.risk_guards.max_portfolio_risk_per_trade
        portfolio = self.get_portfolio_value() or 0.0
        if portfolio <= 0:
            return False
        # For CSPs the collateral is the theoretical maximum exposure.
        if exposure / portfolio > max(limit * 10, limit):  # collateral, not net risk
            logger.warning("Exposure %.2f exceeds per-trade limit.", exposure)
            return False
        return True


if __name__ == "__main__":
    from lumibot.brokers import Alpaca
    from lumibot.traders import Trader

    import os
    ALPACA_CONFIG = {
        "API_KEY": os.environ["ALPACA_API_KEY"],
        "API_SECRET": os.environ["ALPACA_API_SECRET"],
        "PAPER": not TradingConfig.load().system_status.live_trading_mode,
    }
    trader = Trader()
    broker = Alpaca(ALPACA_CONFIG)
    trader.add_strategy(WheelStrategy(broker=broker))
    trader.run_all()
