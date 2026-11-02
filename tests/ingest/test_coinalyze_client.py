"""Cliente de Coinalyze: límite de llamadas, backoff con jitter y la key fuera de todo."""

from __future__ import annotations

import json
import random

import pytest

from data.sources.coinalyze import (
    CALLS_PER_MINUTE,
    JITTER,
    CoinalyzeClient,
    CoinalyzeError,
    RateLimiter,
    jittered,
)

KEY = "11111111-2222-3333-4444-555555555555"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def client(responses: list[tuple[int, dict[str, str], object]]) -> tuple[CoinalyzeClient, list]:
    seen: list[tuple[str, dict[str, str]]] = []

    def transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        seen.append((url, dict(headers)))
        status, hdrs, body = responses.pop(0)
        return status, hdrs, json.dumps(body).encode()

    clock = FakeClock()
    c = CoinalyzeClient(KEY, transport=transport,
                        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
                        sleep=clock.sleep, rng=random.Random(0))
    return c, seen


def test_key_goes_in_the_header_never_in_the_url() -> None:
    c, seen = client([(200, {}, [{"symbol": "BTC.H", "history": []}])])
    c.history("open_interest", ["BTC.H"], "1min", 0, 60)
    ((url, headers),) = seen
    assert KEY not in url and headers["api_key"] == KEY
    assert "convert_to_usd=true" in url


def test_auth_errors_do_not_retry_and_never_leak_the_key() -> None:
    c, seen = client([(401, {}, {"message": f"bad key {KEY}"})])
    with pytest.raises(CoinalyzeError) as exc:
        c.get("exchanges", {})
    assert len(seen) == 1 and KEY not in str(exc.value) and "***" in str(exc.value)


def test_429_waits_for_retry_after_with_jitter() -> None:
    c, _ = client([(429, {"Retry-After": "10"}, {}), (200, {}, [])])
    c.get("exchanges", {})
    waits = c._sleep.__self__.slept  # type: ignore[attr-defined]
    assert len(waits) == 1 and 10 * (1 - JITTER) <= waits[0] <= 10 * (1 + JITTER)


def test_5xx_backs_off_exponentially_then_gives_up() -> None:
    c, seen = client([(503, {}, {})] * 6)
    with pytest.raises(CoinalyzeError, match="reintentos"):
        c.get("exchanges", {})
    waits = c._sleep.__self__.slept  # type: ignore[attr-defined]
    assert len(seen) == 6 and len(waits) == 5
    assert all(waits[i + 1] > waits[i] for i in range(4))          # crece


def test_network_error_is_retried() -> None:
    calls = {"n": 0}

    def transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError(f"timeout con {KEY}")
        return 200, {}, b"[]"

    clock = FakeClock()
    c = CoinalyzeClient(KEY, transport=transport, limiter=RateLimiter(clock, clock.sleep),
                        sleep=clock.sleep, rng=random.Random(0))
    assert c.get("exchanges", {}) == []


def test_jitter_stays_within_30_percent_and_varies() -> None:
    rng = random.Random(1)
    values = [jittered(10, rng) for _ in range(500)]
    assert all(7 <= v <= 13 for v in values) and len({round(v, 3) for v in values}) > 100


def test_rate_limiter_never_exceeds_40_per_minute() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    stamps: list[float] = []
    for _ in range(200):
        limiter.acquire(1)
        stamps.append(clock.t)
    for i, t in enumerate(stamps):
        assert sum(1 for u in stamps[i:] if u - t < 60) <= CALLS_PER_MINUTE
    with pytest.raises(ValueError):
        limiter.acquire(CALLS_PER_MINUTE + 1)


def test_each_symbol_costs_one_call_and_requests_are_chunked() -> None:
    symbols = [f"S{i}.A" for i in range(25)]
    c, seen = client([(200, {}, [{"symbol": s, "history": [{"t": 0}]} for s in symbols[:20]]),
                      (200, {}, [{"symbol": s, "history": []} for s in symbols[20:]])])
    out = c.history("funding_rate", symbols, "1hour", 0, 3600)
    assert len(seen) == 2 and set(out) == set(symbols)
    assert "convert_to_usd" not in seen[0][0]                     # funding no se convierte


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        CoinalyzeClient("  ")
    c, _ = client([])
    with pytest.raises(ValueError):
        c.history("price", ["X"], "1min", 0, 1)
    with pytest.raises(ValueError):
        c.history("open_interest", ["X"], "3min", 0, 1)
