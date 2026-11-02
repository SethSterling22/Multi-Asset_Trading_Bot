"""Dashboard: FastAPI + Jinja + HTMX, de SOLO LECTURA sobre el ledger (informe §8).

Vistas 1 (Live), 2 (P&L por estrategia), 5 (Salud) y 6 (Riesgo) de la §8.2, más
Mercado (el archivo de Coinalyze). Las vistas 3 (aprendizaje) y 4 (propuestas)
dependen de la Fase 3 y no existen todavía.

No hay ningún POST: el kill switch se activa con /run/trading/HALT, Redis o una fila
en risk_events. Cada sección se refresca sola con HTMX cada 30 s.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, text

from api import charts, queries

WEB = Path(__file__).resolve().parents[1] / "web"
REFRESH_SECONDS = 30
VIEWS = [("live", "/", "LIVE"), ("pnl", "/pnl", "P&L POR ESTRATEGIA"),
         ("mercado", "/mercado", "MERCADO"), ("salud", "/salud", "SALUD"),
         ("riesgo", "/riesgo", "RIESGO")]


def money(v: Any) -> str:
    if v is None:
        return "—"
    x = float(v)
    return f"−${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def compact(v: Any) -> str:
    if v is None:
        return "—"
    x = float(v)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"${x / div:,.2f}{suffix}"
    return f"${x:,.0f}"


def pct(v: Any, digits: int = 2) -> str:
    return "—" if v is None else f"{float(v) * 100:+.{digits}f}%"


def signed_class(v: Any) -> str:
    if v is None or float(v) == 0:
        return "neu"
    return "up" if float(v) > 0 else "down"


def quantity(v: Any) -> str:
    """Cantidad sin ceros de relleno: 0.01500000 -> 0.015."""
    return f"{Decimal(v):,.8f}".rstrip("0").rstrip(".")


def ago(d: timedelta | None) -> str:
    if d is None:
        return "—"
    s = int(d.total_seconds())
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    return f"{s / 3600:.1f} h"


def create_app(engine: Engine, *, redis_url: str | None,
               clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FastAPI:
    app = FastAPI(title="Multi-Asset Trading Bot", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    templates = Jinja2Templates(directory=WEB / "templates")
    # Versión del CSS por contenido: tras desplegar, el navegador no usa la copia vieja.
    templates.env.globals["css_version"] = hashlib.sha256(
        (WEB / "static" / "style.css").read_bytes()).hexdigest()[:12]
    templates.env.filters.update(money=money, compact=compact, pct=pct, cls=signed_class,
                                 ago=ago, dec=quantity)

    def header() -> dict[str, Any]:
        with engine.connect() as conn:
            ks = queries.kill_switch_state(engine, redis_url)
            eq = queries.equity_kpis(queries.equity_series(conn, clock() - timedelta(days=30)))
            pos = queries.positions(conn)
            pnl_total = queries.totals(queries.pnl_by_strategy(conn, clock().date()))
            last_run = queries.last_archive_run(conn)
        return {
            "kill_switch": ks, "live": queries.live_trading(), "equity": eq,
            "open_positions": len(pos), "max_positions": queries.MAX_OPEN_POSITIONS,
            "today_net": pnl_total.today_net, "ai_cost": pnl_total.ai_cost,
            "archive_lag": None if last_run is None else clock() - last_run,
            "now": clock(),
        }

    def view(name: str) -> dict[str, Any]:
        now = clock()
        with engine.connect() as conn:
            if name == "live":
                eq = queries.equity_series(conn, now - timedelta(days=7))
                return {"equity_svg": charts.line_chart(eq, y_format=lambda v: f"${v:,.0f}"),
                        "equity_legend": charts.legend(list(eq)),
                        "fills": queries.fills_on(conn, now.date())}
            if name == "pnl":
                rows = queries.pnl_by_strategy(conn, now.date())
                total = queries.totals(rows)
                return {"rows": rows, "total": total,
                        "waterfall_svg": charts.waterfall(queries.waterfall_steps(total),
                                                          money=money)}
            if name == "mercado":
                out: dict[str, Any] = {"venues": queries.market(conn)}
                for asset in ("BTC", "ETH"):
                    oi = queries.metric_series(conn, asset=asset, metric="open_interest",
                                               interval="1min", since=now - timedelta(hours=24),
                                               every_minutes=10, index=True)
                    fr = queries.metric_series(conn, asset=asset, metric="funding_rate",
                                               interval="1hour", since=now - timedelta(days=7),
                                               every_minutes=60)
                    colors = queries.VENUE_COLORS
                    out[f"oi_{asset}"] = charts.line_chart(oi, y_format=lambda v: f"{v:.1f}",
                                                           colors=colors)
                    out[f"oi_{asset}_legend"] = charts.legend(list(oi), colors)
                    out[f"fr_{asset}"] = charts.line_chart(fr, y_format=lambda v: f"{v:.3f}%",
                                                           colors=colors)
                    out[f"fr_{asset}_legend"] = charts.legend(list(fr), colors)
                return out
            if name == "salud":
                return {"archive": queries.archive_health(conn),
                        "kill_switch": queries.kill_switch_state(engine, redis_url)}
            if name == "riesgo":
                return {"positions": queries.positions(conn), "limits": queries.FLOOR_LIMITS}
        raise KeyError(name)

    def page(request: Request, name: str) -> HTMLResponse:
        return templates.TemplateResponse(request, "page.html", {
            "views": VIEWS, "current": name, "refresh": REFRESH_SECONDS,
            "h": header(), "v": view(name), "view_template": f"{name}.html",
        })

    for name, path, _ in VIEWS:
        def handler(request: Request, _name: str = name) -> HTMLResponse:
            return page(request, _name)
        app.add_api_route(path, handler, methods=["GET"], response_class=HTMLResponse,
                          name=name)

    @app.get("/partials/header", response_class=HTMLResponse)
    def header_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "header.html", {"h": header()})

    @app.get("/partials/{name}", response_class=HTMLResponse)
    def view_partial(request: Request, name: str) -> HTMLResponse:
        if name not in {v[0] for v in VIEWS}:
            return HTMLResponse("vista desconocida", status_code=404)
        return templates.TemplateResponse(request, f"{name}.html", {"v": view(name)})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return JSONResponse({"ok": True})

    return app


def main() -> None:
    import uvicorn

    engine = queries.readonly_engine(os.environ["DATABASE_URL"])
    app = create_app(engine, redis_url=os.environ.get("REDIS_URL"))
    uvicorn.run(app, host=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),  # noqa: S104 — en contenedor
                port=int(os.environ.get("DASHBOARD_PORT", "8000")), proxy_headers=False)
