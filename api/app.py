"""Dashboard: FastAPI + Jinja + HTMX, de SOLO LECTURA sobre el ledger (informe §8).

Vistas 1 (Live), 2 (P&L por estrategia), 5 (Salud) y 6 (Riesgo) de la §8.2, más
Mercado (el archivo de Coinalyze), Historial (qué pasó cada día y por qué cambió la
equity) y Operaciones (cada entrada con su salida, a qué precio y por qué), las dos
exportables en CSV. Las vistas 3 (aprendizaje) y 4 (propuestas) dependen de la
Fase 3 y no existen todavía.

No hay ningún POST: el kill switch se activa con /run/trading/HALT, Redis o una fila
en risk_events. Cada sección se refresca sola con HTMX cada 30 s.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, create_engine, text

from api import charts, history, journal, manual, queries, trades
from core.ledger.fills import AssetType, Side

WEB = Path(__file__).resolve().parents[1] / "web"
REFRESH_SECONDS = 30
VIEWS = [("live", "/", "LIVE"), ("pnl", "/pnl", "P&L POR ESTRATEGIA"),
         ("diario", "/diario", "DIARIO"),
         ("historial", "/historial", "HISTORIAL"), ("operaciones", "/operaciones", "OPERACIONES"),
         ("mercado", "/mercado", "MERCADO"), ("salud", "/salud", "SALUD"),
         ("riesgo", "/riesgo", "RIESGO")]


def parse_day(raw: str | None, default: date) -> date:
    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


def fill_marker(f: Any) -> tuple[datetime, str]:
    return f.ts, (f"{f.ts:%H:%M} UTC · {f.strategy_id} {f.side.value} {quantity(f.qty)} "
                  f"{f.symbol} @ {money(f.price)}")


def sleeve_charts(series: dict[str, list[tuple[datetime, float]]], fills: list[Any],
                  ) -> list[dict[str, Any]]:
    """Un gráfico por sleeve: en un eje común, 100 000 $ aplastan a 10 000 $."""
    out = []
    for sid, pts in sorted(series.items()):
        marks = [fill_marker(f) for f in fills if f.strategy_id == sid]
        out.append({"sleeve": sid, "svg": charts.line_chart(
            {sid: pts}, height=170, y_format=lambda v: f"${v:,.0f}", markers=marks)})
    return out


def money(v: Any) -> str:
    if v is None:
        return "—"
    x = round(float(v), 2) + 0.0    # + 0.0: el ruido y el −0.0 no llevan signo
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
    if v is None or round(float(v), 2) == 0:
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


MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def parse_month(raw: str | None, default: date) -> tuple[int, int]:
    try:
        y, m = (int(x) for x in (raw or "").split("-"))
        date(y, m, 1)
        return y, m
    except ValueError:
        return default.year, default.month


def create_app(engine: Engine, *, redis_url: str | None,
               clock: Callable[[], datetime] = lambda: datetime.now(UTC),
               manual_engine: Engine | None = None) -> FastAPI:
    """`manual_engine` es la conexión con el rol `manual_journal` (D-017): sin ella el
    formulario del diario manual está desactivado y el dashboard no escribe nada."""
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
            since = clock() - timedelta(days=30)
            eq = queries.equity_kpis(queries.equity_series(conn, since),
                                     queries.pnl_curves(conn, since))
            pos = queries.positions(conn)
            pnl_total = queries.totals(queries.pnl_by_strategy(conn, clock().date()))
            last_run = queries.last_archive_run(conn)
        return {
            "kill_switch": ks, "live": queries.live_trading(), "equity": eq,
            # El límite del floor es del bot: las manuales (otra cuenta) no cuentan.
            "open_positions": sum(1 for k in pos if k.strategy_id != manual.STRATEGY_ID),
            "max_positions": queries.MAX_OPEN_POSITIONS,
            "today_net": pnl_total.today_net, "ai_cost": pnl_total.ai_cost,
            "archive_lag": None if last_run is None else clock() - last_run,
            "now": clock(),
        }

    def view(name: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        params = params or {}
        now = clock()
        with engine.connect() as conn:
            if name == "live":
                since = now - timedelta(days=7)
                eq = queries.equity_series(conn, since)
                return {"charts": sleeve_charts(eq, queries.fills_since(conn, since)),
                        "fills": queries.fills_on(conn, now.date())}
            if name == "historial":
                day = parse_day(params.get("day"), now.date())
                rows = history.daily_history(conn, until=now.date())
                detail = history.day_detail(conn, day)
                per_sleeve: dict[str, list[tuple[str, float]]] = {}
                for r in reversed(rows):
                    if r.change is not None:
                        per_sleeve.setdefault(r.strategy_id, []).append(
                            (r.day.strftime("%d %b"), r.change))
                first, last = history.activity_days(conn)
                start, end = history.day_bounds(day)
                return {
                    "moves": trades.price_moves(conn, start, end),
                    "day": day, "today": now.date(), "first": first, "last": last,
                    "prev": day - timedelta(days=1), "next": day + timedelta(days=1),
                    "rows": rows, "detail": detail,
                    "day_rows": [r for r in rows if r.day == day],
                    "bars": [{"sleeve": sid, "svg": charts.bars(steps, money=money)}
                             for sid, steps in sorted(per_sleeve.items())],
                    "charts": sleeve_charts(detail.equity, [
                        f for f in queries.fills_on(conn, day)]),
                }
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
            if name == "diario":
                mode = params.get("modo") if params.get("modo") in journal.MODES else "equity"
                runs = journal.backtest_runs(conn)
                source = None
                fuente = params.get("fuente", "real")
                if fuente.startswith("bt:") and fuente[3:].isdigit():
                    source = journal.backtest_source(conn, int(fuente[3:]), mode=mode)
                sleeve = params.get("sleeve") or None
                if source is None:
                    fuente = "real"
                    source = journal.real_source(conn, sleeve=sleeve, until=now.date(),
                                                 mode=mode)
                last_day = max(source.daily, default=now.date()) if fuente != "real" \
                    else now.date()
                year, month = parse_month(params.get("mes"), last_day)
                first = date(year, month, 1)
                prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
                next_m = (first + timedelta(days=32)).strftime("%Y-%m")
                recent = sorted(source.daily.values(), key=lambda d: d.day)[-60:]
                total, active = journal.month_total(source.daily, year, month)
                return {
                    "source": source, "fuente": fuente, "sleeve": sleeve, "mode": mode,
                    "runs": [(f"bt:{r.id}", journal.run_label(r)) for r in runs],
                    "sleeves": journal.sleeves_with_activity(conn), "stats": journal.stats(source),
                    "year": year, "month": month, "month_name": f"{MONTHS[month - 1]} {year}",
                    "weeks": journal.month_weeks(year, month, source.daily),
                    "month_pnl": total, "month_days": active, "prev": prev_m, "next": next_m,
                    "query_base": "&".join(f"{k}={v}" for k, v in (
                        ("fuente", fuente), ("sleeve", sleeve or ""), ("modo", mode))),
                    "balance_svg": charts.line_chart(
                        {"Balance": source.equity, "Asignado (depósitos)": source.allocated},
                        y_format=lambda v: f"${v:,.0f}"),
                    "balance_legend": charts.legend(["Balance", "Asignado (depósitos)"]),
                    "daily_svg": charts.bars([(d.day.strftime("%d %b"), d.pnl) for d in recent],
                                             money=money),
                    "hour_svg": charts.bars(journal.pnl_by_hour(source.trades), money=money),
                    "recent_trades": source.trades[:50],
                    "manual_enabled": manual_engine is not None,
                }
            if name == "operaciones":
                rows = trades.trades(conn)
                return {"trades": rows, "totals": trades.totals(rows)}
            if name == "salud":
                return {"archive": queries.archive_health(conn),
                        "kill_switch": queries.kill_switch_state(engine, redis_url),
                        "by_scope": queries.kill_switch_by_scope(engine, redis_url),
                        "last_events": queries.last_kill_events(conn)}
            if name == "riesgo":
                return {"positions": queries.positions(conn), "limits": queries.FLOOR_LIMITS}
        raise KeyError(name)

    def page(request: Request, name: str) -> HTMLResponse:
        query = f"?{request.url.query}" if request.url.query else ""
        return templates.TemplateResponse(request, "page.html", {
            "views": VIEWS, "current": name, "refresh": REFRESH_SECONDS, "query": query,
            "h": header(), "v": view(name, dict(request.query_params)),
            "view_template": f"{name}.html",
        })

    # -- Diario manual: el único camino de escritura (rol manual_journal, D-017) ---------
    def manual_page(request: Request, *, form: dict[str, str], errors: list[str],
                    preview: manual.ManualEntry | None = None,
                    status_code: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(request, "page.html", {
            "views": VIEWS, "current": "diario", "refresh": REFRESH_SECONDS, "query": "",
            "no_refresh": True, "h": header(), "view_template": "manual.html",
            "v": {"form": form, "errors": errors, "preview": preview, "zones": manual.ZONES,
                  "types": [t.value for t in AssetType], "sides": [s.value for s in Side],
                  "enabled": manual_engine is not None},
        }, status_code=status_code)

    @app.get("/diario/nueva", response_class=HTMLResponse)
    def manual_form(request: Request) -> HTMLResponse:
        local = clock().astimezone(ZoneInfo(manual.ZONES[0]))
        # "Corregir" vuelve aquí con los campos en la URL: se rellenan con ellos.
        return manual_page(request, errors=[], form={
            "fecha": f"{local:%Y-%m-%d}", "hora": f"{local:%H:%M}", "zona": manual.ZONES[0],
            "tipo": "stock", "lado": "buy", "comision": "0", **dict(request.query_params)})

    @app.post("/diario/revisar", response_class=HTMLResponse)
    async def manual_review(request: Request) -> HTMLResponse:
        form = {k: str(v) for k, v in (await request.form()).items()}
        if not manual.same_origin(dict(request.headers)):
            return HTMLResponse("origen no permitido", status_code=403)
        entry = manual.parse(form, now=clock())
        if isinstance(entry, list):
            return manual_page(request, form=form, errors=entry, status_code=422)
        return manual_page(request, form=entry.as_form(form.get("zona", manual.ZONES[0])),
                           errors=[], preview=entry)

    @app.post("/diario/registrar", response_model=None)
    async def manual_record(request: Request) -> HTMLResponse | RedirectResponse:
        form = {k: str(v) for k, v in (await request.form()).items()}
        if not manual.same_origin(dict(request.headers)):
            return HTMLResponse("origen no permitido", status_code=403)
        if manual_engine is None:
            return HTMLResponse("registro manual desactivado (sin MANUAL_DATABASE_URL)",
                                status_code=503)
        entry = manual.parse(form, now=clock())
        if isinstance(entry, list):
            return manual_page(request, form=form, errors=entry, status_code=422)
        manual.record(manual_engine, entry)
        # Post/Redirect/Get: recargar la página no registra la operación otra vez.
        return RedirectResponse(f"/diario?sleeve=manual&mes={entry.ts:%Y-%m}",
                                status_code=303)

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
        return templates.TemplateResponse(request, f"{name}.html",
                                          {"v": view(name, dict(request.query_params))})

    # Exportaciones (solo lectura): el historial diario y los fills, para comparar con
    # un backtest o abrirlos en una hoja de cálculo.
    @app.get("/export/historial.csv")
    def export_history(days: int = history.DEFAULT_DAYS) -> PlainTextResponse:
        with engine.connect() as conn:
            rows = history.daily_history(conn, until=clock().date(), days=max(1, min(days, 3650)))
        return PlainTextResponse(history.history_csv(rows), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="historial_diario.csv"'})

    @app.get("/export/operaciones.csv")
    def export_trades(fuente: str = "real") -> PlainTextResponse:
        with engine.connect() as conn:
            src = (journal.backtest_source(conn, int(fuente[3:]), mode="equity")
                   if fuente.startswith("bt:") and fuente[3:].isdigit() else None)
            rows = src.trades if src is not None else trades.trades(conn)
        return PlainTextResponse(trades.trades_csv(rows), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="operaciones.csv"'})

    @app.get("/export/fills.csv")
    def export_fills(day: str | None = None) -> PlainTextResponse:
        with engine.connect() as conn:
            if day:
                fills = history.day_detail(conn, parse_day(day, clock().date())).fills
                name = f"fills_{day}.csv"
            else:
                fills = history.all_fills(conn)
                name = "fills.csv"
        return PlainTextResponse(history.fills_csv(fills), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return JSONResponse({"ok": True})

    return app


def main() -> None:
    import uvicorn

    engine = queries.readonly_engine(os.environ["DATABASE_URL"])
    # Opcional: sin ella el formulario del diario manual queda desactivado (D-017).
    manual_url = os.environ.get("MANUAL_DATABASE_URL")
    app = create_app(engine, redis_url=os.environ.get("REDIS_URL"),
                     manual_engine=create_engine(manual_url, pool_pre_ping=True)
                     if manual_url else None)
    uvicorn.run(app, host=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),  # noqa: S104 — en contenedor
                port=int(os.environ.get("DASHBOARD_PORT", "8000")), proxy_headers=False)
