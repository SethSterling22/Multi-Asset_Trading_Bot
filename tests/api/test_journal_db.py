"""El Diario contra Postgres: el formulario manual (y lo que Postgres le deja escribir),
las estadísticas con cifras a mano y una corrida de backtest guardada.

Operaciones manuales (cuenta "webull"):
    AAPL: compra 10 a 100 (comisión 1) y venta 10 a 110 (comisión 1) -> neto 100 − 2 = +98
    MSFT: compra 5 a 200 y venta 5 a 190, sin comisión            -> neto −50
  profit factor = 98 / 50 = 1.96; aciertos 1 de 2; media 98 / −50.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from backtest.engine import Bar, run_crypto_rebalance
from backtest.registry import PostgresSearchLog
from backtest.runs import save_runs
from core.config.models import TradingConfig
from core.ledger.fills import AssetType, Fill, Side, insert_fill
from tests.conftest import alembic, throwaway_database

pytestmark = pytest.mark.db
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import journal  # noqa: E402
from api.app import create_app  # noqa: E402
from api.queries import readonly_engine  # noqa: E402

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
ORIGIN = {"origin": "http://testserver"}


def form(**kw: str) -> dict[str, str]:
    base = {"fecha": "2026-11-03", "hora": "09:00", "zona": "UTC", "cuenta": "webull",
            "simbolo": "AAPL", "tipo": "stock", "lado": "buy", "cantidad": "10",
            "precio": "100", "comision": "1", "motivo": "ruptura"}
    return {**base, **kw}


@pytest.fixture(scope="module")
def env() -> Iterator[tuple[TestClient, TestClient, str]]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        raw = url.render_as_string(hide_password=False)
        reader = readonly_engine(raw)
        # El rol del formulario, igual que en Sadida pero sin contraseña: SET ROLE al conectar.
        writer = create_engine(url, connect_args={"options": "-c role=manual_journal"})
        # Una corrida de backtest de verdad, guardada con su N.
        admin = create_engine(url)
        bars = {s: [Bar(date(2026, 1, 1) + timedelta(days=i), 100 + i + (i % 3) * k,
                        100 + i + (i % 2) * k) for i in range(30)]
                for s, k in (("BTC", 7), ("ETH", -5))}
        registry = PostgresSearchLog(admin)
        result = run_crypto_rebalance(TradingConfig.load(), bars, initial_usdc=10_000,
                                      fee_bps=60, slippage_bps=5, registry=registry)
        save_runs(admin, [result], registry=registry, candidate_hash=result.params_hash)
        admin.dispose()
        app = create_app(reader, redis_url=None, clock=lambda: NOW, manual_engine=writer)
        off = create_app(reader, redis_url=None, clock=lambda: NOW)
        yield TestClient(app), TestClient(off), raw
        writer.dispose()
        reader.dispose()


def count(url: str, sql: str) -> int:
    e = create_engine(url)
    with e.connect() as conn:
        n = int(conn.execute(text(sql)).scalar_one())
    e.dispose()
    return n


# -- el rol de escritura: lo que Postgres le deja hacer --------------------------------
def test_manual_role_can_only_write_the_manual_sleeve(env: tuple[TestClient, TestClient, str]
                                                      ) -> None:
    _, _, url = env
    writer = create_engine(url, connect_args={"options": "-c role=manual_journal"})
    other = Fill(ts=NOW, venue="alpaca", symbol="BTC/USD", asset_type=AssetType.CRYPTO_SPOT,
                 side=Side.BUY, qty=D(1), price=D(1), strategy_id="wheel", venue_fill_id="x")
    with writer.begin() as conn, pytest.raises(DBAPIError, match="row-level security"):
        insert_fill(conn, other)
    with writer.begin() as conn, pytest.raises(DBAPIError, match="permission denied"):
        conn.execute(text("DELETE FROM orders"))
    with writer.begin() as conn, pytest.raises(DBAPIError, match="permission denied"):
        conn.execute(text("INSERT INTO risk_events (kind, scope, reason, actor)"
                          " VALUES ('kill_switch_off', 'global', 'x', 'x')"))
    writer.dispose()


# -- el formulario ---------------------------------------------------------------------
def test_review_shows_the_entry_and_writes_nothing(env: tuple[TestClient, TestClient, str]
                                                   ) -> None:
    c, _, url = env
    before = count(url, "SELECT count(*) FROM fills WHERE strategy_id = 'manual'")
    r = c.post("/diario/revisar", data=form(), headers=ORIGIN)
    assert r.status_code == 200 and "Revisa antes de registrar" in r.text
    assert "$1,000.00" in r.text                                       # 10 · 100
    assert count(url, "SELECT count(*) FROM fills WHERE strategy_id = 'manual'") == before


def test_invalid_entry_is_rejected_with_its_errors(env: tuple[TestClient, TestClient, str]
                                                   ) -> None:
    c, _, _ = env
    r = c.post("/diario/revisar", data=form(cantidad="-3", motivo=""), headers=ORIGIN)
    assert r.status_code == 422 and "cantidad &gt; 0" in r.text and "motivo" in r.text


def test_cross_site_post_is_refused(env: tuple[TestClient, TestClient, str]) -> None:
    c, _, url = env
    before = count(url, "SELECT count(*) FROM fills")
    assert c.post("/diario/registrar", data=form(),
                  headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/diario/registrar", data=form()).status_code == 403          # sin Origin
    assert count(url, "SELECT count(*) FROM fills") == before


def test_without_the_writer_nothing_can_be_recorded(env: tuple[TestClient, TestClient, str]
                                                    ) -> None:
    _, off, _ = env
    assert "Registro manual desactivado" in off.get("/diario/nueva").text
    assert off.post("/diario/registrar", data=form(), headers=ORIGIN).status_code == 503


def test_recorded_trades_feed_the_journal(env: tuple[TestClient, TestClient, str]) -> None:
    c, _, url = env
    for f in (form(), form(hora="15:00", lado="sell", precio="110"),
              form(simbolo="MSFT", cantidad="5", precio="200", comision="0", hora="10:00"),
              form(simbolo="MSFT", cantidad="5", precio="190", comision="0", hora="16:00",
                   lado="sell", motivo="stop")):
        r = c.post("/diario/registrar", data=f, headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/diario?sleeve=manual")
    assert count(url, "SELECT count(*) FROM orders WHERE strategy_id = 'manual'"
                      " AND reason IS NOT NULL") == 4

    reader = readonly_engine(url)
    with reader.connect() as conn:
        src = journal.real_source(conn, sleeve="manual", until=NOW.date(), mode="realizado")
    reader.dispose()
    s = journal.stats(src)
    assert (s.n_closed, s.n_wins, s.n_losses) == (2, 1, 1)
    assert s.gross_win == 98 and s.gross_loss == -50 and s.profit_factor == pytest.approx(1.96)
    assert s.win_rate == 0.5 and s.win_loss_ratio == pytest.approx(1.96)
    day = src.daily[date(2026, 11, 3)]
    assert (day.pnl, day.n_closed, day.n_wins) == (48.0, 2, 1)
    # Sin snapshots de equity, el P&L neto es lo realizado, y sin capital no hay % ni balance.
    assert s.net_pnl == 48.0 and s.max_drawdown_pct is None and s.balance is None

    html = c.get("/diario?sleeve=manual&modo=realizado&mes=2026-11").text
    for needle in ("noviembre 2026", "$48.00", "1.96", "50.0%", "ruptura", "stop"):
        assert needle in html, needle
    # Pasan por Operaciones como cualquier otra.
    assert "webull" not in c.get("/salud").text      # no es un venue reconciliado
    assert "stop" in c.get("/operaciones").text


# -- una corrida de backtest -----------------------------------------------------------
def test_backtest_run_shows_with_its_n_and_dsr(env: tuple[TestClient, TestClient, str]) -> None:
    c, _, url = env
    run_id = count(url, "SELECT id FROM backtest_runs")
    n_fills = count(url, "SELECT count(*) FROM backtest_fills")
    assert n_fills > 0
    html = c.get(f"/diario?fuente=bt:{run_id}&mes=2026-01").text
    assert f"Corrida de backtest #{run_id}" in html and "N = 1" in html and "DSR" in html
    assert "enero 2026" in html and re.search(r"weight [\d.]+% (&lt;|&gt;) target", html)
    csv = c.get(f"/export/operaciones.csv?fuente=bt:{run_id}").text
    assert csv.count("\n") > 1 and "backtest" in csv
    # Una fuente que no existe vuelve a lo real, no rompe.
    assert c.get("/diario?fuente=bt:999999").status_code == 200
