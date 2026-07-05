"""Modelos Pydantic v2 para la validación y persistencia de config.json.

Fuente única de verdad del estado del sistema. Todos los módulos
(wheel_strategy, crypto_rebalancer, app_dashboard) leen y escriben
la configuración exclusivamente a través de estas clases, garantizando
que nunca se persistan parámetros inválidos (deltas fuera de rango,
asignaciones que no suman 1.0, etc.).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_PATH = Path(__file__).parent / "config.json"


class SystemStatus(BaseModel):
    active: bool = True
    emergency_kill_switch: bool = False
    live_trading_mode: bool = False  # False => paper trading obligatorio


class WheelParameters(BaseModel):
    whitelist_assets: List[str] = Field(min_length=1)
    target_expiration_days_min: int = Field(ge=1, le=365)
    target_expiration_days_max: int = Field(ge=1, le=365)
    # Delta de la CSP: negativo por convención (put corta OTM).
    delta_limit_csp: float = Field(lt=0.0, ge=-1.0)
    # Delta de la Covered Call: positivo (call corta OTM).
    delta_limit_cc: float = Field(gt=0.0, le=1.0)
    early_close_percentage_gain: float = Field(gt=0.0, le=1.0)
    rolling_trigger_percentage_itm: float = Field(gt=0.0, le=0.50)

    @field_validator("whitelist_assets")
    @classmethod
    def _upper_symbols(cls, v: List[str]) -> List[str]:
        return [s.strip().upper() for s in v if s.strip()]

    @model_validator(mode="after")
    def _check_expiration_window(self) -> "WheelParameters":
        if self.target_expiration_days_min > self.target_expiration_days_max:
            raise ValueError("target_expiration_days_min > target_expiration_days_max")
        return self


class HedgedSpreadParameters(BaseModel):
    spread_width_dollars: float = Field(gt=0.0)
    sell_delta_put: float = Field(lt=0.0, ge=-1.0)
    buy_delta_coverage_put: float = Field(lt=0.0, ge=-1.0)

    @model_validator(mode="after")
    def _coverage_further_otm(self) -> "HedgedSpreadParameters":
        # La put comprada debe estar más OTM (delta menos negativo) que la vendida.
        if self.buy_delta_coverage_put <= self.sell_delta_put:
            raise ValueError(
                "buy_delta_coverage_put debe ser menos negativo que sell_delta_put"
            )
        return self


class CryptoParameters(BaseModel):
    stable_target_allocation: float = Field(ge=0.0, le=1.0)
    crypto_target_allocation: float = Field(ge=0.0, le=1.0)
    rebalance_threshold: float = Field(gt=0.0, le=0.25)
    monitored_assets: List[str] = Field(min_length=1)

    @field_validator("monitored_assets")
    @classmethod
    def _upper_symbols(cls, v: List[str]) -> List[str]:
        return [s.strip().upper() for s in v if s.strip()]

    @model_validator(mode="after")
    def _allocations_sum_to_one(self) -> "CryptoParameters":
        total = self.stable_target_allocation + self.crypto_target_allocation
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Las asignaciones deben sumar 1.0 (actual: {total})")
        return self


class RiskGuards(BaseModel):
    max_portfolio_risk_per_trade: float = Field(gt=0.0, le=0.10)
    daily_loss_limit_percentage: float = Field(gt=0.0, le=0.20)


class TradingConfig(BaseModel):
    """Esquema raíz de config.json."""

    system_status: SystemStatus
    wheel_parameters: WheelParameters
    hedged_spread_parameters: HedgedSpreadParameters
    crypto_parameters: CryptoParameters
    risk_guards: RiskGuards

    # ------------------------------------------------------------------ #
    # Persistencia                                                        #
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "TradingConfig":
        """Carga y valida config.json. Lanza ValidationError si es inválido."""
        with open(path, "r", encoding="utf-8") as fh:
            return cls.model_validate(json.load(fh))

    def save(self, path: Path = CONFIG_PATH) -> None:
        """Escritura atómica: tmpfile + os.replace evita configs corruptas
        si el motor de Lumibot lee el archivo a mitad de escritura."""
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
    print("config.json válido ✔")
    print(cfg.model_dump_json(indent=2))
