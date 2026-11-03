"""Lo que muestra el dashboard, calculado con el código ya probado del sistema.

Nada se recalcula aquí por otra vía: el P&L sale de core/ledger/attribution (la
misma función que cuadra al centavo en los tests del ledger), la cobertura del
archivo de data/ingest/coinalyze y el kill switch de risk/breakers. Si el dashboard
y el sistema discrepan, el fallo está en el sistema y se ve en los dos.

Toda cifra de P&L es NETA (CLAUDE.md §3.4). No se muestra Sharpe: sin deflactar por
N no contiene información (CLAUDE.md §1.11).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from core.config.models import CONFIG_PATH
from core.ledger.attribution import pnl_by_strategy_day, utc_day
from core.ledger.fills import Fill, fetch_fills
from core.ledger.positions import ViewPosition, load_positions_view
from core.metrics import max_drawdown
from core.risk_floor import (
    DAILY_LOSS_STOP_PCT,
    MAX_GROSS_EXPOSURE_PCT,
    MAX_LEVERAGE_ALTS,
    MAX_LEVERAGE_MAJORS,
    MAX_NET_EXPOSURE_PCT,
    MAX_NOTIONAL_PCT_EQUITY,
    MAX_OPEN_POSITIONS,
    MAX_RISK_PER_TRADE_PCT,
    WEEKLY_LOSS_STOP_PCT,
)
from data.ingest.coinalyze import (
    DENSE_METRICS,
    SERIES,
    Series,
    coverage_gaps,
    first_covered,
    last_covered,
    sequence_holes,
)
from risk.breakers import KillSwitch, KillSwitchState, file_source, redis_source, risk_events_source
from strategies.base import ParamsLoader, config_kill_source

ZERO = Decimal(0)


def readonly_engine(url: str) -> Engine:
    """Toda sesión del dashboard es de solo lectura, impuesto por el servidor."""
    return create_engine(
        url, pool_pre_ping=True,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


# ---------------------------------------------------------------------------
# Estado del sistema
# ---------------------------------------------------------------------------
def _kill_switch(engine: Engine, redis_url: str | None) -> KillSwitch:
    import redis

    def no_redis(_: str) -> bool:
        raise RuntimeError("REDIS_URL no configurada")

    params = ParamsLoader(CONFIG_PATH)
    return KillSwitch(
        risk_events=risk_events_source(engine),
        redis=redis_source(redis.Redis.from_url(redis_url)) if redis_url else no_redis,
        halt_file=file_source(),
        extra={"config.json": config_kill_source(params)},
    )


def kill_switch_by_scope(engine: Engine, redis_url: str | None) -> dict[str, KillSwitchState]:
    """El estado para 'global' y para CADA sleeve: la reconciliación para un sleeve
    concreto (scope = strategy_id), y mirar solo 'global' lo escondería."""
    ks = _kill_switch(engine, redis_url)
    with engine.connect() as conn:
        scopes = ["global", *(sid for sid, _ in strategies(conn))]
    return {scope: ks.state(scope) for scope in scopes}


def kill_switch_state(engine: Engine, redis_url: str | None) -> KillSwitchState:
    """Activo si lo está en cualquier scope. Las tres fuentes más config.json; sin Redis
    configurado, esa fuente da error y cuenta como activa: el dashboard enseña lo mismo
    que vería la estrategia."""
    by_scope = kill_switch_by_scope(engine, redis_url)
    reasons = list(by_scope["global"].reasons)
    for scope, st in by_scope.items():
        reasons += [f"{scope}: {r}" for r in st.reasons
                    if scope != "global" and r not in by_scope["global"].reasons]
    return KillSwitchState(active=bool(reasons), reasons=tuple(reasons))


def last_kill_events(conn: Connection) -> dict[str, Any]:
    """El último evento on/off de cada scope, con su motivo y quién lo puso."""
    return {r.scope: r for r in conn.execute(text("""
        SELECT DISTINCT ON (scope) scope, ts, kind, reason, actor FROM risk_events
        WHERE kind IN ('kill_switch_on', 'kill_switch_off') ORDER BY scope, ts DESC, id DESC
    """))}


def live_trading() -> bool | None:
    cfg = ParamsLoader(CONFIG_PATH).current()
    return getattr(getattr(cfg, "system_status", None), "live_trading_mode", None)


def strategies(conn: Connection) -> list[tuple[str, str]]:
    return [(r.strategy_id, r.display_name) for r in conn.execute(
        text("SELECT strategy_id, display_name FROM strategy ORDER BY strategy_id"))]


# ---------------------------------------------------------------------------
# P&L por estrategia (vista 2)
# ---------------------------------------------------------------------------
@dataclass
class StrategyPnl:
    strategy_id: str
    name: str
    gross: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    slippage: Decimal = ZERO
    ai_cost: Decimal = ZERO
    n_fills: int = 0
    today_net: Decimal = ZERO
    n_registered: int = 0
    unrealized: Decimal = ZERO          # del último snapshot de equity del sleeve
    equity: Decimal | None = None       # ídem

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees + self.funding - self.slippage

    @property
    def net_after_ai(self) -> Decimal:
        return self.net - self.ai_cost

    @property
    def total(self) -> Decimal:
        """Neto realizado tras IA más lo no realizado: lo que daría cerrar hoy."""
        return self.net_after_ai + self.unrealized


def pnl_by_strategy(conn: Connection, today: date | None = None) -> list[StrategyPnl]:
    today = today or datetime.now(UTC).date()
    rows = {sid: StrategyPnl(sid, name) for sid, name in strategies(conn)}
    for day in pnl_by_strategy_day(fetch_fills(conn)):
        r = rows.setdefault(day.strategy_id, StrategyPnl(day.strategy_id, day.strategy_id))
        r.gross += day.gross_pnl
        r.fees += day.fees
        r.funding += day.funding
        r.slippage += day.slippage
        r.ai_cost += day.ai_cost
        r.n_fills += day.n_fills
        if day.day == today:
            r.today_net += day.net_pnl_after_ai
    for sid, n in conn.execute(text(
        "SELECT strategy_id, count(*) FROM search_log WHERE kind = 'backtest_config'"
        " GROUP BY strategy_id"
    )):
        if sid in rows:
            rows[sid].n_registered = n
    for r in conn.execute(text(
        "SELECT DISTINCT ON (strategy_id) strategy_id, equity_usd, unrealized_pnl"
        " FROM equity_curve ORDER BY strategy_id, ts DESC"
    )):
        if r.strategy_id in rows:
            rows[r.strategy_id].unrealized = Decimal(str(r.unrealized_pnl))
            rows[r.strategy_id].equity = Decimal(str(r.equity_usd))
    return list(rows.values())


def totals(rows: Sequence[StrategyPnl]) -> StrategyPnl:
    t = StrategyPnl("total", "Total")
    for r in rows:
        t.gross += r.gross
        t.fees += r.fees
        t.funding += r.funding
        t.slippage += r.slippage
        t.ai_cost += r.ai_cost
        t.n_fills += r.n_fills
        t.today_net += r.today_net
        t.unrealized += r.unrealized
    return t


def waterfall_steps(t: StrategyPnl) -> list[tuple[str, float]]:
    return [
        ("Bruto", float(t.gross)),
        ("Comisiones", -float(t.fees)),
        ("Funding", float(t.funding)),
        ("Slippage", -float(t.slippage)),
        ("IA", -float(t.ai_cost)),
        ("Neto", float(t.net_after_ai)),
    ]


# ---------------------------------------------------------------------------
# Equity y operaciones (vista 1)
# ---------------------------------------------------------------------------
def equity_series(conn: Connection, since: datetime) -> dict[str, list[tuple[datetime, float]]]:
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in conn.execute(text(
        "SELECT strategy_id, ts, equity_usd FROM equity_curve WHERE ts >= :since ORDER BY ts"
    ), {"since": since}):
        out[r.strategy_id].append((r.ts, float(r.equity_usd)))
    return dict(out)


@dataclass
class EquityKpis:
    equity: float | None = None
    day_change: float | None = None
    drawdown: float | None = None        # máximo drawdown de la ventana, fracción negativa
    drawdown_sleeve: str | None = None   # de qué sleeve es


def equity_kpis(series: dict[str, list[tuple[datetime, float]]]) -> EquityKpis:
    if not series:
        return EquityKpis()
    latest = sum(pts[-1][1] for pts in series.values() if pts)
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    first_today = [next((v for t, v in pts if t >= start_of_day), None) for pts in series.values()]
    day = (latest - sum(v for v in first_today if v is not None)
           if all(v is not None for v in first_today) else None)
    dds = {sid: max_drawdown([v for _, v in pts]) for sid, pts in series.items() if len(pts) > 1}
    worst = min(dds, key=lambda k: dds[k], default=None)
    return EquityKpis(equity=latest, day_change=day,
                      drawdown=None if worst is None else dds[worst],
                      drawdown_sleeve=worst if worst is not None and dds[worst] < 0 else None)


def fills_on(conn: Connection, day: date) -> list[Fill]:
    return [f for f in fetch_fills(conn) if utc_day(f.ts) == day]


def fills_since(conn: Connection, since: datetime) -> list[Fill]:
    return [f for f in fetch_fills(conn) if f.ts >= since]


def positions(conn: Connection) -> dict[Any, ViewPosition]:
    return load_positions_view(conn)


# ---------------------------------------------------------------------------
# Mercado: el archivo de Coinalyze
# ---------------------------------------------------------------------------
@dataclass
class VenueRow:
    exchange: str
    asset: str
    oi_usd: float | None = None
    oi_change_24h: float | None = None
    funding_pct: float | None = None
    liq_long_24h: float | None = None
    liq_short_24h: float | None = None


def _asset(symbol: str) -> str:
    return "ETH" if symbol.upper().startswith(("ETH", "PF_ETH")) else "BTC"


def market(conn: Connection) -> list[VenueRow]:
    now = datetime.now(UTC)
    rows: dict[tuple[str, str], VenueRow] = {}
    for s in SERIES:
        rows.setdefault((s.exchange, _asset(s.symbol)), VenueRow(s.exchange, _asset(s.symbol)))
    oi = conn.execute(text("""
        SELECT DISTINCT ON (symbol) symbol, exchange, ts, close FROM derivatives_metrics
        WHERE metric = 'open_interest' AND interval = '1min' ORDER BY symbol, ts DESC
    """)).all()
    for r in oi:
        row = rows[(r.exchange, _asset(r.symbol))]
        row.oi_usd = r.close
        old = conn.execute(text("""
            SELECT close FROM derivatives_metrics WHERE symbol = :s AND metric = 'open_interest'
              AND interval = '1min' AND ts <= :t ORDER BY ts DESC LIMIT 1
        """), {"s": r.symbol, "t": r.ts - timedelta(hours=24)}).scalar_one_or_none()
        if old:
            row.oi_change_24h = r.close / old - 1
    for r in conn.execute(text("""
        SELECT DISTINCT ON (symbol) symbol, exchange, close FROM derivatives_metrics
        WHERE metric = 'funding_rate' AND interval = '1hour' ORDER BY symbol, ts DESC
    """)):
        rows[(r.exchange, _asset(r.symbol))].funding_pct = r.close
    for r in conn.execute(text("""
        SELECT symbol, exchange, sum(long_value) AS l, sum(short_value) AS s
        FROM derivatives_metrics
        WHERE metric = 'liquidations' AND interval = '1min' AND ts >= :since
        GROUP BY symbol, exchange
    """), {"since": now - timedelta(hours=24)}):
        row = rows[(r.exchange, _asset(r.symbol))]
        row.liq_long_24h, row.liq_short_24h = r.l, r.s
    return sorted(rows.values(), key=lambda v: (v.asset, v.exchange))


Curves = dict[str, list[tuple[datetime, float]]]


def metric_series(conn: Connection, *, asset: str, metric: str, interval: str, since: datetime,
                  every_minutes: int = 1, index: bool = False) -> Curves:
    """Una serie por venue. Con `index`, base 100 al inicio: los venues tienen tamaños
    muy distintos y en valores absolutos el pequeño se vería plano."""
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in conn.execute(text("""
        SELECT exchange, symbol, ts, close FROM derivatives_metrics
        WHERE metric = :m AND interval = :i AND ts >= :since
          AND (extract(epoch FROM ts)::bigint / 60) % :every = 0
        ORDER BY ts
    """), {"m": metric, "i": interval, "since": since, "every": every_minutes}):
        if _asset(r.symbol) == asset and r.close is not None:
            out[r.exchange].append((r.ts, float(r.close)))
    if index:
        for k, pts in out.items():
            base = pts[0][1] if pts and pts[0][1] else None
            out[k] = [(t, v / base * 100) for t, v in pts] if base else pts
    return {k: out[k] for k in sorted(out)}            # orden estable en la leyenda


# Un color fijo por venue, el mismo en todos los gráficos.
VENUE_COLORS = {e: c for e, c in zip(
    sorted({s.exchange for s in SERIES}),
    ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"),
    strict=False,
)}


# ---------------------------------------------------------------------------
# Salud (vista 5): el archivo
# ---------------------------------------------------------------------------
@dataclass
class SeriesHealth:
    metric: str
    interval: str
    n_series: int = 0
    max_lag: timedelta | None = None
    uncovered: int = 0                  # series con tiempo sin cubrir en la ventana
    source_holes: int = 0               # barras ausentes en la fuente (informativo)
    missing: list[str] = field(default_factory=list)


def archive_health(conn: Connection, hours: float = 24) -> list[SeriesHealth]:
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    groups: dict[tuple[str, str], SeriesHealth] = {}
    for s in SERIES:
        if s.interval == "daily":
            continue
        g = groups.setdefault((s.metric, s.interval), SeriesHealth(s.metric, s.interval))
        g.n_series += 1
        last, first = last_covered(conn, s), first_covered(conn, s)
        if last is None or first is None:
            g.missing.append(s.symbol)
            continue
        lag = now - last
        g.max_lag = lag if g.max_lag is None else max(g.max_lag, lag)
        start = max(since, first)
        if coverage_gaps(conn, s, start, last):
            g.uncovered += 1
        if s.metric in DENSE_METRICS:
            g.source_holes += len(sequence_holes(conn, s, start, last))
    return list(groups.values())


def last_archive_run(conn: Connection) -> datetime | None:
    value: datetime | None = conn.execute(text("SELECT max(run_at) FROM ingest_runs")).scalar_one()
    return value


# ---------------------------------------------------------------------------
# Riesgo (vista 6)
# ---------------------------------------------------------------------------
FLOOR_LIMITS = [
    ("Apalancamiento máx. BTC/ETH", f"{MAX_LEVERAGE_MAJORS}×"),
    ("Apalancamiento máx. alts", f"{MAX_LEVERAGE_ALTS}× (spot, sin cortos)"),
    ("Riesgo por operación", f"{MAX_RISK_PER_TRADE_PCT:.0%}"),
    ("Nocional por posición", f"{MAX_NOTIONAL_PCT_EQUITY:.0%} del equity"),
    ("Exposición bruta", f"{MAX_GROSS_EXPOSURE_PCT:.0%}"),
    ("Exposición neta", f"{MAX_NET_EXPOSURE_PCT:.0%}"),
    ("Posiciones abiertas", f"{MAX_OPEN_POSITIONS}"),
    ("Stop de pérdida diaria", f"{DAILY_LOSS_STOP_PCT:.0%}"),
    ("Stop de pérdida semanal", f"{WEEKLY_LOSS_STOP_PCT:.0%}"),
]


def series_label(s: Series) -> str:
    return f"{s.exchange} {s.symbol}"
