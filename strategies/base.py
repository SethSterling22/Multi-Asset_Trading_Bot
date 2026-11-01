"""Base común de las estrategias: los guards viven aquí, una sola vez.

Duplicar guards entre estrategias es cómo se desincronizan (informe §9.3). Todo
lo que comparten el Wheel y el rebalanceador —parámetros con hot-reload, kill
switch, límites de pérdida, tamaño, caja, cobertura, ledger, feeds— está en este
módulo, y tests/guards/ lo prueba contra la base, no contra cada estrategia.

Cada guard devuelve un Verdict, y cada rechazo pasa por StrategyBase.reject(),
que lo registra con su reason (informe §10, Fase 0.6). Un `return` temprano que
parece redundante aquí es un guard: no se optimiza (CLAUDE.md §1.8).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol

from pydantic import ValidationError
from sqlalchemy import Engine, text

from core import log
from core.config.models import TradingConfig
from core.ledger.equity import EquitySnapshot, write_snapshot
from core.ledger.fills import Fill, insert_fill
from core.risk_floor import MAX_NOTIONAL_PCT_EQUITY, assert_no_mutation
from risk.breakers import KillSwitch, loss_limits, stale_data
from risk.verdict import ALLOWED, Allowed, Rejected, Verdict, first_rejection

__all__ = ["ALLOWED", "Allowed", "Rejected", "Verdict"]

OPTION_MULTIPLIER = 100
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Guards de caja, cobertura y tamaño
# ---------------------------------------------------------------------------
def check_cash_secured(
    *, cash: Decimal, committed_collateral: Decimal, new_collateral: Decimal
) -> Verdict:
    """CSP: la caja libre cubre el colateral de TODAS las puts cortas, más la nueva.

    `committed_collateral` incluye las CSP abiertas y las órdenes pendientes de
    cualquier subyacente: comprobar orden a orden contra la caja total dejaba que
    varios símbolos contasen la misma caja como colateral.
    """
    if new_collateral <= 0:
        return Rejected("invalid_collateral", f"colateral {new_collateral} <= 0")
    free = cash - committed_collateral
    if free < new_collateral:
        return Rejected(
            "no_cash",
            f"caja libre {free:.2f} (caja {cash:.2f} − comprometido "
            f"{committed_collateral:.2f}) < colateral {new_collateral:.2f}",
        )
    return ALLOWED


def check_covered_call(
    *, shares: Decimal, open_short_calls: int, pending_short_calls: int, new_contracts: int
) -> Verdict:
    """CC: 100 acciones por cada contrato vendido, contando los abiertos y los pendientes."""
    if new_contracts < 1:
        return Rejected("invalid_contracts", f"contratos {new_contracts} < 1")
    needed = (open_short_calls + pending_short_calls + new_contracts) * OPTION_MULTIPLIER
    if shares < needed:
        return Rejected("not_covered", f"{shares} acciones < {needed} para cubrir las calls")
    return ALLOWED


def check_collateral_cap(
    *, collateral: Decimal, portfolio_value: Decimal, max_pct: float
) -> Verdict:
    """Colateral de una posición frente al valor de cartera; nunca más laxo que el floor."""
    if portfolio_value <= 0:
        return Rejected("no_equity", f"valor de cartera {portfolio_value} <= 0")
    limit = min(max_pct, MAX_NOTIONAL_PCT_EQUITY)
    share = float(collateral / portfolio_value)
    if share > limit:
        return Rejected("collateral_cap", f"colateral {share:.4f} de la cartera > {limit}")
    return ALLOWED


def cap_spot_sell(*, quantity: Decimal, holdings: Decimal) -> Decimal:
    """Spot sin margen: nunca se vende más de lo que se tiene. Sin cortos."""
    return max(ZERO, min(quantity, holdings))


def cap_spot_buys(
    buys: Sequence[tuple[str, Decimal]], available_cash: Decimal, *, min_notional: Decimal
) -> list[tuple[str, Decimal]]:
    """Spot sin margen: la SUMA de las compras no supera la caja disponible.

    Se asignan en orden; la que no cabe entera se recorta, y las que quedan por
    debajo de `min_notional` se descartan. Topar cada compra por separado dejaba
    que dos compras sumasen más que la caja.
    """
    remaining = max(ZERO, available_cash)
    out: list[tuple[str, Decimal]] = []
    for symbol, notional in buys:
        amount = min(notional, remaining)
        if amount < min_notional:
            continue
        out.append((symbol, amount))
        remaining -= amount
    return out


# ---------------------------------------------------------------------------
# Feeds declarados con su TTL (informe §12.4, patrón 7)
# ---------------------------------------------------------------------------
class Criticality(StrEnum):
    CRITICAL = "critical"     # caído -> sin trading nuevo; se mantiene y se alerta
    IMPORTANT = "important"   # caído -> último valor y tamaño reducido
    OPTIONAL = "optional"     # caído -> feature NULL


@dataclass(frozen=True, slots=True)
class FeedSpec:
    name: str
    ttl: timedelta
    criticality: Criticality


@dataclass(frozen=True, slots=True)
class FeedHealth:
    verdict: Verdict
    degraded: tuple[str, ...]   # feeds IMPORTANT rancios: operar con tamaño reducido


def check_feeds(
    specs: Sequence[FeedSpec], last_seen: Mapping[str, datetime], now: datetime
) -> FeedHealth:
    degraded: list[str] = []
    for spec in specs:
        verdict = stale_data(last_seen.get(spec.name), spec.ttl, now)
        if isinstance(verdict, Allowed):
            continue
        if spec.criticality is Criticality.CRITICAL:
            return FeedHealth(Rejected("stale_feed", f"{spec.name}: {verdict.detail}"), ())
        if spec.criticality is Criticality.IMPORTANT:
            degraded.append(spec.name)
    return FeedHealth(ALLOWED, tuple(degraded))


# ---------------------------------------------------------------------------
# Parámetros con hot-reload
# ---------------------------------------------------------------------------
class ParamsLoader:
    """Recarga config.json cuando cambia su mtime.

    Si la recarga falla la validación, NO se sigue con la versión anterior: el
    loader queda inválido y el gate rechaza todo hasta que el fichero vuelva a ser
    válido. Operar con parámetros que nadie ha validado es peor que no operar.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float | None = None
        self._config: TradingConfig | None = None
        self._error: str | None = None

    def current(self) -> TradingConfig | Rejected:
        try:
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            return Rejected("params_unavailable", f"{self._path}: {exc}")
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._config = TradingConfig.load(self._path)
                self._error = None
            except (ValidationError, ValueError, OSError) as exc:
                self._config = None
                self._error = f"{type(exc).__name__}: {exc}"
        if self._config is None:
            return Rejected("params_invalid", self._error or "config sin cargar")
        return self._config


