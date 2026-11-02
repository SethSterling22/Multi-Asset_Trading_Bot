"""Registro de N: cada configuración evaluada, incluidas las descartadas.

Sin N el informe no se genera (CLAUDE.md §1.11). El motor registra cada prueba
al terminarla; la misma configuración (mismo hash de parámetros + ventana +
supuestos de coste) no cuenta dos veces, y cualquier cambio sí.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Engine, text

KIND = "backtest_config"


def params_hash(params: Mapping[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class TrialRecord:
    params_hash: str
    sharpe: float | None      # por período; None si la prueba se descartó sin Sharpe
    discarded: bool


class SearchLog(Protocol):
    def record(self, strategy_id: str, params: Mapping[str, Any],
               outcome: Mapping[str, Any] | None, discarded: bool) -> str: ...
    def trials(self, strategy_id: str) -> list[TrialRecord]: ...


class MemorySearchLog:
    """Para tests. No persiste: el N de una sesión no sobrevive a la siguiente."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], TrialRecord] = {}

    def record(self, strategy_id: str, params: Mapping[str, Any],
               outcome: Mapping[str, Any] | None, discarded: bool) -> str:
        h = params_hash(params)
        sharpe = None if outcome is None else outcome.get("sharpe_per_period")
        self._rows.setdefault((strategy_id, h), TrialRecord(h, sharpe, discarded))
        return h

    def trials(self, strategy_id: str) -> list[TrialRecord]:
        return [r for (sid, _), r in self._rows.items() if sid == strategy_id]


class PostgresSearchLog:
    """`search_log` en Postgres: append-only, y N sobrevive entre sesiones."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, strategy_id: str, params: Mapping[str, Any],
               outcome: Mapping[str, Any] | None, discarded: bool) -> str:
        h = params_hash(params)
        with self._engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO search_log (kind, strategy_id, params_hash, params, outcome,
                                            discarded)
                    VALUES (:kind, :strategy_id, decode(:h, 'hex'), CAST(:params AS jsonb),
                            CAST(:outcome AS jsonb), :discarded)
                    ON CONFLICT (kind, strategy_id, params_hash) DO NOTHING
                """),
                {"kind": KIND, "strategy_id": strategy_id, "h": h,
                 "params": json.dumps(params, sort_keys=True, default=str),
                 "outcome": None if outcome is None else json.dumps(outcome, default=str),
                 "discarded": discarded},
            )
        return h

    def trials(self, strategy_id: str) -> list[TrialRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT encode(params_hash, 'hex') AS h,
                           (outcome->>'sharpe_per_period')::float AS sharpe, discarded
                    FROM search_log WHERE kind = :kind AND strategy_id = :strategy_id
                """),
                {"kind": KIND, "strategy_id": strategy_id},
            )
            return [TrialRecord(r.h, r.sharpe, r.discarded) for r in rows]
