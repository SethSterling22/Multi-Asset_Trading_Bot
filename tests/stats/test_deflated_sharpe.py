"""Deflated Sharpe: contra la tabla del informe y como propiedad bajo el nulo."""

from __future__ import annotations

import math
import random

import pytest

from core.stats.deflated_sharpe import (
    deflated_sharpe,
    expected_max_sharpe,
    moments,
    probabilistic_sharpe,
)

pytestmark = pytest.mark.stats


@pytest.mark.parametrize(
    ("se", "n", "expected"),
    [
        # Informe §2.4 (docs/model.py): Sharpe espurio esperado del máximo de N
        # variantes con alpha cero, en Sharpe anual, con SE(SR) = 1/sqrt(años).
        (1.414, 10, 2.23), (1.414, 100, 3.58), (1.414, 1000, 4.60),
        (1.000, 10, 1.57), (1.000, 100, 2.53), (1.000, 1000, 3.26), (1.000, 10000, 3.86),
        (0.707, 1000, 2.30), (0.447, 100, 1.13),
    ],
)
def test_threshold_reproduces_the_informe_table(se: float, n: int, expected: float) -> None:
    assert expected_max_sharpe(n, se) == pytest.approx(expected, abs=0.01)


def test_one_trial_has_no_search_inflation() -> None:
    assert expected_max_sharpe(1, 0.5) == 0.0


def test_threshold_grows_with_n() -> None:
    values = [expected_max_sharpe(n, 1.0) for n in (2, 10, 100, 1000)]
    assert values == sorted(values)


def test_moments_of_a_known_series() -> None:
    m = moments([1.0, -1.0, 1.0, -1.0])
    assert (m.sharpe, m.skew, m.kurtosis) == (0.0, 0.0, 1.0)


def test_psr_is_one_half_at_the_benchmark() -> None:
    assert probabilistic_sharpe(0.1, 0.1, 500, 0.0, 3.0) == pytest.approx(0.5)


def test_negative_skew_and_fat_tails_lower_the_psr() -> None:
    normal = probabilistic_sharpe(0.1, 0.0, 250, 0.0, 3.0)
    assert probabilistic_sharpe(0.1, 0.0, 250, -1.0, 9.0) < normal


def _null_trial(rng: random.Random, t: int) -> list[float]:
    return [rng.gauss(0.0, 0.01) for _ in range(t)]


def test_best_of_many_null_strategies_is_rarely_significant() -> None:
    """Propiedad: con alpha cero, elegir el mejor de N=20 casi nunca da DSR > 0.95.

    Sin deflactar, el mejor de 20 pasa un test del 5% la mayoría de las veces; con
    el DSR la tasa de falsos positivos se queda cerca del 5% nominal.
    """
    rng = random.Random(1)
    reps, n, t = 300, 20, 250
    false_positives = 0
    naive = 0
    for _ in range(reps):
        trials = [_null_trial(rng, t) for _ in range(n)]
        sharpes = [moments(tr).sharpe for tr in trials]
        best = trials[max(range(n), key=sharpes.__getitem__)]
        if deflated_sharpe(best, sharpes).dsr > 0.95:
            false_positives += 1
        if probabilistic_sharpe(max(sharpes), 0.0, t, 0.0, 3.0) > 0.95:
            naive += 1
    assert false_positives / reps <= 0.08
    assert naive / reps > 0.5


def test_a_real_edge_survives_deflation() -> None:
    rng = random.Random(2)
    trials = [_null_trial(rng, 1000) for _ in range(19)]
    edge = [rng.gauss(0.002, 0.01) for _ in range(1000)]        # SR por período 0.2
    sharpes = [moments(tr).sharpe for tr in [*trials, edge]]
    result = deflated_sharpe(edge, sharpes)
    assert result.dsr > 0.99 and result.n_trials == 20


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda: expected_max_sharpe(0, 1.0), ValueError),
        (lambda: expected_max_sharpe(5, -1.0), ValueError),
        (lambda: probabilistic_sharpe(0.1, 0.0, 1, 0.0, 3.0), ValueError),
        (lambda: probabilistic_sharpe(2.0, 0.0, 100, 5.0, 1.0), ValueError),
        (lambda: moments([0.1, 0.2]), ValueError),
        (lambda: moments([0.1, 0.1, 0.1]), ValueError),
        (lambda: deflated_sharpe([0.1, -0.1, 0.2], []), ValueError),
    ],
)
def test_invalid_inputs(call: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        call()  # type: ignore[operator]


def test_single_trial_dsr_is_plain_psr_against_zero() -> None:
    rets = [0.01, -0.005, 0.02, 0.0, 0.004, -0.01, 0.015]
    m = moments(rets)
    assert deflated_sharpe(rets, [m.sharpe]).dsr == pytest.approx(
        probabilistic_sharpe(m.sharpe, 0.0, m.n_obs, m.skew, m.kurtosis)
    )
    assert math.isfinite(m.sharpe)
