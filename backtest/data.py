"""Velas diarias públicas de Coinbase (sin API key), con caché en CSV.

GET https://api.exchange.coinbase.com/products/{product}/candles, granularidad
86400, como mucho 300 velas por petición. Respuesta: [time, low, high, open,
close, volume], de la más reciente a la más antigua. Días en UTC.

Un día que falta es un error: no se rellena ni se interpola (CLAUDE.md §2, los
huecos se detectan por secuencia). Un backtest sobre datos rellenados mide el
relleno.
"""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from backtest.engine import Bar

API = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_CANDLES = 300
USER_AGENT = "Multi-Asset-Trading-Bot/0.1 (backtest; read-only public data)"
PAUSE_SECONDS = 0.4          # muy por debajo del límite público de la API


def _fetch_chunk(product: str, start: date, end: date) -> list[list[float]]:
    url = (API.format(product=product)
           + f"?granularity=86400&start={start.isoformat()}T00:00:00Z"
           + f"&end={end.isoformat()}T00:00:00Z")
    if not url.startswith("https://api.exchange.coinbase.com/"):
        raise ValueError(f"URL inesperada: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        rows: list[list[float]] = json.load(response)
    return rows


def check_continuous(bars: list[Bar], start: date, end: date) -> None:
    expected = (end - start).days + 1
    days = [b.day for b in bars]
    if days != [start + timedelta(days=i) for i in range(expected)]:
        have = set(days)
        missing = [start + timedelta(days=i) for i in range(expected)
                   if start + timedelta(days=i) not in have]
        raise ValueError(f"faltan {len(missing)} días, p.ej. {missing[:5]}")


def fetch_daily(product: str, start: date, end: date) -> list[Bar]:
    by_day: dict[date, Bar] = {}
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=MAX_CANDLES - 1))
        for ts, _low, _high, open_, close, _vol in _fetch_chunk(product, cursor, chunk_end):
            day = datetime.fromtimestamp(ts, tz=UTC).date()
            if start <= day <= end:
                by_day[day] = Bar(day, float(open_), float(close))
        cursor = chunk_end + timedelta(days=1)
        time.sleep(PAUSE_SECONDS)
    bars = [by_day[d] for d in sorted(by_day)]
    check_continuous(bars, start, end)
    return bars


def load_daily(product: str, start: date, end: date, cache_dir: Path) -> list[Bar]:
    """Lee de la caché si cubre la ventana; si no, descarga y guarda."""
    path = cache_dir / f"coinbase_{product}_{start}_{end}.csv"
    if path.exists():
        with path.open() as fh:
            bars = [Bar(date.fromisoformat(r["day"]), float(r["open"]), float(r["close"]))
                    for r in csv.DictReader(fh)]
        check_continuous(bars, start, end)
        return bars
    bars = fetch_daily(product, start, end)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["day", "open", "close"])
        writer.writerows([b.day.isoformat(), b.open, b.close] for b in bars)
    return bars
