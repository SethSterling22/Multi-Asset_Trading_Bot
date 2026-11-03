"""Comisión en especie (D-015): el router la registra, la reconciliación cuadra, y la
corrección de los fills anteriores es exacta, idempotente y la única llave para
levantar el kill switch."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_CEILING
from decimal import Decimal as D

import pytest
from sqlalchemy import Engine, create_engine, text

from brokers.base import Execution
from core.ledger.fills import fetch_fills, insert_fill
from core.ledger.positions import reconstruct
from execution.fee_correction import FEE_SUFFIX, corrections, lift_kill_switch
from execution.reconcile import position_mismatches, reconcile
from execution.router import OrderRouter
from risk.breakers import KillSwitch, risk_events_source
from strategies.crypto_rebalance.live import sleeve_cash
from tests.conftest import alembic, throwaway_database
from tests.execution.fakes import FakeVenue, never
from tests.execution.test_router_db import intent, small

pytestmark = pytest.mark.db
STRATEGY = "crypto_rebalance"
RATE = D("0.0025")


def fee(executions: list[Execution]) -> D:
    return sum(((e.qty * RATE).quantize(D("1e-9"), rounding=ROUND_CEILING)
                for e in executions), D(0))


@pytest.fixture
def engine() -> Iterator[Engine]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        eng = create_engine(url)
        yield eng
        eng.dispose()


def router(engine: Engine, venue: FakeVenue) -> OrderRouter:
    ks = KillSwitch(risk_events=risk_events_source(engine), redis=never, halt_file=never)
    return OrderRouter(engine, venue, ks, sleep=lambda _: None)


def test_fee_in_kind_is_recorded_and_reconciliation_is_clean(engine: Engine) -> None:
    venue = FakeVenue(fee_rate=RATE)
    r = router(engine, venue)
    r.submit(intent(qty="0.025617702"), small())
    with engine.connect() as conn:
        (f,) = fetch_fills(conn, STRATEGY)
    assert f.qty == D("0.025553657")                     # 0.025617702 − ceil(0.25 %)
    assert f.commission == D("0.000064045") * f.price
    # El flujo de caja es lo que se pagó de verdad: la cantidad BRUTA al precio.
    assert sleeve_cash([f], D(10000)) == D(10000) - D("0.025617702") * f.price
    assert reconcile(engine, venue, r, strategy_id=STRATEGY).ok


def test_correction_of_fills_recorded_before_d015(engine: Engine) -> None:
    venue = FakeVenue(fee_rate=RATE, hide_fees=True)     # el router de antes: sin comisión
    r = router(engine, venue)
    r.submit(intent(qty="0.876044135"), small())
    report = reconcile(engine, venue, r, strategy_id=STRATEGY)
    assert not report.ok and risk_events_source(engine)(STRATEGY) is True

    with engine.connect() as conn:
        todo = corrections(conn, venue, strategy_id=STRATEGY, fee_in_kind=fee)
    (c,) = todo
    assert (c.side.value, c.qty, c.venue_fill_id) == ("sell", D("0.002190111"), f"v-1{FEE_SUFFIX}")
    assert c.commission == c.qty * c.price and c.client_order_id == intent().client_order_id
    with engine.begin() as conn:
        assert insert_fill(conn, c) is True

    assert position_mismatches(engine, venue.name, STRATEGY, venue.positions()) == []
    with engine.connect() as conn:
        fills = fetch_fills(conn, STRATEGY)
        # Idempotente: ya no queda nada que corregir.
        assert corrections(conn, venue, strategy_id=STRATEGY, fee_in_kind=fee) == []
    (pos,) = reconstruct(fills).values()
    assert pos.net_qty == D("0.873854024")
    assert pos.realized_pnl == 0                          # se "vende" a su propio coste
    assert pos.total_commission == c.commission
    # La corrección no mueve caja: se pagó en especie.
    gross_paid = D("0.876044135") * fills[0].price
    assert sleeve_cash(fills, D(10000)) == D(10000) - gross_paid

    lift_kill_switch(engine, strategy_id=STRATEGY, actor="test", reason="corregido")
    assert risk_events_source(engine)(STRATEGY) is False
    with engine.connect() as conn:
        (row,) = conn.execute(text(
            "SELECT actor, reason FROM risk_events WHERE kind = 'kill_switch_off'")).all()
    assert row.actor == "test"
    # Y con todo cuadrado, la reconciliación siguiente no lo vuelve a poner.
    assert reconcile(engine, venue, r, strategy_id=STRATEGY).ok
    assert risk_events_source(engine)(STRATEGY) is False
