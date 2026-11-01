"""Grupo 4 de CLAUDE.md §1.8: kill switch triple redundante y fail-closed (§1.7)."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from risk.breakers import KillSwitch, file_source, redis_source, risk_events_source
from strategies.base import StrategyBase
from tests.guards.helpers import Events, Switches, setup, write_config

pytestmark = pytest.mark.guards

SOURCES = ("risk_events", "redis", "halt_file")


class Sleeve(StrategyBase):
    strategy_id = "trend"


def _switch(sw: Switches) -> KillSwitch:
    return KillSwitch(risk_events=sw.source("risk_events"), redis=sw.source("redis"),
                      halt_file=sw.source("halt_file"))


def test_all_clear_allows() -> None:
    assert _switch(Switches()).check("trend").ok


@pytest.mark.parametrize("name", SOURCES)
def test_any_single_source_activates(name: str) -> None:
    v = _switch(Switches(**{name: True})).check("trend")
    assert not v.ok and v.code == "kill_switch" and name in v.detail


@pytest.mark.parametrize("name", SOURCES)
def test_error_checking_a_source_counts_as_active(name: str) -> None:
    v = _switch(Switches(**{name: ConnectionError("redis caído")})).check("trend")
    assert not v.ok and "error al comprobar" in v.detail


def test_extra_sources_add_but_never_replace() -> None:
    ks = KillSwitch(risk_events=lambda _: False, redis=lambda _: False,
                    halt_file=lambda _: False, extra={"config.json": lambda _: True})
    assert not ks.check("trend").ok
    # Una fuente extra con el nombre de una de las tres no la sustituye en silencio:
    # el diccionario se construye con las tres primero y `extra` solo puede activar.
    ks2 = KillSwitch(risk_events=lambda _: True, redis=lambda _: False,
                     halt_file=lambda _: False, extra={"other": lambda _: False})
    assert not ks2.check("trend").ok


def test_the_three_sources_are_mandatory() -> None:
    with pytest.raises(TypeError):
        KillSwitch(risk_events=lambda _: False, redis=lambda _: False)  # type: ignore[call-arg]


def test_halt_file_global_and_per_strategy(tmp_path: Path) -> None:
    halt = tmp_path / "HALT"
    check = file_source(halt)
    assert check("trend") is False
    (tmp_path / "HALT.wheel").touch()
    assert check("wheel") is True and check("trend") is False
    halt.touch()
    assert check("trend") is True


def test_halt_file_missing_directory_is_not_active(tmp_path: Path) -> None:
    assert file_source(tmp_path / "no" / "such" / "HALT")("trend") is False


class FakeRedis:
    def __init__(self, keys: set[str]) -> None:
        self.keys = keys

    def exists(self, *names: str) -> int:
        return sum(1 for n in names if n in self.keys)


def test_redis_source_global_and_per_strategy() -> None:
    assert redis_source(FakeRedis(set()))("trend") is False
    assert redis_source(FakeRedis({"trading:halt:trend"}))("trend") is True
    assert redis_source(FakeRedis({"trading:halt:trend"}))("wheel") is False
    assert redis_source(FakeRedis({"trading:halt"}))("wheel") is True


class FakeEngine:
    """Lo mínimo de un Engine de SQLAlchemy: connect() -> execute() -> all()."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.params: dict[str, Any] = {}

    def connect(self) -> FakeEngine:
        return self

    def __enter__(self) -> FakeEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: Any, params: dict[str, Any]) -> FakeEngine:
        self.params = params
        return self

    def all(self) -> list[Any]:
        class Row:
            def __init__(self, scope: str, kind: str) -> None:
                self.scope, self.kind = scope, kind
        return [Row(s, k) for s, k in self.rows]


@pytest.mark.parametrize(
    ("rows", "active"),
    [
        ([], False),
        ([("global", "kill_switch_off")], False),
        ([("global", "kill_switch_on")], True),
        ([("global", "kill_switch_off"), ("trend", "kill_switch_on")], True),
    ],
)
def test_risk_events_source(rows: list[tuple[str, str]], active: bool) -> None:
    engine = FakeEngine(rows)
    assert risk_events_source(engine)("trend") is active  # type: ignore[arg-type]
    assert engine.params == {"strategy_id": "trend"}


def test_config_flag_is_an_extra_source(tmp_path: Path) -> None:
    events = Events()
    cfg = write_config(tmp_path, **{"system_status.emergency_kill_switch": True})
    s = setup(Sleeve(), cfg)
    assert not s.trading_gate(equity=D(100_000)).ok
    (rej,) = events.rejections()
    assert rej["reason"] == "kill_switch" and "config.json" in rej["detail"]


@pytest.mark.parametrize("name", SOURCES)
def test_gate_blocks_when_any_source_is_active(tmp_path: Path, name: str) -> None:
    s = setup(Sleeve(), write_config(tmp_path), switches=Switches(**{name: True}))
    assert not s.trading_gate(equity=D(100_000)).ok


def test_invalid_config_counts_as_kill_switch_active(tmp_path: Path) -> None:
    # Otro consumidor del kill switch (risk-monitor) lo evalúa sin pasar por el gate:
    # un config.json ilegible tiene que contar como activo, no como "flag a false".
    from strategies.base import ParamsLoader, config_kill_source
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    ks = KillSwitch(risk_events=lambda _: False, redis=lambda _: False,
                    halt_file=lambda _: False,
                    extra={"config.json": config_kill_source(ParamsLoader(bad))})
    v = ks.check("trend")
    assert not v.ok and "config.json: error al comprobar" in v.detail
