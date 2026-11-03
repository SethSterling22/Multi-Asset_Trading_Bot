"""La referencia de la pérdida diaria mide P&L, no capital asignado (D-016)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import Engine, create_engine, text

from risk.breakers import loss_limits
from strategies.base import EquityCurveLossReference
from tests.conftest import alembic, throwaway_database

pytestmark = pytest.mark.db
DAY = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        eng = create_engine(url)
        yield eng
        eng.dispose()


def snap(engine: Engine, minutes: int, equity: float, allocated: float) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO equity_curve (ts, strategy_id, equity_usd, allocated_usd, gross_notional)"
            " VALUES (:ts, 'wheel', :e, :a, 0)"),
            {"ts": DAY + timedelta(minutes=minutes), "e": equity, "a": allocated})


def test_switching_to_an_own_budget_is_not_a_loss(engine: Engine) -> None:
    # Antes de D-016 el Wheel escribía la cuenta entera como equity y como asignado.
    snap(engine, 0, 99_914.56, 99_914.56)
    snap(engine, 15, 90_000, 90_000)                   # ahora: su presupuesto de 90 000
    ref = EquityCurveLossReference(engine, UTC)
    start = ref.day_start_equity("wheel", DAY + timedelta(minutes=20))
    assert start == 90_000
    assert loss_limits(equity=D(90000), day_start_equity=start, week_start_equity=start,
                       daily_limit=0.03).ok


def test_a_real_loss_still_counts_in_full(engine: Engine) -> None:
    snap(engine, 0, 90_000, 90_000)
    snap(engine, 15, 87_000, 90_000)                   # −3 000 con la asignación igual
    ref = EquityCurveLossReference(engine, UTC)
    start = ref.day_start_equity("wheel", DAY + timedelta(minutes=20))
    assert start == 90_000
    v = loss_limits(equity=D(87000), day_start_equity=start, week_start_equity=start,
                    daily_limit=0.03)
    assert not v.ok


def test_no_snapshot_today_is_still_none(engine: Engine) -> None:
    snap(engine, -24 * 60, 90_000, 90_000)            # ayer
    assert EquityCurveLossReference(engine, UTC).day_start_equity("wheel", DAY) is None
