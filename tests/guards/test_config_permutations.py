"""Grupo 7 de CLAUDE.md §1.8: cada permutación inválida de config se rechaza.

Y no se rechaza en silencio: una config inválida deja el gate cerrado hasta que
vuelva a ser válida. No se sigue con la última versión buena.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.config.models import CONFIG_PATH, TradingConfig
from strategies.base import ParamsLoader, Rejected, StrategyBase
from tests.guards.helpers import Events, setup, write_config

pytestmark = pytest.mark.guards

INVALID: list[tuple[str, Any]] = [
    ("wheel_parameters.whitelist_assets", []),
    ("wheel_parameters.whitelist_assets", ["  "]),
    ("wheel_parameters.target_expiration_days_min", 0),
    ("wheel_parameters.target_expiration_days_max", 400),
    ("wheel_parameters.target_expiration_days_min", 50),       # min > max (45)
    ("wheel_parameters.delta_limit_csp", 0.10),                # una CSP tiene delta < 0
    ("wheel_parameters.delta_limit_csp", -1.5),
    ("wheel_parameters.delta_limit_cc", -0.10),
    ("wheel_parameters.delta_limit_cc", 1.5),
    ("wheel_parameters.early_close_percentage_gain", 0.0),
    ("wheel_parameters.early_close_percentage_gain", 1.5),
    ("wheel_parameters.rolling_trigger_percentage_itm", 0.0),
    ("wheel_parameters.rolling_trigger_percentage_itm", 0.6),
    ("wheel_parameters.max_collateral_pct_per_trade", 0.0),
    ("wheel_parameters.max_collateral_pct_per_trade", 0.30),   # > MAX_NOTIONAL_PCT_EQUITY
    ("hedged_spread_parameters.spread_width_dollars", 0.0),
    ("hedged_spread_parameters.sell_delta_put", 0.10),
    ("hedged_spread_parameters.buy_delta_coverage_put", -1.5),
    ("hedged_spread_parameters.buy_delta_coverage_put", -0.50),  # más ITM que la vendida
    ("crypto_parameters.stable_target_allocation", 0.7),       # no suma 1.0
    ("crypto_parameters.stable_target_allocation", -0.1),
    ("crypto_parameters.rebalance_threshold", 0.0),
    ("crypto_parameters.rebalance_threshold", 0.30),
    ("crypto_parameters.monitored_assets", []),
    ("risk_guards.max_portfolio_risk_per_trade", 0.0),
    ("risk_guards.max_portfolio_risk_per_trade", 0.20),
    ("risk_guards.daily_loss_limit_percentage", 0.0),
    ("risk_guards.daily_loss_limit_percentage", 0.05),         # > DAILY_LOSS_STOP_PCT
]


def _data(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CONFIG_PATH.read_text())
    for dotted, value in changes.items():
        section, name = dotted.split(".")
        data[section][name] = value
    return data


def test_shipped_config_is_valid() -> None:
    TradingConfig.load(CONFIG_PATH)


@pytest.mark.parametrize(("field", "value"), INVALID)
def test_invalid_value_is_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        TradingConfig.model_validate(_data(**{field: value}))


@pytest.mark.parametrize("section", list(_data().keys()))
def test_missing_section_is_rejected(section: str) -> None:
    data = _data()
    del data[section]
    with pytest.raises(ValidationError):
        TradingConfig.model_validate(data)


class Sleeve(StrategyBase):
    strategy_id = "trend"


@pytest.mark.parametrize(("field", "value"), INVALID[:3] + INVALID[-2:])
def test_invalid_config_closes_the_gate(tmp_path: Path, field: str, value: Any) -> None:
    events = Events()
    s = setup(Sleeve(), write_config(tmp_path, **{field: value}))
    assert not s.trading_gate(equity=D(100_000)).ok
    assert [e["reason"] for e in events.rejections()] == ["params_invalid"]


def test_hot_reload_to_invalid_does_not_keep_the_old_config(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    loader = ParamsLoader(path)
    assert isinstance(loader.current(), TradingConfig)
    path.write_text(json.dumps(_data(**{"crypto_parameters.rebalance_threshold": 0.9})))
    os.utime(path, (1, 1))                        # mtime distinto
    assert isinstance(loader.current(), Rejected)
    path.write_text(json.dumps(_data()))
    os.utime(path, (2, 2))
    assert isinstance(loader.current(), TradingConfig)


def test_unreadable_config(tmp_path: Path) -> None:
    missing = ParamsLoader(tmp_path / "nope.json").current()
    assert isinstance(missing, Rejected) and missing.code == "params_unavailable"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert isinstance(ParamsLoader(bad).current(), Rejected)


def test_inactive_system_closes_the_gate(tmp_path: Path) -> None:
    events = Events()
    s = setup(Sleeve(), write_config(tmp_path, **{"system_status.active": False}))
    assert not s.trading_gate(equity=D(100_000)).ok
    assert [e["reason"] for e in events.rejections()] == ["inactive"]
