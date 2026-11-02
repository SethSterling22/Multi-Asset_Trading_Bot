"""PBO por CSCV: propiedades bajo el nulo y con un edge real."""

from __future__ import annotations

import random

import pytest

from backtest.cscv import probability_of_backtest_overfitting

pytestmark = pytest.mark.stats


def _noise(rng: random.Random, t: int) -> list[float]:
    return [rng.gauss(0.0, 0.01) for _ in range(t)]


def test_pure_noise_gives_pbo_around_one_half() -> None:
    # Sin edge, el mejor in-sample cae fuera de muestra en cualquier sitio: PBO ~ 0.5.
    rng = random.Random(3)
    values = [probability_of_backtest_overfitting([_noise(rng, 320) for _ in range(10)],
                                                  n_blocks=10).pbo for _ in range(20)]
    mean = sum(values) / len(values)
    assert 0.3 <= mean <= 0.7


def test_a_real_edge_gives_low_pbo() -> None:
    rng = random.Random(4)
    trials = [_noise(rng, 640) for _ in range(9)]
    trials.append([rng.gauss(0.003, 0.01) for _ in range(640)])
    result = probability_of_backtest_overfitting(trials)
    assert result.pbo < 0.1
    assert result.n_combinations == 12_870          # C(16, 8)
    assert result.median_logit > 0


def test_inputs_are_validated() -> None:
    rng = random.Random(5)
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([_noise(rng, 100)])
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([_noise(rng, 100), _noise(rng, 99)])
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([_noise(rng, 100)] * 2, n_blocks=7)
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([_noise(rng, 20)] * 2, n_blocks=16)
