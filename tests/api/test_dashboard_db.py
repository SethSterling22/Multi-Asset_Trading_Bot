"""El dashboard contra Postgres: todas las vistas responden, es de solo lectura, y el
P&L por estrategia cuadra al centavo con el ledger (gate de la Sesión 7)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from core.ledger.fills import insert_fills
from tests.conftest import alembic, throwaway_database
from tests.ledger.fixtures import FUNDING, OPTION, TRACED

pytestmark = pytest.mark.db
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.app import create_app  # noqa: E402
from api.queries import readonly_engine  # noqa: E402

NOW = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)          # el día de los fixtures


@pytest.fixture(scope="module")
def client() -> Iterator[tuple[TestClient, str]]:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        writer = create_engine(url)
        with writer.begin() as conn:
            insert_fills(conn, [*TRACED, *OPTION, FUNDING])
            for h in range(3):
                conn.execute(text(
                    "INSERT INTO equity_curve (ts, strategy_id, equity_usd, allocated_usd,"
                    " gross_notional) VALUES (:ts, 'wheel', :e, :e, 0)"),
                    {"ts": NOW - timedelta(hours=3 - h), "e": 100_000 + 100 * h})
        writer.dispose()
        reader = readonly_engine(url.render_as_string(hide_password=False))
        app = create_app(reader, redis_url=None, clock=lambda: NOW)
        yield TestClient(app), url.render_as_string(hide_password=False)
        reader.dispose()


@pytest.mark.parametrize("path", ["/", "/pnl", "/mercado", "/salud", "/riesgo",
                                  "/partials/header", "/partials/pnl", "/healthz"])
def test_every_view_responds(client: tuple[TestClient, str], path: str) -> None:
    c, _ = client
    r = c.get(path)
    assert r.status_code == 200, r.text[:500]


def test_unknown_partial_is_404(client: tuple[TestClient, str]) -> None:
    c, _ = client
    assert c.get("/partials/nope").status_code == 404


def test_dashboard_connection_cannot_write(client: tuple[TestClient, str]) -> None:
    _, url = client
    reader = readonly_engine(url)
    with reader.connect() as conn, pytest.raises(DBAPIError, match="read-only"):
        conn.execute(text("INSERT INTO risk_events (kind, scope, reason, actor)"
                          " VALUES ('x', 'global', 'x', 'test')"))
    reader.dispose()


def _cell_values(html: str, sleeve: str) -> list[str]:
    row = re.search(rf"</i>{sleeve}</span></td>(.*?)</tr>", html, re.S)
    assert row, f"fila de {sleeve} no encontrada"
    return re.findall(r"<td[^>]*>(?:<b>)?([^<]*)(?:</b>)?</td>", row.group(1))


def test_pnl_matches_the_ledger_to_the_cent(client: tuple[TestClient, str]) -> None:
    """Gate de la Sesión 7: el P&L por estrategia cuadra al centavo contra el ledger."""
    c, url = client
    html = c.get("/pnl").text
    engine = create_engine(url)
    with engine.connect() as conn:
        sums = {r.strategy_id: r for r in conn.execute(text(
            "SELECT strategy_id, sum(commission) AS fees, sum(funding_payment) AS funding"
            " FROM fills GROUP BY strategy_id"))}
    engine.dispose()
    # trend: realizado 10 (fixture trazado a mano), funding −3.25, sin comisiones.
    trend = _cell_values(html, "Trend")
    gross, fees, funding, slip, net, ai, net_ai = trend[:7]
    assert fees == f"${sums['trend'].fees:,.2f}" == "$0.00"
    assert funding == "−$3.25" and D(sums["trend"].funding) == D("-3.25")
    assert gross == "$10.00" and net == "$6.75" and net_ai == "$6.75"
    assert slip == "$0.00" and ai == "$0.00"          # sin decision_price ni capa LLM
    # wheel: put vendida a 2.50 y recomprada a 0.50 ×100 = +200; comisiones 1.30.
    wheel = _cell_values(html, "Wheel")
    assert wheel[0] == "$200.00" and wheel[1] == f"${sums['wheel'].fees:,.2f}" == "$1.30"
    assert wheel[4] == "$198.70"
    assert "Sharpe</th>" not in html


def test_header_shows_paper_and_kill_switch_state(client: tuple[TestClient, str]) -> None:
    c, _ = client
    html = c.get("/partials/header").text
    assert "PAPER" in html
    # Sin REDIS_URL, esa fuente da error y cuenta como activa: el dashboard lo enseña.
    assert "KILL SWITCH" in html and "redis" in html
