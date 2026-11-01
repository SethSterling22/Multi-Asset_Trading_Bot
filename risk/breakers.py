"""Kill switch y circuit breakers (informe §12.4, patrón 6).

KILL SWITCH (CLAUDE.md §1.7): activo si CUALQUIER fuente lo dice —fila en
`risk_events`, clave en Redis, fichero /run/trading/HALT—, y un error al
comprobar una fuente cuenta como activo. Las tres fuentes son obligatorias al
construirlo: no existe un kill switch "parcial". Es el único control que tiene
que funcionar cuando todo lo demás está roto.

BREAKERS: funciones puras sobre la historia persistida (órdenes, fills, equity),
no contadores en memoria. Un reinicio no los resetea, porque su entrada sale del
ledger y de `orders`. Los umbrales vienen de core/risk_floor.py cuando existen
ahí; el resto se pasa explícitamente al construir la política de cada venue.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine, text

from core.risk_floor import (
    DAILY_LOSS_STOP_PCT,
    MAX_CONSECUTIVE_LOSSES,
    MAX_ORDERS_PER_DAY,
    MAX_ORDERS_PER_MINUTE,
    WEEKLY_LOSS_STOP_PCT,
)
from risk.verdict import ALLOWED, Rejected, Verdict

HALT_FILE = Path("/run/trading/HALT")
REDIS_HALT_KEY = "trading:halt"

# Informe §12.4: error-rate por venue > 20% en 60 s abre el breaker.
VENUE_ERROR_RATE_MAX = 0.20
VENUE_ERROR_WINDOW = timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
KillSource = Callable[[str], bool]   # strategy_id -> ¿está activo?


class RedisLike(Protocol):
    def exists(self, *names: str) -> int: ...


def file_source(path: Path = HALT_FILE) -> KillSource:
    """Activo si existe el fichero. Que no exista el directorio = no activo."""
    def check(strategy_id: str) -> bool:
        return path.exists() or path.with_name(f"{path.name}.{strategy_id}").exists()
    return check


def redis_source(client: RedisLike, key: str = REDIS_HALT_KEY) -> KillSource:
    def check(strategy_id: str) -> bool:
        return client.exists(key, f"{key}:{strategy_id}") > 0
    return check


def risk_events_source(engine: Engine) -> KillSource:
    """El último kill_switch_on/off de 'global' o de la estrategia decide, por scope."""
    query = text("""
        SELECT DISTINCT ON (scope) scope, kind
        FROM risk_events
        WHERE kind IN ('kill_switch_on', 'kill_switch_off')
          AND scope IN ('global', :strategy_id)
        ORDER BY scope, ts DESC, id DESC
    """)

    def check(strategy_id: str) -> bool:
        with engine.connect() as conn:
            rows = conn.execute(query, {"strategy_id": strategy_id}).all()
        return any(r.kind == "kill_switch_on" for r in rows)
    return check


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    active: bool
    reasons: tuple[str, ...]


class KillSwitch:
    def __init__(
        self,
        *,
        risk_events: KillSource,
        redis: KillSource,
        halt_file: KillSource,
        extra: dict[str, KillSource] | None = None,
    ) -> None:
        # Las tres fuentes son obligatorias. `extra` solo puede AÑADIR fuentes
        # (p.ej. el flag de config.json); nunca sustituir una de las tres.
        self._sources: dict[str, KillSource] = {
            "risk_events": risk_events,
            "redis": redis,
            "halt_file": halt_file,
            **(extra or {}),
        }

    def state(self, strategy_id: str) -> KillSwitchState:
        reasons: list[str] = []
        for name, source in self._sources.items():
            try:
                if source(strategy_id):
                    reasons.append(f"{name}: activo")
            except Exception as exc:  # fail-closed: CUALQUIER error cuenta como activo
                reasons.append(f"{name}: error al comprobar ({type(exc).__name__}: {exc})")
        return KillSwitchState(active=bool(reasons), reasons=tuple(reasons))

    def check(self, strategy_id: str) -> Verdict:
        st = self.state(strategy_id)
        if st.active:
            return Rejected("kill_switch", "; ".join(st.reasons))
        return ALLOWED


# ---------------------------------------------------------------------------
# Los 8 circuit breakers
# ---------------------------------------------------------------------------
def venue_error_rate(results: Iterable[tuple[datetime, bool]], now: datetime) -> Verdict:
    """1. Error-rate por venue: > 20% de respuestas fallidas en los últimos 60 s."""
    window = [ok for ts, ok in results if now - VENUE_ERROR_WINDOW <= ts <= now]
    if not window:
        return ALLOWED
    errors = sum(1 for ok in window if not ok)
    rate = errors / len(window)
    if rate > VENUE_ERROR_RATE_MAX:
        return Rejected("venue_error_rate", f"{errors}/{len(window)} errores en 60 s")
    return ALLOWED


def stale_data(last_message: datetime | None, ttl: timedelta, now: datetime) -> Verdict:
    """2. Datos rancios: por EDAD del último mensaje, nunca por on_close (CLAUDE.md §2)."""
    if last_message is None:
        return Rejected("stale_data", "nunca se recibió un mensaje")
    age = now - last_message
    if age > ttl:
        return Rejected("stale_data", f"último mensaje hace {age}, TTL {ttl}")
    return ALLOWED


def loss_limits(
    *,
    equity: Decimal,
    day_start_equity: Decimal | None,
    week_start_equity: Decimal | None,
    daily_limit: float = DAILY_LOSS_STOP_PCT,
) -> Verdict:
    """3. Pérdida diaria y semanal.

    Sin equity de referencia NO se opera: el bug que tenía el Wheel era justo
    devolver "no superado" cuando el proceso arrancaba a mitad de sesión.
    `daily_limit` solo puede ser más estricto que el floor.
    """
    if day_start_equity is None or week_start_equity is None:
        return Rejected("loss_limit_unknown", "sin equity de inicio de día o de semana")
    if day_start_equity <= 0 or week_start_equity <= 0:
        return Rejected("loss_limit_unknown", "equity de referencia <= 0")
    limit = min(daily_limit, DAILY_LOSS_STOP_PCT)
    daily = float((day_start_equity - equity) / day_start_equity)
    if daily >= limit:
        return Rejected("daily_loss", f"pérdida del día {daily:.4f} >= {limit}")
    weekly = float((week_start_equity - equity) / week_start_equity)
    if weekly >= WEEKLY_LOSS_STOP_PCT:
        return Rejected("weekly_loss", f"pérdida de la semana {weekly:.4f} >= "
                                       f"{WEEKLY_LOSS_STOP_PCT}")
    return ALLOWED


def consecutive_losses(closed_trade_pnl: Sequence[Decimal]) -> Verdict:
    """4. N pérdidas seguidas (las más recientes al final)."""
    streak = 0
    for pnl in reversed(closed_trade_pnl):
        if pnl >= 0:
            break
        streak += 1
    if streak >= MAX_CONSECUTIVE_LOSSES:
        return Rejected("consecutive_losses", f"{streak} pérdidas seguidas")
    return ALLOWED


def order_rate(order_times: Iterable[datetime], now: datetime) -> Verdict:
    """5. Tasa de órdenes: protege contra bucles de bug. Cuenta también la que se va a enviar."""
    times = list(order_times)
    last_minute = sum(1 for t in times if now - timedelta(minutes=1) < t <= now) + 1
    if last_minute > MAX_ORDERS_PER_MINUTE:
        return Rejected("order_rate", f"{last_minute} órdenes en 1 min > {MAX_ORDERS_PER_MINUTE}")
    last_day = sum(1 for t in times if now - timedelta(days=1) < t <= now) + 1
    if last_day > MAX_ORDERS_PER_DAY:
        return Rejected("order_rate", f"{last_day} órdenes en 24 h > {MAX_ORDERS_PER_DAY}")
    return ALLOWED


def rejections(recent_statuses: Sequence[str], max_consecutive: int) -> Verdict:
    """6. Rechazos del venue seguidos (estados de orden, el más reciente al final)."""
    if max_consecutive < 1:
        raise ValueError("max_consecutive >= 1")
    streak = 0
    for status in reversed(recent_statuses):
        if status != "REJECTED":
            break
        streak += 1
    if streak >= max_consecutive:
        return Rejected("rejections", f"{streak} órdenes rechazadas seguidas")
    return ALLOWED


def extreme_spread(
    spread_bps: Decimal | None, atr_bps: Decimal | None, *, max_spread_to_atr: Decimal
) -> Verdict:
    """7. Spread extremo frente al ATR: el libro no es operable."""
    if spread_bps is None or atr_bps is None or atr_bps <= 0:
        return Rejected("extreme_spread", "sin spread o ATR para evaluar")
    ratio = spread_bps / atr_bps
    if ratio > max_spread_to_atr:
        return Rejected("extreme_spread", f"spread/ATR {ratio:.3f} > {max_spread_to_atr}")
    return ALLOWED


def reconciliation_mismatch(unexplained_mismatches: int) -> Verdict:
    """8. Cualquier mismatch de reconciliación sin explicar para el trading nuevo."""
    if unexplained_mismatches > 0:
        return Rejected("reconciliation", f"{unexplained_mismatches} mismatches sin explicar")
    return ALLOWED
