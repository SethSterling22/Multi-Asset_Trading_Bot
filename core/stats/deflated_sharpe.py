"""Deflated Sharpe Ratio (Bailey & López de Prado, 2014).

El mejor Sharpe de N configuraciones probadas está inflado por la búsqueda: con
alpha verdadero cero, el máximo esperado crece con N (informe §2.4). El DSR es
la probabilidad de que el Sharpe verdadero supere ese máximo espurio, corregida
por la asimetría y la cola de los retornos y por la longitud de la muestra.

    SR0 = sqrt(V[SR_n]) · ((1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)))
    DSR = Φ( (SR − SR0)·sqrt(T − 1) / sqrt(1 − γ3·SR + (γ4 − 1)/4·SR²) )

con γ la constante de Euler-Mascheroni, V[SR_n] la varianza de los Sharpe de
las N pruebas, γ3 la asimetría y γ4 la curtosis (3 en la normal). Todo en Sharpe
POR PERÍODO, sin anualizar. Sin N no hay DSR: por eso N se registra siempre.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist, fmean, pvariance

EULER_GAMMA = 0.5772156649015329
_PHI = NormalDist()


def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """Sharpe máximo esperado de N pruebas con alpha cero (aproximación de Gumbel).

    Es el SR0 contra el que se compara el mejor resultado. Con N = 1 no hay
    búsqueda y el umbral es 0.
    """
    if n_trials < 1:
        raise ValueError("n_trials >= 1")
    if sharpe_std < 0:
        raise ValueError("sharpe_std >= 0")
    if n_trials == 1:
        return 0.0
    z1 = _PHI.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _PHI.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return sharpe_std * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def probabilistic_sharpe(
    sharpe: float, benchmark: float, n_obs: int, skew: float, kurtosis: float
) -> float:
    """P(SR verdadero > benchmark) dados SR, T, asimetría y curtosis (no excesiva)."""
    if n_obs < 2:
        raise ValueError("n_obs >= 2")
    variance_term = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe
    if variance_term <= 0:
        raise ValueError("momentos incoherentes: el denominador del PSR no es positivo")
    z = (sharpe - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(variance_term)
    return _PHI.cdf(z)


@dataclass(frozen=True, slots=True)
class Moments:
    n_obs: int
    sharpe: float      # por período
    skew: float
    kurtosis: float    # no excesiva: 3 en la normal


def moments(returns: Sequence[float]) -> Moments:
    n = len(returns)
    if n < 3:
        raise ValueError("hacen falta al menos 3 retornos")
    # Una serie constante en float puede dar varianza ~1e-34 en vez de 0, y un
    # Sharpe enorme sin sentido: se detecta directamente.
    if max(returns) == min(returns):
        raise ValueError("retornos sin varianza: el Sharpe no está definido")
    mean = fmean(returns)
    var = pvariance(returns, mean)
    sd = math.sqrt(var)
    skew = fmean([((r - mean) / sd) ** 3 for r in returns])
    kurt = fmean([((r - mean) / sd) ** 4 for r in returns])
    return Moments(n_obs=n, sharpe=mean / sd, skew=skew, kurtosis=kurt)


@dataclass(frozen=True, slots=True)
class DeflatedSharpe:
    dsr: float              # probabilidad en [0, 1]
    sharpe: float           # por período, del candidato
    threshold: float        # SR0: máximo esperado por azar dados N y V[SR]
    n_trials: int
    n_obs: int


def deflated_sharpe(
    returns: Sequence[float], trial_sharpes: Sequence[float], *, n_trials: int | None = None
) -> DeflatedSharpe:
    """DSR del candidato `returns`, deflactado por TODAS las pruebas registradas.

    `trial_sharpes` son los Sharpe por período de las configuraciones evaluadas
    que tienen Sharpe (incluido el candidato). `n_trials` es N, que además cuenta
    las descartadas sin Sharpe; por defecto, len(trial_sharpes).
    """
    n_trials = len(trial_sharpes) if n_trials is None else n_trials
    if not trial_sharpes or n_trials < 1:
        raise ValueError("sin pruebas registradas no hay DSR: N es obligatorio")
    if n_trials < len(trial_sharpes):
        raise ValueError("n_trials no puede ser menor que el número de Sharpe registrados")
    m = moments(returns)
    sharpe_std = math.sqrt(pvariance(trial_sharpes)) if n_trials > 1 else 0.0
    threshold = expected_max_sharpe(n_trials, sharpe_std)
    dsr = probabilistic_sharpe(m.sharpe, threshold, m.n_obs, m.skew, m.kurtosis)
    return DeflatedSharpe(dsr=dsr, sharpe=m.sharpe, threshold=threshold,
                          n_trials=n_trials, n_obs=m.n_obs)
