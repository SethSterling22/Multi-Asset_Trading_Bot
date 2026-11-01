"""El resto de strategies/base.py: feeds, ledger, equity de referencia, registro."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from core.ledger.equity import EquitySnapshot
from strategies.base import (
    Criticality,
    EquityCurveLossReference,
    FeedSpec,
    PostgresLedger,
    StrategyBase,
    check_feeds,
    day_start,
    week_start,
)
from tests.guards.helpers import NOW, Events, MemoryLedger, setup, write_config
from tests.ledger.fixtures import TRACED

pytestmark = pytest.mark.guards

TTL = timedelta(seconds=30)
SPECS = (
    FeedSpec("book", TTL, Criticality.CRITICAL),
    FeedSpec("funding", TTL, Criticality.IMPORTANT),
    FeedSpec("news", TTL, Criticality.OPTIONAL),
)


def test_feeds_all_fresh() -> None:
    health = check_feeds(SPECS, {n.name: NOW for n in SPECS}, NOW)
    assert health.verdict.ok and health.degraded == ()


def test_critical_feed_stale_or_missing_blocks() -> None:
    for seen in ({"funding": NOW, "news": NOW}, {"book": NOW - timedelta(minutes=5)}):
        health = check_feeds(SPECS, seen, NOW)
        assert not health.verdict.ok and health.verdict.code == "stale_feed"  # type: ignore[union-attr]


def test_important_feed_stale_degrades_optional_is_ignored() -> None:
    health = check_feeds(SPECS, {"book": NOW}, NOW)
    assert health.verdict.ok and health.degraded == ("funding",)


class Fed(StrategyBase):
    strategy_id = "funding_carry"
    required_feeds = (FeedSpec("book", TTL, Criticality.CRITICAL),
                      FeedSpec("funding", TTL, Criticality.IMPORTANT))


def test_gate_checks_declared_feeds(tmp_path: Path) -> None:
    events = Events()
    s = setup(Fed(), write_config(tmp_path))
    assert not s.trading_gate(equity=D(100_000)).ok
    assert [e["reason"] for e in events.rejections()] == ["stale_feed"]
    s.mark_feed("book")
    assert s.trading_gate(equity=D(100_000)).ok
    assert s.degraded_feeds == ("funding",)
    s.mark_feed("funding", NOW)
    assert s.trading_gate(equity=D(100_000)).ok and s.degraded_feeds == ()


def test_guard_helper_logs_and_returns(tmp_path: Path) -> None:
    from risk.verdict import ALLOWED, Rejected
    events = Events()
    s = setup(Fed(), write_config(tmp_path))
    assert s.guard(ALLOWED, action="x") is True
    assert s.guard(Rejected("why", "detail"), action="x", symbol="BTC") is False
    (rej,) = events.rejections()
    assert (rej["reason"], rej["detail"], rej["symbol"]) == ("why", "detail", "BTC")


def test_record_equity_goes_to_the_ledger(tmp_path: Path) -> None:
    ledger = MemoryLedger()
    s = setup(Fed(), write_config(tmp_path), ledger=ledger)
    snap = EquitySnapshot(NOW, "funding_carry", D(1), D(1), D(0), D(0))
    assert s.record_equity(snap) and ledger.equity == [snap]


def test_setup_checks_the_risk_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core import risk_floor
    monkeypatch.setattr(risk_floor, "MAX_LEVERAGE_MAJORS", 3.0)
    with pytest.raises(RuntimeError, match="MUTADO"):
        setup(Fed(), write_config(tmp_path))


def test_day_and_week_start_in_market_timezone() -> None:
    ny = timezone(timedelta(hours=-5))
    # Martes 2026-11-03 02:00 UTC = lunes 2026-11-02 21:00 en Nueva York.
    now = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    assert day_start(now, ny) == datetime(2026, 11, 2, tzinfo=ny)
    assert week_start(now, ny) == datetime(2026, 11, 2, tzinfo=ny)      # lunes
    assert week_start(datetime(2026, 11, 8, 12, tzinfo=UTC), UTC) == datetime(
        2026, 11, 2, tzinfo=UTC
    )


class FakeConn:
    def __init__(self, value: Any = None, inserted: bool = True) -> None:
        self.value, self.inserted, self.calls = value, inserted, []

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> FakeConn:
        self.calls.append(params)
        return self

    def scalar_one_or_none(self) -> Any:
        return self.value

    def first(self) -> Any:
        return (1,) if self.inserted else None

    def one(self) -> Any:
        raise AssertionError("no debería releer")


class FakeEngine:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def connect(self) -> FakeConn:
        return self.conn

    def begin(self) -> FakeConn:
        return self.conn


def test_equity_curve_loss_reference_queries_first_snapshot_since_start() -> None:
    conn = FakeConn(value=12345.5)
    ref = EquityCurveLossReference(FakeEngine(conn), UTC)  # type: ignore[arg-type]
    assert ref.day_start_equity("wheel", NOW) == D("12345.5")
    assert conn.calls[-1]["since"] == datetime(2026, 11, 2, tzinfo=UTC)
    assert ref.week_start_equity("wheel", NOW) == D("12345.5")
    assert conn.calls[-1]["since"] == datetime(2026, 11, 2, tzinfo=UTC)   # lunes
    empty = EquityCurveLossReference(FakeEngine(FakeConn(value=None)), UTC)  # type: ignore[arg-type]
    assert empty.day_start_equity("wheel", NOW) is None


def test_postgres_ledger_writes_in_a_transaction() -> None:
    conn = FakeConn(inserted=True)
    ledger = PostgresLedger(FakeEngine(conn))  # type: ignore[arg-type]
    assert ledger.record_fill(TRACED[0]) is True
    snap = EquitySnapshot(NOW, "trend", D(1), D(1), D(0), D(0))
    assert ledger.record_equity(snap) is True
