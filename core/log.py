"""Logging estructurado (structlog) con campos fijos, en JSON.

Cada evento lleva siempre ts, level, service, strategy_id, symbol, action y
reason, aunque alguno vaya a null: así se puede filtrar en Loki sin adivinar
qué campos tiene cada línea.

Cada rechazo de un guard pasa por `guard_rejected`, que exige `reason`: un
`return` temprano sin motivo registrado no se puede auditar (informe §10, 0.6).
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

FIXED_FIELDS = ("ts", "level", "service", "strategy_id", "symbol", "action", "reason")


def _fixed_fields(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for name in FIXED_FIELDS:
        event_dict.setdefault(name, None)
    return event_dict


def configure(service: str, *, stream: TextIO | None = None) -> None:
    """Configura structlog para el proceso. Se llama una vez al arrancar el servicio."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            _fixed_fields,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(**initial: Any) -> Any:
    return structlog.get_logger(**initial)


def guard_rejected(
    log: Any, *, strategy_id: str, symbol: str | None, action: str, reason: str, **extra: Any
) -> None:
    """Registra el rechazo de un guard. `reason` es obligatorio y no puede ir vacío."""
    if not reason or not reason.strip():
        raise ValueError("un rechazo de guard sin reason no se puede auditar")
    log.warning(
        "guard_rejected",
        strategy_id=strategy_id,
        symbol=symbol,
        action=action,
        reason=reason,
        **extra,
    )
