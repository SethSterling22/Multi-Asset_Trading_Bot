"""Grupo 8 de CLAUDE.md §1.8: ningún effect_kind produce un Intent' de mayor riesgo.

Exhaustivo sobre el vocabulario cerrado: para cada EffectKind, una rejilla de
parámetros válidos y hostiles, más 5,000 combinaciones aleatorias. Cada efecto o
se rechaza (EffectRejectedError) o deja un Intent con riesgo <= el original.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import replace
from decimal import Decimal as D

import pytest

from core.risk_floor import LLM_SIZE_MULTIPLIER_MAX, LLM_SIZE_MULTIPLIER_MIN
from risk.monotone import (
    EffectKind,
    EffectRejectedError,
    Intent,
    apply_effect,
    risk_not_increased,
    size_multiplier,
)

pytestmark = pytest.mark.guards

INTENTS = [
    Intent("trend", "signal", "BIP-20DEC30-CDE", 1, D(3), D(500)),
    Intent("trend", "signal", "BIP-20DEC30-CDE", -1, D(2), D(800), cooldown_minutes=60),
    Intent("wheel", "csp", "AAPL", -1, D(1), None),                 # sin stop
    Intent("xsec_momentum", "rank", "SOL-USD", 1, D(0), D(5), allowed=False),
]

HOSTILE: list[object] = [
    None, "", "1.5", "abc", True, False, [], {}, math.nan, math.inf, -math.inf,
    -1, 0, 0.0, 0.1, 0.25, 0.3, 0.5, 0.99, 1, 1.0, 1.0000001, 1.5, 2, 10, 1e308, -0.25,
]
KEYS = ["multiplier", "distance_factor", "minutes", "component", "tag"]


def _param_grid() -> list[dict[str, object]]:
    grid: list[dict[str, object]] = [{}]
    for key, value in itertools.product(KEYS, HOSTILE):
        grid.append({key: value})
    for component in ("trend", "signal", "csp", "wheel", "other", ""):
        grid.append({"component": component})
    for tag in ("regime:high_vol", "x"):
        grid.append({"tag": tag})
    return grid


GRID = _param_grid()


def _check(intent: Intent, kind: EffectKind, params: dict[str, object]) -> None:
    try:
        after = apply_effect(intent, kind, params)
    except EffectRejectedError:
        return
    assert risk_not_increased(intent, after), (intent, kind, params, after)


@pytest.mark.parametrize("kind", list(EffectKind))
def test_every_effect_kind_is_monotone_over_the_grid(kind: EffectKind) -> None:
    for intent, params in itertools.product(INTENTS, GRID):
        _check(intent, kind, params)


def test_random_effects_never_increase_risk() -> None:
    rng = random.Random(20261101)
    for _ in range(5_000):
        intent = rng.choice(INTENTS)
        kind = rng.choice(list(EffectKind))
        params = {rng.choice(KEYS): rng.choice([*HOSTILE, rng.uniform(-5, 5)])}
        _check(intent, kind, params)


def test_vocabulary_mirrors_the_schema_enum() -> None:
    assert {k.value for k in EffectKind} == {
        "observe_only", "annotate_decision", "size_multiplier", "veto_entry",
        "tighten_stop", "extend_cooldown", "block_component", "propose_config_diff",
    }


def test_unknown_effect_kind_is_rejected() -> None:
    # Rechazado por el PRIMER guard (tipo), no solo por la red final sin rama.
    with pytest.raises(EffectRejectedError, match="desconocido"):
        apply_effect(INTENTS[0], "increase_size", {})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [LLM_SIZE_MULTIPLIER_MIN, 0.5, LLM_SIZE_MULTIPLIER_MAX])
def test_size_multiplier_accepts_its_range(value: float) -> None:
    assert size_multiplier(value) == value
    after = apply_effect(INTENTS[0], EffectKind.SIZE_MULTIPLIER, {"multiplier": value})
    assert after.size == INTENTS[0].size * D(str(value))


@pytest.mark.parametrize("value", [0.24, 1.01, 1.5, 0, -1, math.nan, math.inf, "1", True, None])
def test_size_multiplier_rejects_everything_else(value: object) -> None:
    with pytest.raises(EffectRejectedError):
        size_multiplier(value)


def test_tighten_stop_only_tightens() -> None:
    after = apply_effect(INTENTS[0], EffectKind.TIGHTEN_STOP, {"distance_factor": 0.5})
    assert after.stop_distance == D(250)
    for bad in (1.2, 0.1, "0.5"):
        with pytest.raises(EffectRejectedError):
            apply_effect(INTENTS[0], EffectKind.TIGHTEN_STOP, {"distance_factor": bad})
    # Sin stop no se inventa uno.
    assert apply_effect(INTENTS[2], EffectKind.TIGHTEN_STOP,
                        {"distance_factor": 0.5}) == INTENTS[2]


def test_cooldown_only_extends() -> None:
    after = apply_effect(INTENTS[1], EffectKind.EXTEND_COOLDOWN, {"minutes": 30.5})
    assert after.cooldown_minutes == 91
    for bad in (0, -10):
        with pytest.raises(EffectRejectedError):
            apply_effect(INTENTS[1], EffectKind.EXTEND_COOLDOWN, {"minutes": bad})


def test_veto_and_block() -> None:
    vetoed = apply_effect(INTENTS[0], EffectKind.VETO_ENTRY, {})
    assert not vetoed.allowed and vetoed.size == 0
    blocked = apply_effect(INTENTS[0], EffectKind.BLOCK_COMPONENT, {"component": "signal"})
    assert not blocked.allowed
    assert apply_effect(INTENTS[0], EffectKind.BLOCK_COMPONENT,
                        {"component": "other"}) == INTENTS[0]
    with pytest.raises(EffectRejectedError):
        apply_effect(INTENTS[0], EffectKind.BLOCK_COMPONENT, {"component": 3})


def test_annotations_and_proposals_do_not_touch_risk() -> None:
    tagged = apply_effect(INTENTS[0], EffectKind.ANNOTATE_DECISION, {"tag": "x"})
    assert tagged.tags == ("x",) and replace(tagged, tags=()) == INTENTS[0]
    with pytest.raises(EffectRejectedError):
        apply_effect(INTENTS[0], EffectKind.ANNOTATE_DECISION, {"tag": ""})
    for kind in (EffectKind.OBSERVE_ONLY, EffectKind.PROPOSE_CONFIG_DIFF):
        assert apply_effect(INTENTS[0], kind, {"multiplier": 5}) == INTENTS[0]


@pytest.mark.parametrize(
    "change",
    [
        {"size": D(4)}, {"side": -1}, {"symbol": "ETP-20DEC30-CDE"}, {"strategy_id": "wheel"},
        {"component": "other"}, {"stop_distance": D(600)}, {"stop_distance": None},
        {"cooldown_minutes": 0, "size": D(3)},
    ],
)
def test_risk_not_increased_catches_each_dimension(change: dict[str, object]) -> None:
    before = replace(INTENTS[0], cooldown_minutes=10)
    assert not risk_not_increased(before, replace(before, **change))


def test_reenabling_a_vetoed_intent_is_an_increase() -> None:
    assert not risk_not_increased(INTENTS[3], replace(INTENTS[3], allowed=True))


def test_adding_a_stop_is_not_an_increase() -> None:
    assert risk_not_increased(INTENTS[2], replace(INTENTS[2], stop_distance=D(5)))


@pytest.mark.parametrize(
    "change", [{"side": 0}, {"size": D(-1)}, {"stop_distance": D(0)}, {"cooldown_minutes": -1}]
)
def test_intent_validation(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(INTENTS[0], **change)
