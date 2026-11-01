"""Dobles de prueba para los guards: sin base de datos, sin Redis, sin broker, sin red.

CI corre tests/guards/ solo con el grupo `dev`, así que todo lo de aquí implementa
los mismos Protocols que las piezas reales (KillSource, LedgerSink, LossReference,
OptionsBroker) en memoria.
"""

from __future__ import annotations

import io
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import log
from core.config.models import CONFIG_PATH
from core.greeks import Right, bs_price
from core.ledger.equity import EquitySnapshot
from core.ledger.fills import Fill
from risk.breakers import KillSwitch
from strategies.base import ParamsLoader, StrategyBase, config_kill_source
from strategies.wheel.logic import OptionContract, PendingOrder, ShortOption

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
TODAY = date(2026, 11, 2)


class Events:
    """Captura los eventos JSON que emite core.log."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        log.configure("guards-test", stream=self.buffer)

    def all(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.buffer.getvalue().splitlines()]

    def rejections(self) -> list[dict[str, Any]]:
        return [e for e in self.all() if e["event"] == "guard_rejected"]


def write_config(tmp_path: Path, **changes: Any) -> Path:
    """Copia config.json y aplica cambios con claves 'seccion.campo'."""
    path = tmp_path / "config.json"
    shutil.copy(CONFIG_PATH, path)
    data = json.loads(path.read_text())
    for dotted, value in changes.items():
        section, name = dotted.split(".")
        data[section][name] = value
    path.write_text(json.dumps(data))
    return path


@dataclass
class Switches:
    """Estado de las tres fuentes del kill switch; un valor Exception simula un error."""

    risk_events: bool | Exception = False
    redis: bool | Exception = False
    halt_file: bool | Exception = False

    def source(self, name: str) -> Any:
        def check(_: str) -> bool:
            value = getattr(self, name)
            if isinstance(value, Exception):
                raise value
            return bool(value)
        return check


@dataclass
class MemoryLedger:
    fills: list[Fill] = field(default_factory=list)
    equity: list[EquitySnapshot] = field(default_factory=list)

    def record_fill(self, fill: Fill) -> bool:
        if any(f.venue_fill_id == fill.venue_fill_id for f in self.fills):
            return False
        self.fills.append(fill)
        return True

    def record_equity(self, snapshot: EquitySnapshot) -> bool:
        self.equity.append(snapshot)
        return True


@dataclass
class FixedLossReference:
    day: Decimal | None = Decimal(100_000)
    week: Decimal | None = Decimal(100_000)

    def day_start_equity(self, strategy_id: str, now: datetime) -> Decimal | None:
        return self.day

    def week_start_equity(self, strategy_id: str, now: datetime) -> Decimal | None:
        return self.week


def setup(
    strategy: StrategyBase,
    config_path: Path,
    *,
    switches: Switches | None = None,
    loss: FixedLossReference | None = None,
    ledger: MemoryLedger | None = None,
    now: datetime = NOW,
) -> StrategyBase:
    params = ParamsLoader(config_path)
    sw = switches or Switches()
    strategy.setup_base(
        params=params,
        kill_switch=KillSwitch(
            risk_events=sw.source("risk_events"),
            redis=sw.source("redis"),
            halt_file=sw.source("halt_file"),
            extra={"config.json": config_kill_source(params)},
        ),
        ledger=ledger or MemoryLedger(),
        loss_reference=loss or FixedLossReference(),
        clock=lambda: now,
    )
    return strategy


# ---------------------------------------------------------------------------
# Broker de opciones falso para el Wheel
# ---------------------------------------------------------------------------
SIGMA = 0.30
RATE = 0.045


@dataclass
class FakeOptionsBroker:
    cash_usd: Decimal = Decimal(100_000)
    value_usd: Decimal = Decimal(100_000)
    spots: dict[str, Decimal] = field(default_factory=lambda: {
        "AAPL": Decimal(150), "MSFT": Decimal(400), "SPY": Decimal(550)
    })
    held: dict[str, Decimal] = field(default_factory=dict)
    shorts: list[ShortOption] = field(default_factory=list)
    pending: list[PendingOrder] = field(default_factory=list)
    expiry: date = TODAY + timedelta(days=35)
    deltas: dict[OptionContract, float] = field(default_factory=dict)
    # Sin precio de opción y sin delta de broker = el caso "sin greeks y sin IV".
    quotes: bool = True
    broker_greeks: bool = False
    rate: float | None = RATE
    mids: dict[OptionContract, Decimal] = field(default_factory=dict)
    orders: list[tuple[str, OptionContract, int]] = field(default_factory=list)

    def today(self) -> date:
        return TODAY

    def cash(self) -> Decimal:
        return self.cash_usd

    def portfolio_value(self) -> Decimal:
        return self.value_usd

    def spot(self, symbol: str) -> Decimal | None:
        return self.spots.get(symbol)

    def shares(self, symbol: str) -> Decimal:
        return self.held.get(symbol, Decimal(0))

    def short_options(self) -> list[ShortOption]:
        return list(self.shorts)

    def pending_orders(self) -> list[PendingOrder]:
        return list(self.pending)

    def expirations(self, symbol: str) -> list[date]:
        return [self.expiry]

    def strikes(self, symbol: str, expiration: date, right: Right) -> list[Decimal]:
        spot = self.spots[symbol]
        return [(spot * Decimal(k) / 100).quantize(Decimal(1)) for k in range(70, 131, 5)]

    def option_mid(self, contract: OptionContract) -> Decimal | None:
        if contract in self.mids:
            return self.mids[contract]
        if not self.quotes:
            return None
        t = (contract.expiration - TODAY).days / 365
        price = bs_price(contract.right, float(self.spots[contract.underlying]),
                         float(contract.strike), t, RATE, SIGMA)
        return Decimal(str(round(price, 4)))

    def broker_delta(self, contract: OptionContract) -> float | None:
        if contract in self.deltas:
            return self.deltas[contract]
        return None

    def risk_free_rate(self) -> float | None:
        return self.rate

    def sell_to_open(self, contract: OptionContract, quantity: int) -> str | None:
        self.orders.append(("sell_to_open", contract, quantity))
        return f"order-{len(self.orders)}"

    def buy_to_close(self, contract: OptionContract, quantity: int) -> str | None:
        self.orders.append(("buy_to_close", contract, quantity))
        return f"order-{len(self.orders)}"


def put(underlying: str, strike: int | str, days: int = 35) -> OptionContract:
    return OptionContract(underlying, TODAY + timedelta(days=days), Decimal(strike), Right.PUT)


def call(underlying: str, strike: int | str, days: int = 35) -> OptionContract:
    return OptionContract(underlying, TODAY + timedelta(days=days), Decimal(strike), Right.CALL)
