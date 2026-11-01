"""Resultado de un guard: o se permite, o se rechaza con un código y un motivo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class Allowed:
    ok: Literal[True] = True


@dataclass(frozen=True, slots=True)
class Rejected:
    code: str       # estable, para filtrar: 'kill_switch', 'daily_loss', 'no_cash'…
    detail: str     # legible, con los números que llevaron al rechazo
    ok: Literal[False] = False


Verdict = Allowed | Rejected
ALLOWED: Final = Allowed()


def first_rejection(*verdicts: Verdict) -> Verdict:
    """El primer rechazo, o ALLOWED si no hay ninguno."""
    for v in verdicts:
        if isinstance(v, Rejected):
            return v
    return ALLOWED
