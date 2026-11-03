"""El ciclo del rebalanceador cripto de punta a punta: reconciliación, snapshot,
plan, router y ledger, con Postgres real y el venue falso."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from core import log
from core.config.models import TradingConfig
from core.ledger.fills import AssetType, Fill, Side, fetch_fills
from execution.router import OrderRouter
from risk.breakers import KillSwitch, risk_events_source
from strategies.base import EquityCurveLossReference, PostgresLedger, StaticParams
from strategies.crypto_rebalance.live import (
    STRATEGY_ID,
    RoutedSpotBroker,
    floor_to,
    holdings,
    run_cycle,
    sleeve_cash,
)
from strategies.crypto_rebalance.rebalancer import CryptoRebalancer
from tests.conftest import alembic, throwaway_database
from tests.execution.fakes import FakeVenue, never

# CI corre los guards solo con el grupo `dev`, sin el SDK de Alpaca.
pytest.importorskip("alpaca")
from brokers.alpaca_crypto import pair

BUDGET = D(10000)
pytestmark = pytest.mark.db


# -- sin base de datos -------------------------------------------------------------
def fill(side: Side, qty: str, price: str, commission: str = "0") -> Fill:
    return Fill(ts=datetime(2026, 11, 2, tzinfo=UTC), venue="fake", symbol="BTC/USD",
                asset_type=AssetType.CRYPTO_SPOT, side=side, qty=D(qty), price=D(price),
                strategy_id=STRATEGY_ID, venue_fill_id=f"{side}-{qty}",
                commission=D(commission))


def test_sleeve_cash_is_budget_plus_fill_cash_flow_net_of_costs() -> None:
    fills = [fill(Side.BUY, "0.1", "60000", "1.5"), fill(Side.SELL, "0.04", "65000", "0.5")]
    assert sleeve_cash(fills, BUDGET) == BUDGET - 6000 - D("1.5") + 2600 - D("0.5")
    assert holdings(fills) == {"BTCUSD": D("0.06")}


def test_floor_to_never_rounds_up() -> None:
    assert floor_to(D("0.0123456789"), D("0.000001")) == D("0.012345")
    with pytest.raises(ValueError):
        floor_to(D(1), D(0))


# -- el ciclo contra Postgres --------------------------------------------------------
@pytest.fixture
def engine() -> Iterator[Engine]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        eng = create_engine(url)
        yield eng
        eng.dispose()


class Sleeve:
    def __init__(self, engine: Engine, venue: FakeVenue, *, dry_run: bool = False) -> None:
        self.engine, self.venue = engine, venue
        kill = KillSwitch(risk_events=risk_events_source(engine), redis=never, halt_file=never)
        self.router = OrderRouter(engine, venue, kill, sleep=lambda _: None)
        self.broker = RoutedSpotBroker(engine, venue, self.router, budget_usd=BUDGET,
                                       pair=pair, dry_run=dry_run)
        self.bot = CryptoRebalancer(self.broker)
        self.ledger = PostgresLedger(engine)
        self.bot.setup_base(params=StaticParams(TradingConfig.load()), kill_switch=kill,
                            ledger=self.ledger,
                            loss_reference=EquityCurveLossReference(engine, UTC),
                            clock=lambda: datetime.now(UTC))

    def cycle(self) -> list[str]:
        return run_cycle(self.engine, self.venue, self.router, self.broker, self.bot,
                         budget_usd=BUDGET, now=datetime.now(UTC),
                         record_equity=self.ledger.record_equity, logger=log.get_logger())

    def count(self, table: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def value(fills: list[Fill], symbol: str) -> D:
    return sum((f.qty * f.price for f in fills if f.symbol == symbol), D(0))


def test_first_cycle_buys_to_target_within_the_sleeve_budget(engine: Engine) -> None:
    sleeve = Sleeve(engine, FakeVenue())
    ids = sleeve.cycle()
    assert len(ids) == 2
    with engine.connect() as conn:
        fills = fetch_fills(conn, STRATEGY_ID)
    # config.json: 40 % cripto a partes iguales entre BTC y ETH -> ~2000 $ cada uno.
    for symbol in ("BTC/USD", "ETH/USD"):
        assert D(1990) < value(fills, symbol) <= D(2000)
    assert sleeve_cash(fills, BUDGET) >= D(6000)
    # Un snapshot antes de operar y otro después, ya con las posiciones.
    with engine.connect() as conn:
        snaps = conn.execute(text(
            "SELECT gross_notional FROM equity_curve ORDER BY ts")).scalars().all()
    assert snaps[0] == 0 and snaps[1] > 3900
    assert sleeve.count("reconciliation_runs") == 1
    # El motivo de cada orden queda con ella, y el precio de cada posición con el snapshot.
    with engine.connect() as conn:
        reasons = conn.execute(text("SELECT reason FROM orders")).scalars().all()
        marks = conn.execute(text("SELECT symbol, qty FROM position_marks")).all()
    assert all(r and "target 20.00%" in r for r in reasons)
    assert sorted(m.symbol for m in marks) == ["BTC/USD", "ETH/USD"]


def test_second_cycle_is_quiet(engine: Engine) -> None:
    sleeve = Sleeve(engine, FakeVenue())
    sleeve.cycle()
    assert sleeve.cycle() == []
    assert sleeve.count("orders") == 2


def test_buys_never_exceed_the_shared_account_cash(engine: Engine) -> None:
    venue = FakeVenue(cash_usd=D(1500))     # la Wheel se llevó el resto de la caja
    sleeve = Sleeve(engine, venue)
    sleeve.cycle()
    with engine.connect() as conn:
        spent = sum((f.qty * f.price for f in fetch_fills(conn, STRATEGY_ID)), D(0))
    assert spent <= D(1500)
    assert venue.cash_usd >= 0


def test_sell_right_after_buy_is_blocked_by_min_hold(engine: Engine) -> None:
    venue = FakeVenue()
    sleeve = Sleeve(engine, venue)
    sleeve.cycle()
    venue.prices["BTCUSD"] *= 2                     # BTC sobrepondera: el plan quiere vender
    sleeve.broker.begin(datetime.now(UTC))
    assert [o.side for o in sleeve.bot.plan()] == ["sell"]
    assert sleeve.cycle() == []
    assert sleeve.count("orders") == 2              # el rechazo no escribió nada


def test_dry_run_sends_nothing(engine: Engine) -> None:
    venue = FakeVenue()
    sleeve = Sleeve(engine, venue, dry_run=True)
    assert sleeve.cycle() == []
    # Y aunque alguien llame al broker directamente, en dry-run no envía.
    sleeve.broker.begin(datetime.now(UTC))
    assert sleeve.broker.market_buy("BTC", 2000.0) == ""
    assert venue.submits == 0
    assert sleeve.count("orders") == 0


def test_mismatch_skips_the_cycle_and_halts(engine: Engine) -> None:
    venue = FakeVenue(held={"BTCUSD": D(1)})       # algo que el ledger no conoce
    sleeve = Sleeve(engine, venue)
    assert sleeve.cycle() == []
    assert venue.submits == 0
    assert risk_events_source(engine)(STRATEGY_ID) is True
    # Parado, pero la equity se sigue registrando: el historial no se queda en blanco.
    assert sleeve.count("equity_curve") == 1


def test_proposed_state_reflects_the_order(engine: Engine) -> None:
    sleeve = Sleeve(engine, FakeVenue())
    sleeve.broker.begin(datetime.now(UTC))
    s: Any = sleeve.broker.proposed("BTC/USD", "buy", D("0.03"))
    assert s.position_qty == D("0.03")
    assert s.position_notional == D("0.03") * D(60000)
    assert s.equity == BUDGET and s.open_positions == 1 and s.leverage == 1
