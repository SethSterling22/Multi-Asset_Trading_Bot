"""Archivador de Coinalyze: OI, funding, liquidaciones y long/short a `derivatives_metrics`.

Sustituye al grabador por WebSocket del informe (DECISIONS.md D-009): el intradía
de Coinalyze dura ~1,500 puntos (25 h a 1 minuto), así que basta con una ejecución
REST cada pocas horas para no perder nada, sin sockets que vigilar y sin depender
de que un exchange offshore acepte una IP de EE.UU.

Cada ejecución, por serie (símbolo, métrica, intervalo):
1. pide desde el final de lo ya cubierto, menos una barra de solape;
2. guarda solo barras COMPLETAS (la que está en curso se descarta);
3. inserta de forma idempotente (ON CONFLICT DO NOTHING): re-ejecutar no duplica;
4. registra en `ingest_runs` la ventana que cubrió.

Cada grupo va en su propia transacción: si el proceso muere a mitad, lo escrito se
queda y la siguiente ejecución rellena el resto, siempre que llegue antes de que
Coinalyze borre el intradía.

Huecos: `coverage_gaps` da el tiempo que ninguna ejecución cubrió (vale también
para las liquidaciones, que son dispersas); `sequence_holes` da las barras que
faltan DENTRO de lo cubierto en las series densas. Detección por secuencia de
barras, nunca por timestamps sueltos (CLAUDE.md §2).
"""

from __future__ import annotations

import argparse
import os
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from core import log
from data.sources.coinalyze import (
    INTERVAL_SECONDS,
    INTRADAY_RETAINED_POINTS,
    CoinalyzeClient,
)

SOURCE = "coinalyze"
BACKFILL_START = datetime(2020, 1, 1, tzinfo=UTC)   # el diario de Binance empieza 2020-01-21
DENSE_METRICS = frozenset({"open_interest", "funding_rate", "long_short_ratio"})
UNITS = {"open_interest": "usd", "funding_rate": "percent", "liquidations": "usd",
         "long_short_ratio": "ratio"}
# El diario de OI y liquidaciones va en unidad nativa: en USD se corta en ~1,500 días.
# Todos los mercados de abajo están denominados en el activo base (BTC, ETH), según
# `oi_lq_vol_denominated_in` de /future-markets.
NATIVE_DAILY = frozenset({"open_interest", "liquidations"})


def in_usd(metric: str, interval: str) -> bool:
    return not (interval == "daily" and metric in NATIVE_DAILY)


def unit(metric: str, interval: str) -> str:
    return UNITS[metric] if in_usd(metric, interval) else "base_asset"


@dataclass(frozen=True, slots=True)
class Series:
    symbol: str
    exchange: str
    metric: str
    interval: str


# BTC y ETH en los venues con más profundidad que cubre Coinalyze. Los perps de
# EE.UU. de Coinbase no están (D-009): esto es contexto de mercado, no el funding
# que cobraría funding_carry.
_PERPS = [
    ("BTCUSDT_PERP.A", "binance"), ("ETHUSDT_PERP.A", "binance"),
    ("BTCUSDT.6", "bybit"), ("ETHUSDT.6", "bybit"),
    ("BTCUSDT_PERP.3", "okx"), ("ETHUSDT_PERP.3", "okx"),
    ("BTC.H", "hyperliquid"), ("ETH.H", "hyperliquid"),
    ("pf_xbtusd.K", "kraken_futures"), ("pf_ethusd.K", "kraken_futures"),
]
_WITH_LONG_SHORT = {"binance", "bybit"}        # has_long_short_ratio_data en /future-markets

SERIES: tuple[Series, ...] = tuple(
    [Series(s, e, "open_interest", "1min") for s, e in _PERPS]
    + [Series(s, e, "liquidations", "1min") for s, e in _PERPS]
    + [Series(s, e, "funding_rate", "1hour") for s, e in _PERPS]
    + [Series(s, e, "long_short_ratio", "1hour") for s, e in _PERPS if e in _WITH_LONG_SHORT]
    + [Series(s, e, m, "daily") for s, e in _PERPS
       for m in ("open_interest", "funding_rate", "liquidations")]
)


def _step(interval: str) -> int:
    return INTERVAL_SECONDS[interval]


def bar_floor(ts: int, interval: str) -> int:
    return ts - ts % _step(interval)


def to_row(series: Series, bar: dict[str, Any]) -> dict[str, Any]:
    base = {
        "ts": datetime.fromtimestamp(bar["t"], UTC), "source": SOURCE, "symbol": series.symbol,
        "exchange": series.exchange, "metric": series.metric, "interval": series.interval,
        "unit": unit(series.metric, series.interval),
        "open": None, "high": None, "low": None, "close": None,
        "long_value": None, "short_value": None, "ratio": None,
    }
    if series.metric in ("open_interest", "funding_rate"):
        base.update(open=bar["o"], high=bar["h"], low=bar["l"], close=bar["c"])
    elif series.metric == "liquidations":
        base.update(long_value=bar["l"], short_value=bar["s"])
    else:
        base.update(ratio=bar["r"], long_value=bar["l"], short_value=bar["s"])
    return base


