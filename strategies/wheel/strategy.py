"""Adaptador de Lumibot + Alpaca para el Wheel. La lógica está en logic.py.

Este fichero solo traduce: llamadas de Lumibot <-> el Protocol OptionsBroker,
callbacks de fills -> ledger, y la equity del broker -> equity_curve (de donde
salen la pérdida diaria y semanal). No contiene guards: los guards son de la base.

Ejecutar (paper por defecto):

    DATABASE_URL=... REDIS_URL=... uv run python -m strategies.wheel

Parar: Ctrl+C, o `pkill -f strategies.wheel` (SIGTERM) desde otra terminal. El
proceso termina en los dos casos (strategies/runtime.py).

Sin DATABASE_URL o sin REDIS_URL no arranca: sin ledger no hay registro que
evaluar, y sin las tres fuentes el kill switch no existe (CLAUDE.md §1.7).

⚠ Este adaptador no tiene tests contra Lumibot real: necesita un broker. Lo que
sí está probado es la lógica (tests/guards/test_wheel_guards.py) con un broker
falso que implementa el mismo Protocol.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from lumibot.entities import Asset, Order
from lumibot.strategies import Strategy
from sqlalchemy import create_engine

from core import log
from core.config.models import CONFIG_PATH, TradingConfig
from core.greeks import Right
from core.ledger.equity import EquitySnapshot
from core.ledger.fills import AssetType, Fill, Side, fetch_fills
from risk.breakers import KillSwitch, file_source, redis_source, risk_events_source
from strategies.base import (
    EquityCurveLossReference,
    ParamsLoader,
    PostgresLedger,
    config_kill_source,
)
from strategies.runtime import run_process
from strategies.wheel.logic import (
    VENUE,
    OptionContract,
    OptionsBroker,
    PendingOrder,
    ShortOption,
    WheelLogic,
)

MARKET_TZ = ZoneInfo("America/New_York")   # el "día" de la pérdida diaria del Wheel
SIDES = {s.value: s for s in Side}


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _contract(asset: Asset) -> OptionContract:
    right = Right.PUT if str(asset.right).lower() == "put" else Right.CALL
    return OptionContract(asset.symbol, asset.expiration, Decimal(str(asset.strike)), right)


def _asset(contract: OptionContract) -> Asset:
    return Asset(contract.underlying, asset_type=Asset.AssetType.OPTION,
                 expiration=contract.expiration, strike=float(contract.strike),
                 right=contract.right.value)


class LumibotOptionsBroker(OptionsBroker):
    """OptionsBroker sobre los métodos de una Strategy de Lumibot."""

    def __init__(self, strategy: Strategy) -> None:
        self.s = strategy

    def today(self) -> date:
        return datetime.now(MARKET_TZ).date()

    def cash(self) -> Decimal:
        return Decimal(str(self.s.get_cash() or 0))

    def portfolio_value(self) -> Decimal:
        return Decimal(str(self.s.get_portfolio_value() or 0))

    def spot(self, symbol: str) -> Decimal | None:
        return _dec(self.s.get_last_price(Asset(symbol, asset_type=Asset.AssetType.STOCK)))

    def shares(self, symbol: str) -> Decimal:
        pos = self.s.get_position(Asset(symbol, asset_type=Asset.AssetType.STOCK))
        return Decimal(str(pos.quantity)) if pos else Decimal(0)

    def short_options(self) -> list[ShortOption]:
        return [
            ShortOption(_contract(p.asset), int(abs(p.quantity)))
            for p in self.s.get_positions()
            if p.asset.asset_type == Asset.AssetType.OPTION and p.quantity < 0
        ]

    def pending_orders(self) -> list[PendingOrder]:
        out = []
        for o in self.s.get_orders(statuses=Order.ACTIVE_STATUSES) or []:
            is_option = o.asset.asset_type == Asset.AssetType.OPTION
            out.append(PendingOrder(
                underlying=o.asset.symbol,
                contract=_contract(o.asset) if is_option else None,
                side=SIDES.get(str(o.side), Side.BUY),
                quantity=int(abs(o.quantity)),
            ))
        return out

    def expirations(self, symbol: str) -> list[date]:
        chains = self.s.get_chains(Asset(symbol, asset_type=Asset.AssetType.STOCK))
        return list(self.s.get_expiration(chains) or []) if chains else []

    def strikes(self, symbol: str, expiration: date, right: Right) -> list[Decimal]:
        asset = Asset(symbol, asset_type=Asset.AssetType.OPTION, expiration=expiration,
                      right=right.value)
        return [Decimal(str(k)) for k in self.s.get_strikes(asset) or []]

    def option_mid(self, contract: OptionContract) -> Decimal | None:
        quote = self.s.get_quote(_asset(contract))
        if quote and quote.bid and quote.ask:
            return (Decimal(str(quote.bid)) + Decimal(str(quote.ask))) / 2
        return None   # sin bid/ask no hay precio: el último trade puede ser de hace días

    def broker_delta(self, contract: OptionContract) -> float | None:
        greeks = self.s.get_greeks(_asset(contract), query_greeks=True)
        delta = greeks.get("delta") if greeks else None
        return None if delta is None else float(delta)

    def risk_free_rate(self) -> float | None:
        rate = self.s.risk_free_rate
        return None if rate is None else float(rate)

    def sell_to_open(self, contract: OptionContract, quantity: int) -> str | None:
        order = self.s.submit_order(
            self.s.create_order(_asset(contract), quantity, Order.OrderSide.SELL_TO_OPEN)
        )
        return str(order.identifier) if order else None

    def buy_to_close(self, contract: OptionContract, quantity: int) -> str | None:
        order = self.s.submit_order(
            self.s.create_order(_asset(contract), quantity, Order.OrderSide.BUY_TO_CLOSE)
        )
        return str(order.identifier) if order else None


class WheelStrategy(Strategy):  # type: ignore[misc]
    def initialize(self) -> None:
        self.sleeptime = "15M"
        engine = create_engine(os.environ["DATABASE_URL"])
        import redis

        params = ParamsLoader(CONFIG_PATH)
        self.logic = WheelLogic(
            broker=LumibotOptionsBroker(self),
            fills=lambda: self._wheel_fills(engine),
        )
        self.logic.setup_base(
            params=params,
            kill_switch=KillSwitch(
                risk_events=risk_events_source(engine),
                redis=redis_source(redis.Redis.from_url(os.environ["REDIS_URL"])),
                halt_file=file_source(),
                extra={"config.json": config_kill_source(params)},
            ),
            ledger=PostgresLedger(engine),
            loss_reference=EquityCurveLossReference(engine, MARKET_TZ),
            clock=lambda: datetime.now(UTC),
        )
        self._fill_seq: dict[str, int] = {}

    @staticmethod
    def _wheel_fills(engine: Any) -> Sequence[Fill]:
        with engine.connect() as conn:
            return fetch_fills(conn, strategy_id="wheel")

    def on_trading_iteration(self) -> None:
        # El snapshot va ANTES del gate: es la referencia de la pérdida diaria, y
        # tiene que existir aunque el proceso arranque a mitad de sesión.
        value = Decimal(str(self.get_portfolio_value() or 0))
        self.logic.record_equity(EquitySnapshot(
            ts=datetime.now(UTC), strategy_id="wheel", equity_usd=value,
            allocated_usd=value, gross_notional=Decimal(0), unrealized_pnl=Decimal(0),
        ))
        self.logic.run_iteration()

    def _record(self, order: Order, price: float, quantity: float, multiplier: float,
                kind: str) -> None:
        is_option = order.asset.asset_type == Asset.AssetType.OPTION
        n = self._fill_seq.get(order.identifier, 0) + 1
        self._fill_seq[order.identifier] = n
        self.logic.record_fill(Fill(
            ts=datetime.now(UTC),
            venue=VENUE,
            symbol=_contract(order.asset).occ_symbol if is_option else order.asset.symbol,
            asset_type=AssetType.OPTION if is_option else AssetType.STOCK,
            side=SIDES[str(order.side)],
            qty=Decimal(str(abs(quantity))),
            price=Decimal(str(price)),
            multiplier=Decimal(str(multiplier or 1)),
            strategy_id="wheel",
            # Lumibot no da id de fill: identificador de la orden + secuencia por
            # evento. La reconciliación de la Sesión 5 lo cruza con el venue.
            venue_fill_id=f"{order.identifier}-{kind}-{n}",
            client_order_id=None,
        ))

    def on_filled_order(self, position: Any, order: Order, price: float,
                        quantity: float, multiplier: float) -> None:
        self._record(order, price, quantity, multiplier, "fill")

    def on_partially_filled_order(self, position: Any, order: Order, price: float,
                                  quantity: float, multiplier: float) -> None:
        self._record(order, price, quantity, multiplier, "partial")


def main() -> None:
    from dotenv import load_dotenv
    from lumibot.brokers import Alpaca
    from lumibot.traders import Trader

    load_dotenv()
    log.configure("wheel")
    required = ("ALPACA_API_KEY", "ALPACA_API_SECRET", "DATABASE_URL", "REDIS_URL")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit(f"Faltan variables: {', '.join(missing)} (ver RUNNING.md §3 y §6).")

    config = TradingConfig.load()
    is_paper = not config.system_status.live_trading_mode
    log.get_logger().warning("startup", action="start",
                             reason="LIVE: dinero real" if not is_paper else "paper")
    broker = Alpaca({
        "API_KEY": os.environ["ALPACA_API_KEY"],
        "API_SECRET": os.environ["ALPACA_API_SECRET"],
        "PAPER": is_paper,
    })
    trader = Trader()
    trader.add_strategy(WheelStrategy(broker=broker))
    # Ctrl+C lo atiende Lumibot; SIGTERM y la salida final, run_process. Sin esto
    # el proceso no muere: el hilo del WebSocket de Alpaca no es daemon.
    run_process(trader.run_all, trader.stop_all)
