"""core/greeks.py contra valores de libro (Hull, ejemplo 15.6) y propiedades."""

from __future__ import annotations

import math

import pytest

from core.greeks import (
    SIGMA_MIN,
    DeltaSource,
    NoDelta,
    Right,
    bs_delta,
    bs_price,
    implied_vol,
    plausible_delta_band,
    resolve_delta,
)

# S=100, K=100, T=1, r=5%, sigma=20%: call 10.4506, put 5.5735, delta call 0.6368.


def test_textbook_values() -> None:
    assert bs_price(Right.CALL, 100, 100, 1, 0.05, 0.2) == pytest.approx(10.4506, abs=1e-4)
    assert bs_price(Right.PUT, 100, 100, 1, 0.05, 0.2) == pytest.approx(5.5735, abs=1e-4)
    assert bs_delta(Right.CALL, 100, 100, 1, 0.05, 0.2) == pytest.approx(0.6368, abs=1e-4)
    assert bs_delta(Right.PUT, 100, 100, 1, 0.05, 0.2) == pytest.approx(0.6368 - 1, abs=1e-4)


def test_put_call_parity() -> None:
    c = bs_price(Right.CALL, 105, 100, 0.5, 0.04, 0.3)
    p = bs_price(Right.PUT, 105, 100, 0.5, 0.04, 0.3)
    assert c - p == pytest.approx(105 - 100 * math.exp(-0.04 * 0.5))


@pytest.mark.parametrize("sigma", [0.2, 0.6, 2.0])
@pytest.mark.parametrize("strike", [80, 100, 120])
def test_implied_vol_round_trip(sigma: float, strike: float) -> None:
    for right in Right:
        price = bs_price(right, 100, strike, 0.25, 0.03, sigma)
        assert implied_vol(right, price, 100, strike, 0.25, 0.03) == pytest.approx(sigma, rel=1e-6)


@pytest.mark.parametrize("strike", [80, 100, 120])
def test_implied_vol_reprices_even_when_vega_vanishes(strike: float) -> None:
    # A sigma 5% y 3 meses, las opciones lejos del dinero valen su intrínseco (o 0):
    # sigma no es identificable. La propiedad que sí vale: o no hay IV (el precio no
    # supera el de SIGMA_MIN), o la IV reproduce el precio.
    for right in Right:
        price = bs_price(right, 100, strike, 0.25, 0.03, 0.05)
        iv = implied_vol(right, price, 100, strike, 0.25, 0.03)
        if iv is None:
            assert price <= bs_price(right, 100, strike, 0.25, 0.03, SIGMA_MIN)
        else:
            assert bs_price(right, 100, strike, 0.25, 0.03, iv) == pytest.approx(price, abs=1e-9)


def test_price_outside_bounds_has_no_iv() -> None:
    assert implied_vol(Right.CALL, 150.0, 100, 100, 1, 0.05) is None   # > spot
    assert implied_vol(Right.PUT, 0.0, 100, 50, 1, 0.05) is None


def test_band_contains_every_bs_delta() -> None:
    low, high = plausible_delta_band(Right.PUT, 100, 90, 35 / 365, 0.045)
    for sigma in (0.05, 0.3, 1.0, 4.0):
        assert low <= bs_delta(Right.PUT, 100, 90, 35 / 365, 0.045, sigma) <= high
    assert -1 <= low < high <= 0


def test_resolution_order() -> None:
    kw = {"spot": 100.0, "strike": 90.0, "t_years": 35 / 365, "risk_free_rate": 0.045}
    broker = resolve_delta(Right.PUT, broker_delta=-0.15, option_price=0.6, **kw)
    assert broker.source is DeltaSource.BROKER  # type: ignore[union-attr]
    bs = resolve_delta(Right.PUT, broker_delta=None, option_price=0.6, **kw)
    assert bs.source is DeltaSource.BLACK_SCHOLES and bs.implied_vol  # type: ignore[union-attr]
    nan = resolve_delta(Right.PUT, broker_delta=math.nan, option_price=0.6, **kw)
    assert nan.source is DeltaSource.BLACK_SCHOLES  # type: ignore[union-attr]
    none = resolve_delta(Right.PUT, broker_delta=None, option_price=None, **kw)
    assert isinstance(none, NoDelta) and none.reason == "no_greeks"
    bad_price = resolve_delta(Right.PUT, broker_delta=None, option_price=50.0, **kw)
    assert isinstance(bad_price, NoDelta) and bad_price.reason == "no_greeks"


@pytest.mark.parametrize(("spot", "strike", "t"), [(None, 90, 0.1), (0, 90, 0.1), (100, 0, 0.1),
                                                   (100, 90, 0)])
def test_invalid_inputs(spot: float | None, strike: float, t: float) -> None:
    r = resolve_delta(Right.CALL, spot=spot, strike=strike, t_years=t, risk_free_rate=0.04,
                      broker_delta=0.3, option_price=1.0)
    assert isinstance(r, NoDelta) and r.reason == "invalid_inputs"