_INSERT = text("""
    INSERT INTO derivatives_metrics (ts, source, symbol, exchange, metric, interval, unit,
                                     open, high, low, close, long_value, short_value, ratio)
    VALUES (:ts, :source, :symbol, :exchange, :metric, :interval, :unit,
            :open, :high, :low, :close, :long_value, :short_value, :ratio)
    ON CONFLICT (source, symbol, metric, interval, ts) DO NOTHING
""")
_RUN = text("""
    INSERT INTO ingest_runs (source, symbol, metric, interval, covered_from, covered_to,
                             rows_received, rows_inserted)
    VALUES (:source, :symbol, :metric, :interval, :covered_from, :covered_to,
            :rows_received, :rows_inserted)
""")
_LAST = text("""
    SELECT max(covered_to) FROM ingest_runs
    WHERE source = :source AND symbol = :symbol AND metric = :metric AND interval = :interval
""")


def last_covered(conn: Connection, s: Series) -> datetime | None:
    value: datetime | None = conn.execute(_LAST, {
        "source": SOURCE, "symbol": s.symbol, "metric": s.metric, "interval": s.interval,
    }).scalar_one()
    return value


def retention_floor(interval: str, now: int) -> int:
    """Lo más antiguo que Coinalyze garantiza tener en este intervalo."""
    if interval == "daily":
        return int(BACKFILL_START.timestamp())
    return now - INTRADAY_RETAINED_POINTS * _step(interval)


@dataclass(frozen=True, slots=True)
class RunResult:
    series: Series
    covered_from: datetime
    covered_to: datetime
    received: int
    inserted: int


def archive_group(
    engine: Engine, client: CoinalyzeClient, group: Sequence[Series], now: int
) -> list[RunResult]:
    """Una petición por grupo (misma métrica e intervalo), una transacción por grupo."""
    metric, interval = group[0].metric, group[0].interval
    step = _step(interval)
    end = bar_floor(now, interval)                    # exclusivo: inicio de la barra en curso
    floor = retention_floor(interval, now)
    with engine.connect() as conn:
        starts = {}
        for s in group:
            last = last_covered(conn, s)
            starts[s] = max(floor, int(last.timestamp()) - step) if last else floor
    start = min(starts.values())
    if start >= end:
        return []
    history = client.history(metric, [s.symbol for s in group], interval, start, end - 1,
                             usd=in_usd(metric, interval))

    results = []
    with engine.begin() as conn:
        for s in group:
            bars = [b for b in history.get(s.symbol, []) if start <= b["t"] < end]
            inserted = 0
            for bar in bars:
                inserted += conn.execute(_INSERT, to_row(s, bar)).rowcount
            covered_from = datetime.fromtimestamp(max(starts[s], floor), UTC)
            covered_to = datetime.fromtimestamp(end, UTC)
            conn.execute(_RUN, {"source": SOURCE, "symbol": s.symbol, "metric": s.metric,
                                "interval": s.interval, "covered_from": covered_from,
                                "covered_to": covered_to, "rows_received": len(bars),
                                "rows_inserted": inserted})
            results.append(RunResult(s, covered_from, covered_to, len(bars), inserted))
    return results


def groups(series: Sequence[Series]) -> list[list[Series]]:
    by_key: dict[tuple[str, str], list[Series]] = defaultdict(list)
    for s in series:
        by_key[(s.metric, s.interval)].append(s)
    return list(by_key.values())


def archive(engine: Engine, client: CoinalyzeClient, series: Sequence[Series] = SERIES,
            *, clock: Callable[[], float] = time.time) -> list[RunResult]:
    results = []
    logger = log.get_logger()
    for group in groups(series):
        res = archive_group(engine, client, group, int(clock()))
        for r in res:
            logger.info("archived", action="archive", symbol=r.series.symbol,
                        reason=f"{r.series.metric}/{r.series.interval}",
                        received=r.received, inserted=r.inserted)
        results.extend(res)
    return results


