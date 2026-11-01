"""Guard: assert_no_mutation() detecta cualquier constante del risk floor parcheada en runtime.

Este guard tuvo un bug de NameError que lo hacía inútil en silencio: el exec del
fichero fallaba antes de comparar nada. Por eso se comprueba que lanza
RuntimeError (no cualquier excepción) para CADA constante, no solo para una.
"""

from __future__ import annotations

import pytest

from core import risk_floor

pytestmark = pytest.mark.guards

CONSTANTS = sorted(
    name for name in vars(risk_floor) if name.isupper() and not name.startswith("_")
)


def _different(value: object) -> object:
    """Un valor del mismo tipo que difiere del original."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, frozenset):
        return value | {"__MUTATED__"}
    if isinstance(value, dict):
        return {**value, "__MUTATED__": 0}
    raise TypeError(f"Tipo sin mutación definida en el test: {type(value).__name__}")


def test_constants_are_discovered() -> None:
    # Si el descubrimiento devolviese una lista vacía, el test parametrizado de
    # abajo pasaría sin comprobar nada.
    assert "MAX_LEVERAGE_MAJORS" in CONSTANTS
    assert "LLM_SIZE_MULTIPLIER_MAX" in CONSTANTS


def test_intact_floor_passes() -> None:
    risk_floor.assert_no_mutation()


@pytest.mark.parametrize("name", CONSTANTS)
def test_monkeypatched_constant_raises(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(risk_floor, name, _different(getattr(risk_floor, name)))
    with pytest.raises(RuntimeError, match=name):
        risk_floor.assert_no_mutation()


def test_in_place_dict_mutation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # El único contenedor mutable del floor: un test descuidado puede editarlo
    # sin reasignar el nombre.
    monkeypatch.setitem(risk_floor.MAX_ROUNDTRIPS_PER_YEAR, "xsec_momentum", 999)
    with pytest.raises(RuntimeError, match="MAX_ROUNDTRIPS_PER_YEAR"):
        risk_floor.assert_no_mutation()


def test_floor_hash_is_sha256() -> None:
    digest = risk_floor.floor_hash()
    assert len(digest) == 64
    assert digest == risk_floor.floor_hash()
