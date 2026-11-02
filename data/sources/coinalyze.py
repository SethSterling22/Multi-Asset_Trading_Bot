"""Cliente de la API de Coinalyze (https://api.coinalyze.net/v1/doc/).

Hechos verificados el 2026-11-02 con una key real (DECISIONS.md D-009):
- La key va en la cabecera `api_key`; nunca en la URL, para que no acabe en logs.
- 40 llamadas por minuto por key. Cada SÍMBOLO de una petición cuenta como una
  llamada (hasta 20 símbolos por petición).
- Intradía (1min–12hour): solo se guardan ~1,500–2,000 puntos; lo antiguo se borra
  cada día. Diario: sin borrado (BTCUSDT de Binance desde 2020-01-21).
- `t` en segundos, inicio de la barra. La última barra puede estar incompleta.
- Liquidaciones DISPERSAS: un intervalo sin liquidaciones no aparece.
- No cubre los perps de EE.UU. de Coinbase (BIP-…-CDE); "Coinbase" es Coinbase
  International.

Backoff exponencial con jitter ±30% (CLAUDE.md §2); en un 429 se respeta
`Retry-After`. Un error nunca contiene la key.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://api.coinalyze.net/v1/"
CALLS_PER_MINUTE = 40
MAX_SYMBOLS_PER_REQUEST = 20
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0
JITTER = 0.30
USER_AGENT = "Multi-Asset-Trading-Bot/0.1 (archiver)"

INTERVAL_SECONDS = {
    "1min": 60, "5min": 300, "15min": 900, "30min": 1800, "1hour": 3600, "2hour": 7200,
    "4hour": 14400, "6hour": 21600, "12hour": 43200, "daily": 86400,
}
# Lo MÍNIMO que la documentación dice que se conserva en intradía. Para calcular
# qué ventana cubre una ejecución se usa el mínimo: suponer más sería inventar datos.
INTRADAY_RETAINED_POINTS = 1500

ENDPOINTS = {
    "open_interest": "open-interest-history",
    "funding_rate": "funding-rate-history",
    "liquidations": "liquidation-history",
    "long_short_ratio": "long-short-ratio-history",
}
USD_CONVERTIBLE = {"open_interest", "liquidations"}


class CoinalyzeError(RuntimeError):
    """La API respondió con un error que no se arregla reintentando."""


def jittered(seconds: float, rng: random.Random) -> float:
    return seconds * (1 + rng.uniform(-JITTER, JITTER))


@dataclass
class RateLimiter:
    """Ventana deslizante de 60 s: nunca más de CALLS_PER_MINUTE llamadas."""

    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    calls: list[float] = field(default_factory=list)

    def acquire(self, n: int) -> None:
        if n > CALLS_PER_MINUTE:
            raise ValueError(f"{n} llamadas de golpe superan el límite de {CALLS_PER_MINUTE}")
        while True:
            now = self.clock()
            self.calls = [t for t in self.calls if now - t < 60]
            if len(self.calls) + n <= CALLS_PER_MINUTE:
                self.calls.extend([now] * n)
                return
            self.sleep(60 - (now - self.calls[0]) + 0.05)


Transport = Callable[[str, Mapping[str, str]], tuple[int, Mapping[str, str], bytes]]


def urllib_transport(url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
    if not url.startswith(BASE_URL):
        raise ValueError(f"URL inesperada: {url}")
    request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class CoinalyzeClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport = urllib_transport,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("falta COINALYZE_API_KEY")
        self._key = api_key.strip()
        self._transport = transport
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 — jitter de backoff, no criptografía

    def _safe(self, text: str) -> str:
        return text.replace(self._key, "***")

    def get(self, path: str, params: Mapping[str, Any], *, cost: int = 1) -> Any:
        url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"api_key": self._key, "User-Agent": USER_AGENT}
        for attempt in range(MAX_RETRIES + 1):
            self._limiter.acquire(cost)
            try:
                status, resp_headers, body = self._transport(url, headers)
            except (OSError, TimeoutError) as exc:      # red caída, timeout
                status, resp_headers, body = 0, {}, self._safe(str(exc)).encode()
            if status == 200:
                return json.loads(body)
            if status in (400, 401, 403, 404):
                raise CoinalyzeError(f"HTTP {status} en /{path}: "
                                     f"{self._safe(body.decode(errors='replace'))[:200]}")
            if attempt == MAX_RETRIES:
                break
            if status == 429:
                retry_after = float(resp_headers.get("Retry-After", BACKOFF_BASE_SECONDS))
                self._sleep(jittered(max(retry_after, 1.0), self._rng))
            else:                                        # 5xx o error de red
                self._sleep(jittered(BACKOFF_BASE_SECONDS * 2**attempt, self._rng))
        raise CoinalyzeError(f"/{path} sigue fallando tras {MAX_RETRIES} reintentos "
                             f"(último HTTP {status})")

    def history(
        self, metric: str, symbols: Sequence[str], interval: str, start: int, end: int,
        *, usd: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """Historia de `metric` para `symbols` entre `start` y `end` (segundos UTC, inclusivos).

        `usd=False` pide OI y liquidaciones en la unidad nativa del mercado: con
        `convert_to_usd` el histórico DIARIO se corta en ~1,500 puntos (desde 2022-09
        en vez de 2020-01 para BTCUSDT de Binance; comprobado el 2026-11-02).
        """
        if metric not in ENDPOINTS:
            raise ValueError(f"métrica desconocida: {metric}")
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"intervalo desconocido: {interval}")
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(symbols), MAX_SYMBOLS_PER_REQUEST):
            chunk = list(symbols[i:i + MAX_SYMBOLS_PER_REQUEST])
            params: dict[str, Any] = {"symbols": ",".join(chunk), "interval": interval,
                                      "from": start, "to": end}
            if metric in USD_CONVERTIBLE and usd:
                params["convert_to_usd"] = "true"
            for row in self.get(ENDPOINTS[metric], params, cost=len(chunk)):
                out[row["symbol"]] = list(row.get("history") or [])
        for s in symbols:
            out.setdefault(s, [])
        return out