# ---------------------------------------------------------------------------
# Huecos
# ---------------------------------------------------------------------------
def coverage_gaps(conn: Connection, s: Series, since: datetime,
                  until: datetime) -> list[tuple[datetime, datetime]]:
    """Intervalos de [since, until) que ninguna ejecución cubrió."""
    windows = conn.execute(text("""
        SELECT covered_from, covered_to FROM ingest_runs
        WHERE source = :source AND symbol = :symbol AND metric = :metric
          AND interval = :interval AND covered_to > :since AND covered_from < :until
        ORDER BY covered_from
    """), {"source": SOURCE, "symbol": s.symbol, "metric": s.metric, "interval": s.interval,
           "since": since, "until": until}).all()
    gaps: list[tuple[datetime, datetime]] = []
    cursor = since
    for start, end in windows:
        if start > cursor:
            gaps.append((cursor, min(start, until)))
        cursor = max(cursor, end)
        if cursor >= until:
            break
    if cursor < until:
        gaps.append((cursor, until))
    return gaps


def sequence_holes(conn: Connection, s: Series, since: datetime,
                   until: datetime) -> list[datetime]:
    """Barras que faltan dentro de lo cubierto (solo series densas)."""
    if s.metric not in DENSE_METRICS:
        raise ValueError(f"{s.metric} es dispersa: su ausencia no es un hueco")
    present = {r[0] for r in conn.execute(text("""
        SELECT ts FROM derivatives_metrics
        WHERE source = :source AND symbol = :symbol AND metric = :metric
          AND interval = :interval AND ts >= :since AND ts < :until
    """), {"source": SOURCE, "symbol": s.symbol, "metric": s.metric, "interval": s.interval,
           "since": since, "until": until})}
    step = timedelta(seconds=_step(s.interval))
    uncovered = coverage_gaps(conn, s, since, until)
    holes = []
    t = datetime.fromtimestamp(bar_floor(int(since.timestamp()), s.interval), UTC)
    if t < since:
        t += step
    while t < until:
        inside_gap = any(a <= t < b for a, b in uncovered)
        if not inside_gap and t not in present:
            holes.append(t)
        t += step
    return holes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def report(engine: Engine, hours: float, max_lag_hours: float,
           series: Sequence[Series] = SERIES) -> int:
    """Imprime huecos y retraso de las últimas `hours` horas. Devuelve cuántos problemas hay.

    - Sin cubrir: tiempo entre ejecuciones que ninguna cubrió. Es lo que se pierde
      para siempre cuando el archivador está parado más de lo que Coinalyze conserva.
    - Retraso: tiempo desde la última ejecución. No es un hueco todavía; lo es si
      supera `max_lag_hours` (el archivador está parado).
    - Ausentes en la fuente: barras de series densas que faltan DENTRO de lo cubierto.
      Coinalyze no las devolvió; se informan, no se inventan.
    """
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    problems = 0
    with engine.connect() as conn:
        for s in series:
            if s.interval == "daily":
                continue
            last = last_covered(conn, s)
            if last is None:
                problems += 1
                print(f"SIN DATOS {s.symbol} {s.metric}/{s.interval}")
                continue
            gaps = coverage_gaps(conn, s, since, last)
            holes = sequence_holes(conn, s, since, last) if s.metric in DENSE_METRICS else []
            lag = now - last
            label = f"{s.symbol} {s.metric}/{s.interval}"
            if gaps:
                problems += 1
                spans = [(a.isoformat(), b.isoformat()) for a, b in gaps[:3]]
                print(f"SIN CUBRIR {label}: {spans}")
            if lag > timedelta(hours=max_lag_hours):
                problems += 1
                print(f"RETRASO {label}: última ejecución hace {lag}")
            if holes:
                print(f"ausentes en la fuente {label}: {len(holes)} barras,"
                      f" p.ej. {[h.isoformat() for h in holes[:3]]}")
    print(f"{len(series)} series · {problems} problemas en las últimas {hours:g} h"
          f" (retraso máximo {max_lag_hours:g} h)")
    return problems


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="Archivador de Coinalyze")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="una ejecución (o un bucle con --every-hours)")
    run.add_argument("--every-hours", type=float)
    rep = sub.add_parser("report", help="huecos de las últimas N horas")
    rep.add_argument("--hours", type=float, default=48)
    # Coinalyze conserva ~25 h a 1 minuto: con más de 12 h sin ejecutar, el margen se acaba.
    rep.add_argument("--max-lag-hours", type=float, default=12)
    args = ap.parse_args()

    log.configure("ingest-coinalyze")
    engine = create_engine(os.environ["DATABASE_URL"])
    if args.cmd == "report":
        raise SystemExit(1 if report(engine, args.hours, args.max_lag_hours) else 0)
    client = CoinalyzeClient(os.environ.get("COINALYZE_API_KEY", ""))
    while True:
        results = archive(engine, client)
        print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z · {len(results)} series ·"
              f" {sum(r.inserted for r in results)} filas nuevas", flush=True)
        if not args.every_hours:
            return
        time.sleep(args.every_hours * 3600)


if __name__ == "__main__":
    main()
