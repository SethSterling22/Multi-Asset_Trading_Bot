"""Router y reconciliación contra Postgres real y un venue falso, incluido el caos:
caída entre el INSERT y el POST, entre el POST y el ack, timeouts y ids repetidos.

El router confirma sus transacciones (es lo que se prueba), así que cada test usa
su propia base desechable en lugar de la conexión con rollback de `db`.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal as D

import pytest
from sqlalchemy import Engine, create_engine, text

from brokers.base import OrderStatus
from core.ledger.fills import AssetType, fetch_fills
from core.risk_floor import MAX_ORDERS_PER_MINUTE, floor_hash
from execution.idempotency import OrderIntent, client_order_id, get_order, record_intent
from execution.reconcile import reconcile
from execution.router import OrderRouter
from risk.breakers import KillSwitch, risk_events_source
from risk.pretrade import ProposedState
from tests.conftest import alembic, throwaway_database
from tests.execution.fakes import FakeVenue, never
from tests.guards.helpers import Events

pytestmark = pytest.mark.db
STRATEGY = "crypto_rebalance"


@pytest.fixture
def engine() -> Iterator[Engine]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        eng = create_engine(url)
        yield eng
        eng.dispose()


def kill_switch(engine: Engine, *, halted: bool = False) -> KillSwitch:
    return KillSwitch(risk_events=risk_events_source(engine), redis=never,
                      halt_file=lambda _: halted)


def intent(symbol: str = "BTC/USD", side: str = "buy", qty: str = "0.01",
           bar: int = 1) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id(STRATEGY, symbol, bar), venue="fake", symbol=symbol,
        asset_type=AssetType.CRYPTO_SPOT, side=side, qty=D(qty), strategy_id=STRATEGY,
        decision_price=D(60000), risk_floor_hash=floor_hash(),
    )


def small(symbol: str = "BTC/USD") -> ProposedState:
    """Una compra de 600 $ en una cartera de 10 000: pasa todos los checks."""
    return ProposedState(symbol=symbol, asset_type=AssetType.CRYPTO_SPOT, equity=D(10000),
                         position_qty=D("0.01"), position_notional=D(600), leverage=D(1),
                         gross_notional=D(600), net_notional=D(600), open_positions=1,
                         risk_usd=None)


def rows(engine: Engine, sql: str) -> list[object]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql)))


def make_router(engine: Engine, venue: FakeVenue, **kw: object) -> OrderRouter:
    return OrderRouter(engine, venue, kill_switch(engine, **kw), sleep=lambda _: None)  # type: ignore[arg-type]


# -- camino feliz e idempotencia -------------------------------------------------
def test_filled_order_lands_in_orders_and_in_the_ledger(engine: Engine) -> None:
    venue = FakeVenue()
    result = make_router(engine, venue).submit(intent(), small())
    assert result.status is OrderStatus.FILLED
    with engine.connect() as conn:
        stored = get_order(conn, result.client_order_id)
        (fill,) = fetch_fills(conn, STRATEGY)
    assert stored is not None and stored.status is OrderStatus.FILLED
    assert stored.venue_order_id == "v-1"
    assert (fill.qty, fill.venue_fill_id, fill.client_order_id) == (D("0.01"), "v-1",
                                                                     result.client_order_id)
    assert fill.decision_price == D(60000)


def test_duplicate_intent_is_never_resent(engine: Engine) -> None:
    venue = FakeVenue()
    router = make_router(engine, venue)
    router.submit(intent(), small())
    again = router.submit(intent(), small())
    assert again.reason == "duplicate"
    assert venue.submits == 1
    assert len(rows(engine, "SELECT * FROM fills")) == 1


# -- rechazos antes de escribir nada ---------------------------------------------
def test_kill_switch_blocks_before_insert(engine: Engine, events: Events) -> None:
    venue = FakeVenue()
    result = make_router(engine, venue, halted=True).submit(intent(), small())
    assert (result.status, result.reason) == (None, "kill_switch")
    assert venue.submits == 0
    assert rows(engine, "SELECT * FROM orders") == []
    (rejection,) = events.rejections()
    assert rejection["reason"] == "kill_switch" and "halt_file" in rejection["detail"]


def test_pretrade_rejection_blocks_before_insert(engine: Engine) -> None:
    venue = FakeVenue()
    too_big = ProposedState(symbol="BTC/USD", asset_type=AssetType.CRYPTO_SPOT,
                            equity=D(10000), position_qty=D(1), position_notional=D(3000),
                            leverage=D(1), gross_notional=D(3000), net_notional=D(3000),
                            open_positions=1, risk_usd=None)
    result = make_router(engine, venue).submit(intent(), too_big)
    assert result.reason == "position_notional"
    assert venue.submits == 0
    assert rows(engine, "SELECT * FROM orders") == []


def test_order_rate_counts_every_intent(engine: Engine) -> None:
    venue = FakeVenue(submit_fault="reject")     # las fallidas también cuentan
    router = make_router(engine, venue)
    for bar in range(MAX_ORDERS_PER_MINUTE):
        assert router.submit(intent(bar=bar), small()).status is OrderStatus.REJECTED
    blocked = router.submit(intent(bar=99), small())
    assert blocked.reason == "order_rate"
    assert venue.submits == MAX_ORDERS_PER_MINUTE


def test_min_hold_blocks_undoing_a_fresh_fill(engine: Engine) -> None:
    venue = FakeVenue()
    router = make_router(engine, venue)
    router.submit(intent(), small())
    result = router.submit(intent(side="sell", bar=2), small())
    assert result.reason == "min_hold"
    assert venue.submits == 1


def test_venue_rejection_is_recorded_without_a_fill(engine: Engine) -> None:
    venue = FakeVenue(submit_fault="reject")
    result = make_router(engine, venue).submit(intent(), small())
    assert result.status is OrderStatus.REJECTED
    (row,) = rows(engine, "SELECT status, reject_reason FROM orders")
    assert row.status == "REJECTED" and "insufficient" in row.reject_reason  # type: ignore[attr-defined]
    assert rows(engine, "SELECT * FROM fills") == []


# -- caos ------------------------------------------------------------------------
def test_ack_lost_after_the_venue_accepted_it(engine: Engine) -> None:
    """Timeout en el POST con la orden ya en el venue: se consulta, no se reenvía."""
    venue = FakeVenue(submit_fault="timeout_after")
    result = make_router(engine, venue).submit(intent(), small())
    assert result.status is OrderStatus.FILLED
    assert venue.submits == 1
    assert len(rows(engine, "SELECT * FROM fills")) == 1


def test_timeout_with_venue_unreachable_leaves_unknown_and_reconcile_halts(
        engine: Engine) -> None:
    venue = FakeVenue(submit_fault="timeout_before", lookup_down=True)
    router = make_router(engine, venue)
    result = router.submit(intent(), small())
    assert result.status is OrderStatus.UNKNOWN
    assert venue.submits == 1

    # El venue vuelve, y la orden nunca llegó: es un mismatch, y modo seguro.
    venue.lookup_down = False
    report = reconcile(engine, venue, router, strategy_id=STRATEGY)
    assert not report.ok and "sin rastro" in report.mismatches[0]
    assert venue.submits == 1                                   # nunca se reenvía
    assert risk_events_source(engine)(STRATEGY) is True
    (run,) = rows(engine, "SELECT mismatches, action_taken FROM reconciliation_runs")
    assert (run.mismatches, run.action_taken) == (1, "safe_mode")  # type: ignore[attr-defined]
    # Con el kill switch puesto por la reconciliación, el router ya no deja pasar nada.
    assert router.submit(intent(bar=2), small()).reason == "kill_switch"


def test_crash_between_insert_and_post(engine: Engine) -> None:
    """La intención quedó escrita y el proceso murió antes del POST."""
    venue = FakeVenue()
    with engine.begin() as conn:
        record_intent(conn, intent())
    router = make_router(engine, venue)
    report = reconcile(engine, venue, router, strategy_id=STRATEGY)
    assert len(report.mismatches) == 1
    assert venue.submits == 0
    with engine.connect() as conn:
        stored = get_order(conn, intent().client_order_id)
    assert stored is not None and stored.status is OrderStatus.UNKNOWN
    # El mismo ciclo replanificado no la duplica: el id ya existe.
    assert router.submit(intent(), small()).reason == "kill_switch"


def test_crash_between_post_and_ack(engine: Engine) -> None:
    """La orden llegó y se llenó, pero el proceso murió antes de registrarlo."""
    venue = FakeVenue()
    with engine.begin() as conn:
        record_intent(conn, intent())
    venue.submit_market_order(client_order_id=intent().client_order_id, symbol="BTC/USD",
                              side="buy", qty=D("0.01"))
    router = make_router(engine, venue)
    report = reconcile(engine, venue, router, strategy_id=STRATEGY)
    assert report.ok and report.orders_resolved == 1
    assert len(rows(engine, "SELECT * FROM fills")) == 1
    assert risk_events_source(engine)(STRATEGY) is False
    # Reconciliar otra vez no duplica nada: el fill es idempotente por venue_fill_id.
    assert reconcile(engine, venue, router, strategy_id=STRATEGY).ok
    assert len(rows(engine, "SELECT * FROM fills")) == 1


def test_open_order_is_picked_up_later(engine: Engine) -> None:
    venue = FakeVenue(leave_open=True)
    router = make_router(engine, venue)
    assert router.submit(intent(), small()).status is OrderStatus.NEW
    assert rows(engine, "SELECT * FROM fills") == []
    # El venue la llena después; la siguiente reconciliación la recoge.
    coid = intent().client_order_id
    venue.leave_open = False
    del venue.orders[coid]
    venue.submit_market_order(client_order_id=coid, symbol="BTC/USD", side="buy",
                              qty=D("0.01"))
    assert reconcile(engine, venue, router, strategy_id=STRATEGY).ok
    assert len(rows(engine, "SELECT * FROM fills")) == 1


def test_position_mismatch_halts(engine: Engine) -> None:
    venue = FakeVenue()
    router = make_router(engine, venue)
    router.submit(intent(), small())
    venue.held["BTCUSD"] += D("0.5")        # alguien operó fuera del bot
    report = reconcile(engine, venue, router, strategy_id=STRATEGY)
    assert report.mismatches == ["BTCUSD: ledger 0.01 ≠ venue 0.51"]
    assert risk_events_source(engine)(STRATEGY) is True
    # Un segundo mismatch no duplica el evento del kill switch.
    reconcile(engine, venue, router, strategy_id=STRATEGY)
    assert len(rows(engine, "SELECT * FROM risk_events WHERE kind = 'kill_switch_on'")) == 1


def test_unreachable_venue_is_not_ok_but_does_not_halt(engine: Engine) -> None:
    venue = FakeVenue(positions_down=True)
    report = reconcile(engine, venue, make_router(engine, venue), strategy_id=STRATEGY)
    assert not report.ok and report.unreachable == 1 and not report.mismatches
    assert risk_events_source(engine)(STRATEGY) is False
