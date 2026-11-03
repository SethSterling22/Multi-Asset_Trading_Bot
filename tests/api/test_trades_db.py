"""El diario de operaciones contra Postgres, con cifras calculadas a mano.

BTC (orden anterior a D-015, sin motivo): compra bruta 0.0256177 a 78 000 y su
corrección de comisión en especie, 0.000064 a 78 000 (= 4.992 $). Lote neto 0.0255537,
marca 77 000:
    bruto = (77 000 − 78 000) · 0.0255537 = −25.5537;  neto = −25.5537 − 4.992 = −30.5457
ETH: compra 1 a 3 000 con 7.50 de comisión; venta 0.4 a 3 300 con 3.30.
    cerrada: 0.4 · 300 = +120 bruto; comisiones 7.50·0.4 + 3.30 = 6.30; neto 113.70
    abierta: 0.6 a 3 000, marca 3 100: +60 bruto; comisiones 4.50; neto 55.50
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import Engine, create_engine, text

from core.ledger.fills import AssetType, Fill, Side, insert_fills
from tests.conftest import alembic, throwaway_database

pytestmark = pytest.mark.db
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import trades  # noqa: E402
from api.app import create_app  # noqa: E402
from api.queries import readonly_engine  # noqa: E402

T = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
S = "crypto_rebalance"


def fill(i: int, symbol: str, side: Side, qty: str, price: str, commission: str = "0",
         coid: str | None = None, vid: str | None = None) -> Fill:
    return Fill(ts=T + timedelta(hours=i), venue="alpaca", symbol=symbol,
                asset_type=AssetType.CRYPTO_SPOT, side=side, qty=D(qty), price=D(price),
                commission=D(commission), strategy_id=S, venue_fill_id=vid or f"v-{i}",
                client_order_id=coid)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        eng = create_engine(url)
        with eng.begin() as conn:
            for coid, reason in (("btc-1", None), ("eth-1", "weight 0.00% < target 20.00%"),
                                 ("eth-2", "weight 26.00% > target 20.00%")):
                conn.execute(text("""
                    INSERT INTO orders (client_order_id, venue, symbol, asset_type, side, qty,
                                        order_type, status, strategy_id, risk_floor_hash, reason)
                    VALUES (:c, 'alpaca', 'X', 'crypto_spot', 'buy', 1, 'market', 'FILLED',
                            :s, '\\x00', :r)"""), {"c": coid, "s": S, "r": reason})
            insert_fills(conn, [
                fill(0, "BTC/USD", Side.BUY, "0.0256177", "78000", coid="btc-1", vid="o-btc"),
                fill(0, "BTC/USD", Side.SELL, "0.000064", "78000", "4.992", coid="btc-1",
                     vid="o-btc:fee"),
                fill(1, "ETH/USD", Side.BUY, "1", "3000", "7.50", coid="eth-1"),
                fill(30, "ETH/USD", Side.SELL, "0.4", "3300", "3.30", coid="eth-2"),
            ])
            for sym, qty, mark, h in (("BTC/USD", "0.0255537", "78500", 2),
                                      ("BTC/USD", "0.0255537", "77000", 40),
                                      ("ETH/USD", "1", "3050", 2),
                                      ("ETH/USD", "0.6", "3100", 40)):
                conn.execute(text("""
                    INSERT INTO position_marks (ts, strategy_id, venue, symbol, qty, mark)
                    VALUES (:ts, :s, 'alpaca', :sym, :q, :m)"""),
                    {"ts": T + timedelta(hours=h), "s": S, "sym": sym, "q": qty, "m": mark})
        yield eng
        eng.dispose()


def test_trades_pair_entries_with_exits(engine: Engine) -> None:
    with engine.connect() as conn:
        rows = trades.trades(conn)
    by = {(t.symbol, t.is_open): t for t in rows}

    btc = by[("BTC/USD", True)]
    assert btc.qty == D("0.0255537") and btc.fees == D("4.992")    # neto de la comisión
    assert btc.entry_reason == trades.NO_REASON and btc.mark == 77000
    assert btc.gross_pnl == D("-25.5537") and btc.net_pnl == D("-30.5457")
    # Lo pagado es la cantidad BRUTA al precio: la comisión en especie también se pagó.
    assert btc.entry_amount == D("0.0256177") * 78000

    closed = by[("ETH/USD", False)]
    assert (closed.qty, closed.entry_price, closed.exit_price) == (D("0.4"), 3000, 3300)
    assert closed.entry_reason == "weight 0.00% < target 20.00%"
    assert closed.exit_reason == "weight 26.00% > target 20.00%"
    assert (closed.entry_notional, closed.exit_notional) == (1200, 1320)
    # Pagado con su parte de la comisión de entrada; cobrado neto de la de salida.
    assert (closed.entry_amount, closed.exit_amount) == (D("1203.00"), D("1316.70"))
    assert closed.exit_amount - closed.entry_amount == closed.net_pnl
    assert closed.fees == D("6.30") and closed.net_pnl == D("113.70")
    assert closed.held_for == timedelta(hours=29)

    still = by[("ETH/USD", True)]
    assert still.qty == D("0.6") and still.fees == D("4.50") and still.net_pnl == D("55.50")

    t = trades.totals(rows)
    assert (t.n_open, t.n_closed) == (2, 1)
    assert t.net_closed == D("113.70") and t.net_open == D("-30.5457") + D("55.50")
    assert t.fees == D("4.992") + D("6.30") + D("4.50")


def test_price_moves_explain_the_unrealized(engine: Engine) -> None:
    with engine.connect() as conn:
        (btc, eth) = trades.price_moves(conn, T + timedelta(hours=24), T + timedelta(hours=48))
    # Del último precio ANTES del tramo (h2) al último dentro (h40).
    assert (btc.symbol, btc.mark_from, btc.mark_to) == ("BTC/USD", 78500, 77000)
    assert btc.pnl == D("0.0255537") * -1500
    assert eth.change_pct == D(3100) / D(3050) - 1


def test_operaciones_view_and_csv(engine: Engine) -> None:
    url = engine.url.render_as_string(hide_password=False)
    reader = readonly_engine(url)
    c = TestClient(create_app(reader, redis_url=None,
                              clock=lambda: T + timedelta(hours=48)))
    html = c.get("/operaciones").text
    for needle in ("weight 26.00% &gt; target 20.00%", "sigue abierta", "$113.70",
                   trades.NO_REASON):
        assert needle in html, needle
    rows = list(csv.DictReader(io.StringIO(c.get("/export/operaciones.csv").text)))
    assert list(rows[0]) == list(trades.TRADE_COLUMNS)
    eth = next(r for r in rows if r["status"] == "cerrada")
    assert (D(eth["net_pnl"]), eth["held_hours"]) == (D("113.70"), "29.0")
    day = c.get("/historial?day=2026-11-03").text          # h24..h48 = 3 de noviembre
    assert "Por qué se movió el no realizado" in day and "$77,000.00" in day
    fills = c.get("/historial?day=2026-11-02").text
    assert "comisión en especie" in fills
    kinds = [r["kind"] for r in csv.DictReader(io.StringIO(
        c.get("/export/fills.csv?day=2026-11-02").text))]
    assert sorted(kinds) == ["fee_in_kind", "fill", "fill"]
    reader.dispose()
