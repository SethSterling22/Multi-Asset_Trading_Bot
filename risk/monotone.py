"""El espacio de acción del LLM: solo puede llevar un Intent hacia MENOS riesgo.

CLAUDE.md §1.2. Esta es la segunda de las tres capas (la primera son los CHECK
de la tabla `hypothesis`; la tercera, tests/guards/test_llm_monotone.py, que
prueba cada `EffectKind` con parámetros válidos y hostiles).

Un efecto puede vetar una entrada, reducir el tamaño con un multiplicador en
[LLM_SIZE_MULTIPLIER_MIN, LLM_SIZE_MULTIPLIER_MAX], estrechar un stop, ampliar
un cooldown o bloquear un componente. No puede abrir lo que la lógica
determinista no propuso, aumentar tamaño, ensanchar o quitar un stop, acortar un
cooldown, cambiar de lado ni de símbolo.

Con esta restricción, el peor caso de un LLM alucinado es un bot que no opera.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import NewType

from core.risk_floor import LLM_SIZE_MULTIPLIER_MAX, LLM_SIZE_MULTIPLIER_MIN

# Factor sobre la DISTANCIA al stop. 1.0 = no cambia; < 1.0 = más cerca.
# Mismos límites que el CHECK stop_only_tightens del schema de referencia.
STOP_DISTANCE_FACTOR_MIN = 0.30
STOP_DISTANCE_FACTOR_MAX = 1.00


class EffectRejectedError(ValueError):
    """Los parámetros del efecto no están dentro de su vocabulario acotado."""


class EffectKind(StrEnum):
    """Vocabulario CERRADO. Espejo del enum `effect_kind` del schema de referencia."""

    OBSERVE_ONLY = "observe_only"
    ANNOTATE_DECISION = "annotate_decision"
    SIZE_MULTIPLIER = "size_multiplier"
    VETO_ENTRY = "veto_entry"
    TIGHTEN_STOP = "tighten_stop"
    EXTEND_COOLDOWN = "extend_cooldown"
    BLOCK_COMPONENT = "block_component"
    PROPOSE_CONFIG_DIFF = "propose_config_diff"


SizeMultiplier = NewType("SizeMultiplier", float)


def size_multiplier(value: object) -> SizeMultiplier:
    """Única forma de construir un SizeMultiplier: valida tipo, finitud y rango."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EffectRejectedError(f"multiplier debe ser numérico, no {type(value).__name__}")
    x = float(value)
    if not math.isfinite(x) or not LLM_SIZE_MULTIPLIER_MIN <= x <= LLM_SIZE_MULTIPLIER_MAX:
        raise EffectRejectedError(
            f"multiplier {value!r} fuera de [{LLM_SIZE_MULTIPLIER_MIN}, {LLM_SIZE_MULTIPLIER_MAX}]"
        )
    return SizeMultiplier(x)


@dataclass(frozen=True, slots=True)
class Intent:
    """Lo que propone la lógica determinista (L2) antes del gate (L3)."""

    strategy_id: str
    component: str
    symbol: str
    side: int                       # +1 largo, −1 corto
    size: Decimal                   # >= 0, en unidades del instrumento
    stop_distance: Decimal | None   # None = la estrategia no usa stop (p.ej. una CSP)
    cooldown_minutes: int = 0
    allowed: bool = True
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError("side debe ser +1 o −1")
        if self.size < 0:
            raise ValueError("size >= 0")
        if self.stop_distance is not None and self.stop_distance <= 0:
            raise ValueError("stop_distance > 0")
        if self.cooldown_minutes < 0:
            raise ValueError("cooldown_minutes >= 0")


def _number(params: dict[str, object], key: str) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise EffectRejectedError(f"{key} debe ser un número finito, no {value!r}")
    return float(value)


def apply_effect(intent: Intent, kind: EffectKind, params: dict[str, object]) -> Intent:
    """Aplica un efecto. Lanza EffectRejectedError si los parámetros no son válidos.

    Todo lo que no sea una de las ramas de abajo es un error: no hay rama por
    defecto que deje pasar un efecto desconocido.
    """
    if not isinstance(kind, EffectKind):
        raise EffectRejectedError(f"effect_kind desconocido: {kind!r}")
    if kind in (EffectKind.OBSERVE_ONLY, EffectKind.PROPOSE_CONFIG_DIFF):
        # propose_config_diff va a la cola de aprobación humana: no toca el Intent.
        return intent
    if kind is EffectKind.ANNOTATE_DECISION:
        tag = params.get("tag")
        if not isinstance(tag, str) or not tag:
            raise EffectRejectedError("annotate_decision necesita un tag de texto")
        return replace(intent, tags=(*intent.tags, tag))
    if kind is EffectKind.VETO_ENTRY:
        return replace(intent, allowed=False, size=Decimal(0))
    if kind is EffectKind.SIZE_MULTIPLIER:
        m = size_multiplier(params.get("multiplier"))
        return replace(intent, size=intent.size * Decimal(str(m)))
    if kind is EffectKind.TIGHTEN_STOP:
        factor = _number(params, "distance_factor")
        if not STOP_DISTANCE_FACTOR_MIN <= factor <= STOP_DISTANCE_FACTOR_MAX:
            raise EffectRejectedError(f"distance_factor {factor} fuera de rango")
        if intent.stop_distance is None:
            return intent   # sin stop no hay nada que estrechar; no se inventa uno
        return replace(intent, stop_distance=intent.stop_distance * Decimal(str(factor)))
    if kind is EffectKind.EXTEND_COOLDOWN:
        minutes = _number(params, "minutes")
        if minutes <= 0:
            raise EffectRejectedError("extend_cooldown solo puede alargar: minutes > 0")
        return replace(intent, cooldown_minutes=intent.cooldown_minutes + math.ceil(minutes))
    if kind is EffectKind.BLOCK_COMPONENT:
        component = params.get("component")
        if not isinstance(component, str) or not component:
            raise EffectRejectedError("block_component necesita el nombre del componente")
        if component in (intent.component, intent.strategy_id):
            return replace(intent, allowed=False, size=Decimal(0))
        return intent
    raise EffectRejectedError(f"effect_kind sin rama: {kind}")   # pragma: no cover


def risk_not_increased(before: Intent, after: Intent) -> bool:
    """True si `after` no asume más riesgo que `before` en ninguna dimensión."""
    same_target = (
        after.strategy_id == before.strategy_id
        and after.component == before.component
        and after.symbol == before.symbol
        and after.side == before.side
    )
    stop_ok = before.stop_distance is None or (
        after.stop_distance is not None and after.stop_distance <= before.stop_distance
    )
    return (
        same_target
        and after.size <= before.size
        and stop_ok
        and after.cooldown_minutes >= before.cooldown_minutes
        and (before.allowed or not after.allowed)
    )
