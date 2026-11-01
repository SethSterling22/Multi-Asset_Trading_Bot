"""Delta de una opción, con orden de precedencia estricto (informe §10, Fase 0.3).

1. La delta del broker, si viene y es plausible para la moneyness del contrato.
2. Black-Scholes con la volatilidad implícita del precio de mercado de la opción,
   spot, strike, tiempo a vencimiento y tasa libre de riesgo.
3. Si ninguna está disponible: NO hay delta. El trade se salta y se registra con
   reason='no_greeks'. Nunca se adivina: el proxy de moneyness que había en el
   Wheel no era una delta y ya no existe.

Banda de sanidad: una delta solo es plausible si Black-Scholes puede producirla
para esa moneyness con ALGUNA volatilidad entre SIGMA_MIN y SIGMA_MAX. La banda
se deriva barriendo la volatilidad, no se escribe a mano. Modelo europeo sin
dividendos: DELTA_BAND_TOLERANCE cubre el desvío que introducen los dividendos y
el ejercicio anticipado de una opción americana.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

# Límites físicos de la volatilidad anual que se consideran posibles: 1% y 500%.
SIGMA_MIN = 0.01
SIGMA_MAX = 5.0
DELTA_BAND_TOLERANCE = 0.02
_SIGMA_GRID = [SIGMA_MIN * (SIGMA_MAX / SIGMA_MIN) ** (i / 400) for i in range(401)]
_IV_TOLERANCE = 1e-10
_IV_MAX_ITERATIONS = 200


class Right(StrEnum):
    CALL = "call"
    PUT = "put"


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(spot: float, strike: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / vol_t
    return d1, d1 - vol_t


def bs_price(right: Right, spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    d1, d2 = _d1_d2(spot, strike, t, r, sigma)
    discounted = strike * math.exp(-r * t)
    if right is Right.CALL:
        return spot * _norm_cdf(d1) - discounted * _norm_cdf(d2)
    return discounted * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(right: Right, spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    d1, _ = _d1_d2(spot, strike, t, r, sigma)
    return _norm_cdf(d1) if right is Right.CALL else _norm_cdf(d1) - 1.0


def implied_vol(
    right: Right, price: float, spot: float, strike: float, t: float, r: float
) -> float | None:
    """Volatilidad implícita por bisección, o None si el precio no admite ninguna.

    El precio de Black-Scholes es creciente en sigma, así que la bisección converge
    si el precio cae entre los de SIGMA_MIN y SIGMA_MAX. La cota inferior es
    estricta: un precio igual al de SIGMA_MIN (el valor intrínseco, o 0 en una
    opción muy OTM) no dice nada de la volatilidad, y devolver SIGMA_MIN sería
    inventarse una IV.
    """
    low, high = SIGMA_MIN, SIGMA_MAX
    if not bs_price(right, spot, strike, t, r, low) < price <= bs_price(
        right, spot, strike, t, r, high
    ):
        return None
    for _ in range(_IV_MAX_ITERATIONS):
        mid = 0.5 * (low + high)
        if bs_price(right, spot, strike, t, r, mid) < price:
            low = mid
        else:
            high = mid
        if high - low < _IV_TOLERANCE:
            break
    return 0.5 * (low + high)


def plausible_delta_band(
    right: Right, spot: float, strike: float, t: float, r: float
) -> tuple[float, float]:
    """Rango de deltas que Black-Scholes admite para esta moneyness, con tolerancia."""
    deltas = [bs_delta(right, spot, strike, t, r, s) for s in _SIGMA_GRID]
    lower, upper = (-1.0, 0.0) if right is Right.PUT else (0.0, 1.0)
    return (
        max(lower, min(deltas) - DELTA_BAND_TOLERANCE),
        min(upper, max(deltas) + DELTA_BAND_TOLERANCE),
    )


class DeltaSource(StrEnum):
    BROKER = "broker"
    BLACK_SCHOLES = "black_scholes"


@dataclass(frozen=True, slots=True)
class Delta:
    value: float
    source: DeltaSource
    implied_vol: float | None = None


@dataclass(frozen=True, slots=True)
class NoDelta:
    """No hay delta fiable: el contrato no se puede seleccionar."""

    reason: str          # 'no_greeks' | 'delta_outside_band' | 'invalid_inputs'
    detail: str


def resolve_delta(
    right: Right,
    *,
    spot: float | None,
    strike: float,
    t_years: float,
    risk_free_rate: float | None,
    broker_delta: float | None,
    option_price: float | None,
) -> Delta | NoDelta:
    if spot is None or spot <= 0 or strike <= 0 or t_years <= 0:
        return NoDelta("invalid_inputs", f"spot={spot} strike={strike} t={t_years}")
    rate = risk_free_rate if risk_free_rate is not None else math.nan
    band_rate = 0.0 if math.isnan(rate) else rate
    low, high = plausible_delta_band(right, spot, strike, t_years, band_rate)

    if broker_delta is not None and math.isfinite(broker_delta):
        if not low <= broker_delta <= high:
            return NoDelta(
                "delta_outside_band",
                f"broker delta {broker_delta:.4f} fuera de [{low:.4f}, {high:.4f}]",
            )
        return Delta(broker_delta, DeltaSource.BROKER)

    if option_price is None or option_price <= 0 or math.isnan(rate):
        return NoDelta(
            "no_greeks",
            f"sin delta del broker y sin IV: price={option_price} rate={risk_free_rate}",
        )
    iv = implied_vol(right, option_price, spot, strike, t_years, rate)
    if iv is None:
        return NoDelta("no_greeks", f"el precio {option_price} no admite una IV en el rango")
    value = bs_delta(right, spot, strike, t_years, rate, iv)
    return Delta(value, DeltaSource.BLACK_SCHOLES, implied_vol=iv)
