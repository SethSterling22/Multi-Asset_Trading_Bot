"""Estrategia 'The Wheel' (La Rueda) sobre Lumibot + Alpaca.

Ciclo de vida:
  1. Venta de Cash-Secured Puts (CSP) con colateral 100% inmovilizado.
  2. Si hay asignación (>=100 acciones), venta de Covered Calls (CC).
  3. Cierre anticipado de cualquier opción corta al capturar el
     `early_close_percentage_gain` (80%) de la prima recolectada.

Reglas de seguridad (no negociables):
  - CSP: cash disponible >= strike * 100 antes de transmitir la orden.
  - CC:  >=100 acciones del subyacente en cartera por contrato vendido.
  - Kill switch y límite de pérdida diaria evaluados en cada iteración.
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
    # Ciclo de vida Lumibot                                               #
    # ------------------------------------------------------------------ #
    def initialize(self) -> None:
        self.sleeptime = "15M"  # frecuencia de evaluación
        self.config: TradingConfig = TradingConfig.load(CONFIG_PATH)
        self._config_mtime: float = CONFIG_PATH.stat().st_mtime
        # Prima recolectada por opción corta abierta: {order_identifier: credito}
        self._open_premiums: dict[str, float] = {}
        self._session_start_value: Optional[float] = None
        logger.info("WheelStrategy inicializada. Whitelist: %s",
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
            except Exception:  # noqa: BLE001 - un activo no debe tumbar el bucle
                logger.exception("Error en el ciclo de %s", symbol)

    def on_abnormal_market_conditions(self) -> None:
        """Disyuntor: cierra las posiciones cortas de opciones ante anomalías."""
        logger.warning("Condiciones anormales de mercado: cerrando opciones cortas.")
        for pos in self.get_positions():
            if pos.asset.asset_type == Asset.AssetType.OPTION and pos.quantity < 0:
                self._close_short_option(pos)

    # ------------------------------------------------------------------ #
    # Guardas de seguridad                                                 #
    # ------------------------------------------------------------------ #
    def _system_is_safe_to_trade(self) -> bool:
        status = self.config.system_status
        if not status.active or status.emergency_kill_switch:
            logger.warning("Sistema inactivo o kill switch activado. Sin operaciones.")
            return False
        if self._daily_loss_limit_breached():
            logger.error("Límite de pérdida diaria alcanzado. Trading suspendido hoy.")
            return False
        return True

    def _daily_loss_limit_breached(self) -> bool:
        if not self._session_start_value:
            return False
        current = self.get_portfolio_value()
        drawdown = (self._session_start_value - current) / self._session_start_value
        return drawdown >= self.config.risk_guards.daily_loss_limit_percentage

    def _reload_config_if_changed(self) -> None:
        """Recarga en caliente cuando el dashboard reescribe config.json."""
        mtime = CONFIG_PATH.stat().st_mtime
        if mtime != self._config_mtime:
            self.config = TradingConfig.load(CONFIG_PATH)
            self._config_mtime = mtime
            logger.info("config.json recargado en caliente.")

    # ------------------------------------------------------------------ #
    # Núcleo de La Rueda                                                   #
    # ------------------------------------------------------------------ #
    def _run_wheel_cycle(self, symbol: str) -> None:
        underlying = Asset(symbol, asset_type=Asset.AssetType.STOCK)
        shares = self._shares_held(underlying)
        short_options = self._short_options_for(symbol)

        # 1) Gestionar opciones cortas existentes (cierre anticipado al 80%).
        for pos in short_options:
            self._maybe_close_early(pos)

        # Solo una opción corta viva por subyacente.
        if self._short_options_for(symbol):
            return

        # 2) Asignado (>=100 acciones) -> vender Covered Call.
        if shares >= OPTION_MULTIPLIER:
            self._sell_covered_call(underlying, shares)
        # 3) Sin acciones -> vender Cash-Secured Put.
        else:
            self._sell_cash_secured_put(underlying)

    def _sell_cash_secured_put(self, underlying: Asset) -> None:
        wheel = self.config.wheel_parameters
        contract = self._select_contract(
            underlying, right="put", target_delta=wheel.delta_limit_csp
        )
        if contract is None:
            return

        # --- CONTROL CASH-SECURED: colateral íntegro antes de la orden ---
        collateral = contract.strike * OPTION_MULTIPLIER
        if self.get_cash() < collateral:
            logger.warning(
                "CSP %s rechazada: cash %.2f < colateral %.2f",
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
            logger.info("CSP vendida %s strike %.2f prima ~%.2f",
                        underlying.symbol, contract.strike, premium)

    def _sell_covered_call(self, underlying: Asset, shares: float) -> None:
        wheel = self.config.wheel_parameters
        contract = self._select_contract(
            underlying, right="call", target_delta=wheel.delta_limit_cc
        )
        if contract is None:
            return

        # --- CONTROL COVERED: nunca más contratos que lotes de 100 ---
        max_contracts = int(shares // OPTION_MULTIPLIER)
        if max_contracts < 1:
            return

        premium = self._mid_price(contract)
        order = self.create_order(contract, 1, Order.OrderSide.SELL_TO_OPEN)
        submitted = self.submit_order(order)
        if submitted:
            self._open_premiums[str(submitted.identifier)] = premium
            logger.info("CC vendida %s strike %.2f prima ~%.2f",
                        underlying.symbol, contract.strike, premium)

    def _maybe_close_early(self, position) -> None:
        """Recompra la opción corta al capturar el % objetivo de la prima."""
        wheel = self.config.wheel_parameters
        entry_premium = self._open_premiums.get(str(position.orders[0].identifier)
                                                if position.orders else "", None)
        current = self._mid_price(position.asset)
        if entry_premium is None or current is None or entry_premium <= 0:
            return
        captured = (entry_premium - current) / entry_premium
        if captured >= wheel.early_close_percentage_gain:
            logger.info("Cierre anticipado %s: %.0f%% de la prima capturada",
                        position.asset, captured * 100)
            self._close_short_option(position)

    def _close_short_option(self, position) -> None:
        order = self.create_order(
            position.asset, abs(position.quantity), Order.OrderSide.BUY_TO_CLOSE
        )
        self.submit_order(order)

    # ------------------------------------------------------------------ #
    # Selección de contratos                                               #
    # ------------------------------------------------------------------ #
    def _select_contract(
        self, underlying: Asset, right: str, target_delta: float
    ) -> Optional[Asset]:
        """Elige el contrato dentro de la ventana DTE cuyo delta se aproxima
        al objetivo. El delta se estima con greeks del bróker; si no están
        disponibles, se usa la distancia OTM como proxy conservador."""
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
            # Solo strikes OTM (put: por debajo del precio; call: por encima).
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
        # Proxy sin greeks: moneyness lineal acotada (aprox. burda, solo fallback).
        moneyness = abs(spot - contract.strike) / spot
        magnitude = max(0.05, min(0.95, 0.5 - moneyness * 2.5))
        sign = -1.0 if contract.right.lower() == "put" else 1.0
        return sign * magnitude

    # ------------------------------------------------------------------ #
    # Utilidades                                                           #
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
        # Para CSP el colateral es la exposición máxima teórica.
        if exposure / portfolio > max(limit * 10, limit):  # colateral, no riesgo neto
            logger.warning("Exposición %.2f excede el límite por operación.", exposure)
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
