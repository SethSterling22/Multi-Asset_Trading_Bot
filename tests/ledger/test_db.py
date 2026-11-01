"""El ledger contra Postgres + TimescaleDB, con la migración real."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.ledger.equity import snapshot, write_snapshot
from core.ledger.fills import FillConflictError, fetch_fills, insert_fill, insert_fills
from core.ledger.positions import load_positions_view, reconstruct
from tests.conftest import alembic, throwaway_database
from tests.ledger.fixtures import FUNDING, OPTION, TRACED, at

pytestmark = pytest.mark.db

ALL = [*TRACED, *OPTION, FUNDING]


def test_migration_round_trip() -> None:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        alembic(url, "downgrade", "base")
        alembic(url, "upgrade", "head")


def test_fills_round_trip(db: Connection) -> None:
    assert insert_fills(db, ALL) == len(ALL)
    assert fetch_fills(db) == sorted(ALL, key=lambda f: f.ts)


def test_double_insert_is_a_noop(db: Connection) -> None:
    assert insert_fill(db, TRACED[0]) is True
    assert insert_fill(db, TRACED[0]) is False
    assert db.execute(text("SELECT count(*) FROM fills")).scalar_one() == 1


def test_same_id_with_different_content_raises(db: Connection) -> None:
    insert_fill(db, TRACED[0])
    with pytest.raises(FillConflictError):
        insert_fill(db, replace(TRACED[0], qty=D(3)))


def test_view_matches_replay(db: Connection) -> None:
    insert_fills(db, ALL)
    view = load_positions_view(db)
    replayed = reconstruct(fetch_fills(db))
    assert view.keys() == replayed.keys()
    for key, row in view.items():
        pos = replayed[key]
        assert (row.net_qty, row.net_cost, row.total_commission, row.total_funding) == (
            pos.net_qty, pos.net_cost, pos.total_commission, pos.total_funding
        )
    # La opción cerrada no aparece; el corto de trend, sí.
    (only,) = view.values()
    assert only.net_qty == D(-1)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE fills SET qty = 99", "DELETE FROM fills", "TRUNCATE fills CASCADE"],
)
def test_fills_are_append_only(db: Connection, statement: str) -> None:
    insert_fill(db, TRACED[0])
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text(statement))


def test_side_must_be_a_known_value(db: Connection) -> None:
    # 'BUY' en mayúsculas contaría como venta en la vista `positions`.
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO fills (ts, venue, symbol, asset_type, side, qty, price,
                               strategy_id, venue_fill_id)
            VALUES (now(), 'coinbase', 'BTC-USD', 'crypto_spot', 'BUY', 1, 100,
                    'trend', 'x-1')
        """))


def test_negative_commission_is_rejected_by_the_database(db: Connection) -> None:
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO fills (ts, venue, symbol, asset_type, side, qty, price,
                               commission, strategy_id, venue_fill_id)
            VALUES (now(), 'coinbase', 'BTC-USD', 'crypto_spot', 'buy', 1, 100,
                    -0.5, 'trend', 'x-2')
        """))


def test_unknown_strategy_is_rejected(db: Connection) -> None:
    with pytest.raises(IntegrityError):
        insert_fill(db, replace(TRACED[0], strategy_id="nope"))


def test_equity_snapshot_is_written_once(db: Connection) -> None:
    insert_fills(db, ALL)
    snap = snapshot(
        fetch_fills(db), strategy_id="trend", allocated_usd=D(1000),
        marks={("coinbase", "BIP-20DEC30-CDE"): D(85)}, ts=at(10),
    )
    # realizado 10 − fees 0 + funding −3.25 + no realizado 5 -> 1011.75
    assert snap.equity_usd == D("1011.75")
    assert write_snapshot(db, snap) is True
    assert write_snapshot(db, snap) is False
    stored = db.execute(text("SELECT equity_usd FROM equity_curve")).scalar_one()
    assert stored == pytest.approx(1011.75)
    later = replace(snap, ts=snap.ts + timedelta(hours=1))
    assert write_snapshot(db, later) is True
