"""Rebalanceador periódico de cartera cripto (spot únicamente).

Asignación objetivo (config.json):
  - stable_target_allocation  -> USDC (cojín defensivo)
  - crypto_target_allocation  -> repartido a partes iguales entre monitored_assets

Reglas de seguridad:
  - Sin apalancamiento, sin cortos: solo órdenes spot; una venta nunca
    excede las tenencias reales y una compra nunca excede el USDC disponible.
  - Solo se generan órdenes si la desviación absoluta de peso supera
    `rebalance_threshold` (±5% por defecto).

El acceso al bróker se abstrae con `SpotBroker` para poder inyectar
Alpaca Crypto o Coinbase Advanced (endpoint /api/v3/brokerage con claves
CDP) sin tocar la lógica de rebalanceo. Incluye un broker simulado para
pruebas locales.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Protocol

from config_models import CONFIG_PATH, TradingConfig

logger = logging.getLogger("crypto_rebalancer")

STABLE_SYMBOL = "USDC"
MIN_ORDER_NOTIONAL_USD = 10.0  # evita micro-órdenes por ruido de precios


# ---------------------------------------------------------------------- #
# Abstracción del bróker spot                                             #
# ---------------------------------------------------------------------- #
class SpotBroker(Protocol):
    def get_balances(self) -> Dict[str, float]:
        """Unidades por símbolo, ej. {'BTC': 0.5, 'ETH': 4.0, 'USDC': 12000}."""
        ...

    def get_price_usd(self, symbol: str) -> float:
        """Precio spot en USD del símbolo."""
        ...

    def market_buy(self, symbol: str, notional_usd: float) -> str:
        """Compra spot por importe en USD. Devuelve el id de la orden."""
        ...

    def market_sell(self, symbol: str, quantity: float) -> str:
        """Venta spot por cantidad de unidades. Devuelve el id de la orden."""
        ...


class PaperSpotBroker:
    """Broker simulado en memoria para desarrollo y backtests rápidos."""

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
# Lógica de rebalanceo                                                    #
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class RebalanceOrder:
    symbol: str
    side: str            # "buy" | "sell"
    notional_usd: float  # importe objetivo de la orden
    quantity: float      # unidades (relevante en ventas)
    reason: str


class CryptoRebalancer:
    def __init__(self, broker: SpotBroker, config: TradingConfig | None = None):
        self.broker = broker
        self.config = config or TradingConfig.load(CONFIG_PATH)

    # ------------------------- cálculo de pesos ------------------------ #
    def target_weights(self) -> Dict[str, float]:
        p = self.config.crypto_parameters
        per_asset = p.crypto_target_allocation / len(p.monitored_assets)
        weights = {sym: per_asset for sym in p.monitored_assets}
        weights[STABLE_SYMBOL] = p.stable_target_allocation
        return weights

    def current_state(self) -> tuple[Dict[str, float], float]:
        """Devuelve (valor_usd_por_activo, valor_total)."""
        balances = self.broker.get_balances()
        values: Dict[str, float] = {}
        for sym in self.target_weights():
            qty = balances.get(sym, 0.0)
            price = 1.0 if sym == STABLE_SYMBOL else self.broker.get_price_usd(sym)
            values[sym] = qty * price
        return values, sum(values.values())

    # ------------------------- plan de órdenes ------------------------- #
    def plan(self) -> List[RebalanceOrder]:
        if self.config.system_status.emergency_kill_switch:
            logger.warning("Kill switch activo: rebalanceo abortado.")
            return []

        values, total = self.current_state()
        if total <= 0:
            logger.warning("Cartera vacía: nada que rebalancear.")
            return []

        threshold = self.config.crypto_parameters.rebalance_threshold
        orders: List[RebalanceOrder] = []
        balances = self.broker.get_balances()

        for sym, target_w in self.target_weights().items():
            if sym == STABLE_SYMBOL:
                continue  # la stablecoin absorbe el residuo de las demás órdenes
            current_w = values[sym] / total
            deviation = current_w - target_w
            if abs(deviation) < threshold:
                continue

            delta_usd = abs(deviation) * total
            if delta_usd < MIN_ORDER_NOTIONAL_USD:
                continue
            price = self.broker.get_price_usd(sym)

            if deviation > 0:
                # Sobreponderado -> vender. Nunca más de lo que se posee (sin cortos).
                qty = min(delta_usd / price, balances.get(sym, 0.0))
                if qty <= 0:
                    continue
                orders.append(RebalanceOrder(
                    sym, "sell", qty * price, qty,
                    f"peso {current_w:.2%} > objetivo {target_w:.2%}",
                ))
            else:
                # Infraponderado -> comprar. Limitado al USDC real (sin margen).
                notional = min(delta_usd, balances.get(STABLE_SYMBOL, 0.0))
                if notional < MIN_ORDER_NOTIONAL_USD:
                    continue
                orders.append(RebalanceOrder(
                    sym, "buy", notional, notional / price,
                    f"peso {current_w:.2%} < objetivo {target_w:.2%}",
                ))
        return orders

    def execute(self, dry_run: bool = True) -> List[str]:
        """Ejecuta el plan. Con dry_run=True solo registra las órdenes."""
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
    # Demostración con cartera desviada: BTC sobreponderado.
    demo = PaperSpotBroker(
        balances={"BTC": 0.30, "ETH": 2.0, "USDC": 5000.0},
        prices={"BTC": 68000.0, "ETH": 3500.0},
    )
    bot = CryptoRebalancer(demo)
    cfg_live = not bot.config.system_status.live_trading_mode
    ids = bot.execute(dry_run=cfg_live)  # dry-run salvo modo live explícito
    values, total = bot.current_state()
    print(f"Valor total: ${total:,.2f} | Órdenes ejecutadas: {ids or 'ninguna (dry-run)'}")
