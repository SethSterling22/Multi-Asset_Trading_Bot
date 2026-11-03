"""El historial diario contra Postgres, con cifras calculadas a mano.

Dos días, dos sleeves:
- crypto_rebalance: cierra el día 1 en 10 000 (sin posiciones). El día 2 compra 0.01 BTC
  a 60 000 con 10 $ de comisión y cierra en 9 950 con −40 de no realizado:
      Δ = −50 = realizado neto (−10) + Δ no realizado (−40) + fuera del ledger (0).
- wheel: 100 000 → 99 900 sin un solo fill: los −100 son "fuera del ledger", que es
  justo lo que pasa cuando un sleeve toma la equity de la cuenta entera del broker.
El día 2 la reconciliación encuentra 2 mismatches en crypto_rebalance y lo para.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, text

from core.ledger.fills import AssetType, Fill, Side, insert_fills
from tests.conftest import alembic, throwaway_database

pytestmark = pytest.mark.db
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import history  # noqa: E402
from api.app import create_app  # noqa: E402
from api.queries import readonly_engine  # noqa: E402

DAY1 = datetime(2026, 11, 1, tzinfo=UTC)
DAY2 = datetime(2026, 11, 2, tzinfo=UTC)
NOW = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)


def at(day: datetime, hour: int) -> datetime:
    return day.replace(hour=hour)


@pytest.fixture(scope="module")
def setup() -> Iterator[tuple[TestClient, str]]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        writer = create_engine(url)
        with writer.begin() as conn:
            for coid, sym, status, reason in (
                    ("crypto_rebalance-BTCUSD-1-1", "BTC/USD", "FILLED", None),
                    ("crypto_rebalance-ETHUSD-1-1", "ETH/USD", "REJECTED", "insufficient balance")):
                conn.execute(text("""
                    INSERT INTO orders (client_order_id, ts_intent, venue, symbol, asset_type,
                                        side, qty, order_type, status, reject_reason,
                                        strategy_id, risk_floor_hash)
                    VALUES (:coid, :ts, 'alpaca', :sym, 'crypto_spot', 'buy', 0.01, 'market',
                            CAST(:status AS order_status), :reason, 'crypto_rebalance', '\\x00')
                """), {"coid": coid, "ts": at(DAY2, 9), "sym": sym, "status": status,
                       "reason": reason})
            insert_fills(conn, [Fill(
                ts=at(DAY2, 9), venue="alpaca", symbol="BTC/USD",
                asset_type=AssetType.CRYPTO_SPOT, side=Side.BUY, qty=D("0.01"),
                price=D(60000), commission=D(10), decision_price=D(59990),
                strategy_id="crypto_rebalance", venue_fill_id="v-1",
                client_order_id="crypto_rebalance-BTCUSD-1-1")])
            snaps = [("crypto_rebalance", at(DAY1, 10), 10_000, 0),
                     ("crypto_rebalance", at(DAY2, 10), 9_950, -40),
                     ("wheel", at(DAY1, 15), 100_000, 0),
                     ("wheel", at(DAY2, 15), 99_900, 0)]
            for sid, ts, eq, unreal in snaps:
                conn.execute(text(
                    "INSERT INTO equity_curve (ts, strategy_id, equity_usd, allocated_usd,"
                    " gross_notional, unrealized_pnl) VALUES (:ts, :s, :e, :e, 0, :u)"),
                    {"ts": ts, "s": sid, "e": eq, "u": unreal})
            conn.execute(text("""
                INSERT INTO reconciliation_runs (ts, venue, mismatches, detail, action_taken)
                VALUES (:ts, 'alpaca', 2, CAST(:d AS jsonb), 'safe_mode')
            """), {"ts": at(DAY2, 11), "d": '{"strategy_id": "crypto_rebalance", "mismatches":'
                                            ' ["BTCUSD: ledger 0.01 ≠ venue 0.009975"]}'})
            conn.execute(text("""
                INSERT INTO risk_events (ts, kind, scope, reason, actor)
                VALUES (:ts, 'kill_switch_on', 'crypto_rebalance', 'reconciliación: BTCUSD',
                        'reconciler')
            """), {"ts": at(DAY2, 11)})
        writer.dispose()
        reader = readonly_engine(url.render_as_string(hide_password=False))
        yield TestClient(create_app(reader, redis_url=None, clock=lambda: NOW)), \
            url.render_as_string(hide_password=False)
        reader.dispose()


def rows_by(url: str) -> dict[tuple[str, str], history.DayRow]:
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = history.daily_history(conn, until=NOW.date())
    engine.dispose()
    return {(r.day.isoformat(), r.strategy_id): r for r in rows}


def test_day_change_is_decomposed_to_the_cent(setup: tuple[TestClient, str]) -> None:
    rows = rows_by(setup[1])
    crypto = rows[("2026-11-02", "crypto_rebalance")]
    assert (crypto.open_equity, crypto.close_equity, crypto.change) == (10_000, 9_950, -50)
    assert crypto.realized_net == D(-10) and crypto.fees == D(10)
    assert crypto.unrealized_change == -40
    assert crypto.outside_ledger == 0
    assert crypto.slippage == D("0.10")                 # (60 000 − 59 990) × 0.01
    assert (crypto.n_fills, crypto.n_orders, crypto.n_rejected, crypto.mismatches) == (1, 2, 1, 2)

    wheel = rows[("2026-11-02", "wheel")]
    assert wheel.change == -100 and wheel.realized_net == 0 and wheel.outside_ledger == -100
    # El primer día no tiene cierre anterior: abre en su propio primer snapshot.
    assert rows[("2026-11-01", "wheel")].change == 0


def test_history_view_shows_what_happened_that_day(setup: tuple[TestClient, str]) -> None:
    c, _ = setup
    html = c.get("/historial?day=2026-11-02").text
    for needle in ("crypto_rebalance-BTCUSD-1-1", "insufficient balance",
                   "BTCUSD: ledger 0.01 ≠ venue 0.009975", "reconciler", "safe_mode",
                   "−$100.00"):
        assert needle in html, needle
    # Día sin nada: responde igual, con los vacíos explicados.
    assert "Ningún fill este día" in c.get("/historial?day=2026-10-01").text
    # Una fecha inválida no rompe la vista: cae en hoy.
    assert c.get("/historial?day=no-es-fecha").status_code == 200
    assert c.get("/partials/historial?day=2026-11-01").status_code == 200


def test_a_halted_sleeve_is_visible(setup: tuple[TestClient, str]) -> None:
    c, _ = setup
    salud = c.get("/salud").text
    assert "PARADO" in salud and "reconciliación: BTCUSD" in salud and "reconciler" in salud
    header = c.get("/partials/header").text
    assert "KILL SWITCH" in header and "crypto_rebalance: risk_events" in header


def test_drawdown_names_its_sleeve(setup: tuple[TestClient, str]) -> None:
    header = setup[0].get("/partials/header").text
    assert "peor sleeve: crypto_rebalance" in header        # −0.5 % frente a −0.1 %


def test_csv_exports(setup: tuple[TestClient, str]) -> None:
    c, _ = setup
    r = c.get("/export/historial.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert list(rows[0]) == list(history.HISTORY_COLUMNS)
    crypto = next(x for x in rows if x["day"] == "2026-11-02"
                  and x["strategy_id"] == "crypto_rebalance")
    assert (float(crypto["change"]), D(crypto["realized_net"])) == (-50.0, D(-10))
    fills = list(csv.DictReader(io.StringIO(c.get("/export/fills.csv?day=2026-11-02").text)))
    assert [(f["symbol"], f["side"], D(f["notional"])) for f in fills] == [
        ("BTC/USD", "buy", D(600))]
    assert len(list(csv.DictReader(io.StringIO(c.get("/export/fills.csv").text)))) == 1


def test_pnl_shows_unrealized_next_to_realized(setup: tuple[TestClient, str]) -> None:
    html = setup[0].get("/pnl").text
    assert "No realizado</th>" in html and "−$40.00" in html