# ---------------------------------------------------------------------------
# Ledger y equity de referencia para los límites de pérdida
# ---------------------------------------------------------------------------
class LedgerSink(Protocol):
    def record_fill(self, fill: Fill) -> bool: ...
    def record_equity(self, snapshot: EquitySnapshot) -> bool: ...


class LossReference(Protocol):
    def day_start_equity(self, strategy_id: str, now: datetime) -> Decimal | None: ...
    def week_start_equity(self, strategy_id: str, now: datetime) -> Decimal | None: ...


def day_start(now: datetime, tz: tzinfo) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime, tz: tzinfo) -> datetime:
    start = day_start(now, tz)
    return start - timedelta(days=start.weekday())


class PostgresLedger:
    """Escribe fills y snapshots de equity; cada escritura en su propia transacción."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_fill(self, fill: Fill) -> bool:
        with self._engine.begin() as conn:
            return insert_fill(conn, fill)

    def record_equity(self, snapshot: EquitySnapshot) -> bool:
        with self._engine.begin() as conn:
            return write_snapshot(conn, snapshot)


class EquityCurveLossReference:
    """La equity de referencia es el PRIMER snapshot del día (o de la semana) en `tz`.

    Persistido en `equity_curve`: un reinicio a mitad de sesión encuentra el
    snapshot que escribió el proceso anterior, en lugar de tomar la equity actual
    como punto de partida y ocultar la pérdida del día.
    """

    _QUERY = text("""
        SELECT equity_usd FROM equity_curve
        WHERE strategy_id = :strategy_id AND ts >= :since
        ORDER BY ts LIMIT 1
    """)

    def __init__(self, engine: Engine, tz: tzinfo) -> None:
        self._engine = engine
        self._tz = tz

    def _first_since(self, strategy_id: str, since: datetime) -> Decimal | None:
        with self._engine.connect() as conn:
            value = conn.execute(
                self._QUERY, {"strategy_id": strategy_id, "since": since}
            ).scalar_one_or_none()
        return None if value is None else Decimal(str(value))

    def day_start_equity(self, strategy_id: str, now: datetime) -> Decimal | None:
        return self._first_since(strategy_id, day_start(now, self._tz))

    def week_start_equity(self, strategy_id: str, now: datetime) -> Decimal | None:
        return self._first_since(strategy_id, week_start(now, self._tz))


# ---------------------------------------------------------------------------
# La base
# ---------------------------------------------------------------------------
class StrategyBase:
    """Mixin con el gate común. Se combina con la clase del framework (p.ej. Lumibot)."""

    strategy_id: ClassVar[str]
    required_feeds: ClassVar[tuple[FeedSpec, ...]] = ()

    def setup_base(
        self,
        *,
        params: ParamsLoader,
        kill_switch: KillSwitch,
        ledger: LedgerSink,
        loss_reference: LossReference,
        clock: Callable[[], datetime],
        logger: Any | None = None,
    ) -> None:
        # El floor se comprueba al arrancar cada servicio (CLAUDE.md §1.1).
        assert_no_mutation()
        self._params = params
        self._kill_switch = kill_switch
        self._ledger = ledger
        self._loss_reference = loss_reference
        self._clock = clock
        self._log = logger if logger is not None else log.get_logger()
        self._feeds_seen: dict[str, datetime] = {}
        self._degraded: tuple[str, ...] = ()

    # -- registro ----------------------------------------------------------
    def reject(self, verdict: Rejected, *, action: str, symbol: str | None = None) -> Rejected:
        """Registra un rechazo con su reason y lo devuelve."""
        log.guard_rejected(
            self._log,
            strategy_id=self.strategy_id,
            symbol=symbol,
            action=action,
            reason=verdict.code,
            detail=verdict.detail,
        )
        return verdict

    def guard(self, verdict: Verdict, *, action: str, symbol: str | None = None) -> bool:
        """True si se permite; si no, registra el rechazo y devuelve False."""
        if isinstance(verdict, Rejected):
            self.reject(verdict, action=action, symbol=symbol)
            return False
        return True

    # -- feeds -------------------------------------------------------------
    def mark_feed(self, name: str, ts: datetime | None = None) -> None:
        self._feeds_seen[name] = ts or self._clock()

    @property
    def degraded_feeds(self) -> tuple[str, ...]:
        return self._degraded

    # -- parámetros --------------------------------------------------------
    def config(self) -> TradingConfig | Rejected:
        return self._params.current()

    # -- ledger --------------------------------------------------------------
    def record_fill(self, fill: Fill) -> bool:
        if fill.strategy_id != self.strategy_id:
            raise ValueError(f"fill de {fill.strategy_id} registrado por {self.strategy_id}")
        return self._ledger.record_fill(fill)

    def record_equity(self, snapshot: EquitySnapshot) -> bool:
        return self._ledger.record_equity(snapshot)

    # -- el gate -------------------------------------------------------------
    def trading_gate(self, *, equity: Decimal) -> Verdict:
        """Todo lo que tiene que cumplirse antes de abrir CUALQUIER posición nueva.

        Orden: parámetros válidos, sistema activo, kill switch, límites de pérdida,
        feeds. El primer rechazo se registra y se devuelve.
        """
        now = self._clock()
        cfg = self.config()
        if isinstance(cfg, Rejected):
            return self.reject(cfg, action="trading_gate")

        active: Verdict = ALLOWED
        if not cfg.system_status.active:
            active = Rejected("inactive", "system_status.active = false")

        feeds = check_feeds(self.required_feeds, self._feeds_seen, now)
        self._degraded = feeds.degraded

        verdict = first_rejection(
            active,
            self._kill_switch.check(self.strategy_id),
            loss_limits(
                equity=equity,
                day_start_equity=self._loss_reference.day_start_equity(self.strategy_id, now),
                week_start_equity=self._loss_reference.week_start_equity(self.strategy_id, now),
                daily_limit=cfg.risk_guards.daily_loss_limit_percentage,
            ),
            feeds.verdict,
        )
        if isinstance(verdict, Rejected):
            return self.reject(verdict, action="trading_gate")
        return ALLOWED


def config_kill_source(params: ParamsLoader) -> Callable[[str], bool]:
    """El flag `emergency_kill_switch` de config.json, como fuente EXTRA del kill switch.

    Config inválida o ilegible cuenta como activo (fail-closed).
    """
    def check(_: str) -> bool:
        cfg = params.current()
        if isinstance(cfg, Rejected):
            raise RuntimeError(cfg.detail)
        return cfg.system_status.emergency_kill_switch
    return check
