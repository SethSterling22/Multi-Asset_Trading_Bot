"""Probability of Backtest Overfitting por CSCV (Bailey, Borwein, López de Prado
& Zhu, 2017).

Se parte la matriz de retornos (T filas × N configuraciones) en S bloques
contiguos. Para cada combinación de S/2 bloques como in-sample (y el resto como
out-of-sample): se elige la configuración con mejor Sharpe in-sample y se mira
en qué posición queda out-of-sample. Si queda por debajo de la mediana, esa
combinación cuenta como sobreajuste. PBO es la fracción de combinaciones así.

PBO ≈ 0.5: elegir el mejor in-sample no dice nada del out-of-sample (ruido).
PBO ≈ 0: el mejor in-sample sigue siéndolo fuera. El gate de la Fase 2 pide < 0.5.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations, pairwise
from statistics import median

DEFAULT_BLOCKS = 16


@dataclass(frozen=True, slots=True)
class PBO:
    pbo: float
    n_trials: int
    n_blocks: int
    n_combinations: int
    median_logit: float


def _sharpe(s: float, s2: float, n: int) -> float:
    mean = s / n
    var = max(s2 / n - mean * mean, 0.0)
    if var <= 1e-24:
        return 0.0
    return mean / math.sqrt(var)


def _relative_rank(values: Sequence[float], index: int) -> float:
    """Rango (1..N, empates promediados) de values[index], dividido por N + 1."""
    v = values[index]
    below = sum(1 for x in values if x < v)
    ties = sum(1 for x in values if x == v)
    return (below + (ties + 1) / 2) / (len(values) + 1)


def probability_of_backtest_overfitting(
    trial_returns: Sequence[Sequence[float]], n_blocks: int = DEFAULT_BLOCKS
) -> PBO:
    n = len(trial_returns)
    if n < 2:
        raise ValueError("PBO necesita al menos 2 configuraciones")
    t = len(trial_returns[0])
    if any(len(r) != t for r in trial_returns):
        raise ValueError("todas las configuraciones deben tener la misma serie temporal")
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("n_blocks debe ser par y >= 2")
    if t < 2 * n_blocks:
        raise ValueError(f"{t} observaciones no llegan para {n_blocks} bloques")

    edges = [round(i * t / n_blocks) for i in range(n_blocks + 1)]
    # Por configuración y bloque: (suma, suma de cuadrados, n).
    stats = [
        [(sum(r[a:b]), sum(x * x for x in r[a:b]), b - a) for a, b in pairwise(edges)]
        for r in trial_returns
    ]
    logits: list[float] = []
    overfit = 0
    for in_sample in combinations(range(n_blocks), n_blocks // 2):
        chosen = set(in_sample)
        is_sr, oos_sr = [], []
        for per_block in stats:
            s_in = s2_in = n_in = 0.0
            s_out = s2_out = n_out = 0.0
            for b, (s, s2, k) in enumerate(per_block):
                if b in chosen:
                    s_in, s2_in, n_in = s_in + s, s2_in + s2, n_in + k
                else:
                    s_out, s2_out, n_out = s_out + s, s2_out + s2, n_out + k
            is_sr.append(_sharpe(s_in, s2_in, int(n_in)))
            oos_sr.append(_sharpe(s_out, s2_out, int(n_out)))
        best = max(range(n), key=is_sr.__getitem__)
        w = _relative_rank(oos_sr, best)
        logit = math.log(w / (1 - w))
        logits.append(logit)
        if logit <= 0:
            overfit += 1
    return PBO(
        pbo=overfit / len(logits),
        n_trials=n,
        n_blocks=n_blocks,
        n_combinations=len(logits),
        median_logit=median(logits),
    )
