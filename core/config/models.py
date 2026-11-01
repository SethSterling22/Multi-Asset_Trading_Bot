"""Pydantic v2 models for validating and persisting config.json.

All modules (strategies/wheel, strategies/crypto_rebalance, app_dashboard)
read and write configuration exclusively through these classes, guaranteeing
that invalid parameters (out-of-range deltas, allocations that don't sum
to 1.0, etc.) are never persisted.

Strategy parameters may only be TIGHTER than core/risk_floor.py, never looser:
the bounds below import the floor constants instead of repeating the numbers.
The kill switch in `system_status` is one of the sources of risk/breakers.py,
not the only one (CLAUDE.md §1.7).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from core.risk_floor import DAILY_LOSS_STOP_PCT, MAX_NOTIONAL_PCT_EQUITY

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


def _clean_symbols(v: list[str]) -> list[str]:
    # min_length se comprueba ANTES de este validador: ["  "] lo pasaba y quedaba
    # una lista vacía (división por cero en el rebalanceador). Se comprueba aquí.
    cleaned = [s.strip().upper() for s in v if s.strip()]
    if not cleaned:
        raise ValueError("at least one non-blank symbol is required")
    return cleaned


class SystemStatus(BaseModel):
    active: bool = True
    emergency_kill_switch: bool = False
    live_trading_mode: bool = False  # False => paper trading enforced


class WheelParameters(BaseModel):
    whitelist_assets: list[str] = Field(min_length=1)
    target_expiration_days_min: int = Field(ge=1, le=365)
    target_expiration_days_max: int = Field(ge=1, le=365)
    # CSP delta: negative by convention (short OTM put).
    delta_limit_csp: float = Field(lt=0.0, ge=-1.0)
    # Covered Call delta: positive (short OTM call).
    delta_limit_cc: float = Field(gt=0.0, le=1.0)
    early_close_percentage_gain: float = Field(gt=0.0, le=1.0)
    rolling_trigger_percentage_itm: float = Field(gt=0.0, le=0.50)
    # Collateral of one CSP (strike × 100) as a fraction of portfolio value.
    # 0.20 reproduces the old `max(limit * 10, limit)` check with the 0.02 in
    # config.json, without the hidden ×10 (DECISIONS.md D-006).
    max_collateral_pct_per_trade: float = Field(
        default=0.20, gt=0.0, le=MAX_NOTIONAL_PCT_EQUITY
    )

    @field_validator("whitelist_assets")
    @classmethod
    def _upper_symbols(cls, v: list[str]) -> list[str]:
        return _clean_symbols(v)

    @model_validator(mode="after")
    def _check_expiration_window(self) -> WheelParameters:
        if self.target_expiration_days_min > self.target_expiration_days_max:
            raise ValueError("target_expiration_days_min > target_expiration_days_max")
        return self


class HedgedSpreadParameters(BaseModel):
    spread_width_dollars: float = Field(gt=0.0)
    sell_delta_put: float = Field(lt=0.0, ge=-1.0)
    buy_delta_coverage_put: float = Field(lt=0.0, ge=-1.0)

    @model_validator(mode="after")
    def _coverage_further_otm(self) -> HedgedSpreadParameters:
        # The long (protective) put must be further OTM (less negative delta)
        # than the short put.
        if self.buy_delta_coverage_put <= self.sell_delta_put:
            raise ValueError(
                "buy_delta_coverage_put must be less negative than sell_delta_put"
            )
        return self


class CryptoParameters(BaseModel):
    stable_target_allocation: float = Field(ge=0.0, le=1.0)
    crypto_target_allocation: float = Field(ge=0.0, le=1.0)
    rebalance_threshold: float = Field(gt=0.0, le=0.25)
    monitored_assets: list[str] = Field(min_length=1)

    @field_validator("monitored_assets")
    @classmethod
    def _upper_symbols(cls, v: list[str]) -> list[str]:
        return _clean_symbols(v)

    @model_validator(mode="after")
    def _allocations_sum_to_one(self) -> CryptoParameters:
        total = self.stable_target_allocation + self.crypto_target_allocation
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Allocations must sum to 1.0 (current: {total})")
        return self


class RiskGuards(BaseModel):
    # Stop-based risk per trade. Not applied by the Wheel (a CSP has no stop; it
    # is bounded by max_collateral_pct_per_trade) nor by the rebalancer.
    max_portfolio_risk_per_trade: float = Field(gt=0.0, le=0.10)
    # Can only be tighter than the floor's DAILY_LOSS_STOP_PCT.
    daily_loss_limit_percentage: float = Field(gt=0.0, le=DAILY_LOSS_STOP_PCT)


class TradingConfig(BaseModel):
    """Root schema of config.json."""

    system_status: SystemStatus
    wheel_parameters: WheelParameters
    hedged_spread_parameters: HedgedSpreadParameters
    crypto_parameters: CryptoParameters
    risk_guards: RiskGuards

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> TradingConfig:
        """Load and validate config.json. Raises ValidationError if invalid."""
        with open(path, encoding="utf-8") as fh:
            return cls.model_validate(json.load(fh))

    def save(self, path: Path = CONFIG_PATH) -> None:
        """Atomic write: tmpfile + os.replace prevents corrupted configs
        if the Lumibot engine reads the file mid-write."""
        payload = self.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


if __name__ == "__main__":
    cfg = TradingConfig.load()
    print("config.json is valid ✔")
    print(cfg.model_dump_json(indent=2))
