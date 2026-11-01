"""Checks pre-trade contra el risk floor. Librería in-process, NUNCA un servicio.

CLAUDE.md §1.6: un check de riesgo que puede hacer timeout fallará abierto justo
cuando importa. Se importa dentro de execution/router.py y se llama con el estado
de la cartera DESPUÉS de la orden propuesta.

Cada check devuelve un Verdict. Un dato que falta para evaluar un check
(margen de mantenimiento, sigma, stop) es un rechazo, no un pase.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.ledger.fills import AssetType
from core.risk_floor import (
    CORRELATION_STRESS_ASSUMPTION,
    MAJORS,
    MAX_CORRELATION_ADJUSTED_RISK_PCT,
    MAX_GROSS_EXPOSURE_PCT,
    MAX_NET_EXPOSURE_PCT,
    MAX_NOTIONAL_PCT_EQUITY,
    MAX_OPEN_POSITIONS,
    MAX_RISK_PER_TRADE_PCT,
    MIN_LIQ_DISTANCE_SIGMAS,
    correlation_adjusted_risk,
    max_leverage_for,
)
from risk.verdict import ALLOWED, Rejected, Verdict, first_rejection

DERIVATIVES = frozenset({AssetType.CRYPTO_PERP, AssetType.FUTURE})


@dataclass(frozen=True, slots=True)
class ProposedState:
    """La cartera tal como quedaría si se ejecutase la orden."""

    symbol: str
    asset_type: AssetType
    equity: Decimal
    position_qty: Decimal            # con signo, tras la orden
    position_notional: Decimal       # |qty·price·multiplier| de esta posición, tras la orden
    leverage: Decimal                # nocional / margen de esta posición (1 en spot)
    gross_notional: Decimal          # Σ |nocional| de la cartera
    net_notional: Decimal            # Σ nocional con signo
    open_positions: int
    risk_usd: Decimal | None         # pérdida hasta el stop; None = sin stop
    maintenance_margin: Decimal | None = None   # fracción, p.ej. 0.004 en BTC
    daily_sigma: Decimal | None = None          # sigma diaria del precio, fracción
    position_risks_pct: tuple[Decimal, ...] = ()  # riesgo de cada posición abierta / equity
    mean_correlation: Decimal | None = None       # correlación media rodante


def _base(symbol: str) -> str:
    return symbol.split("-")[0].split("/")[0].upper()


def check_equity(s: ProposedState) -> Verdict:
    if s.equity <= 0:
        return Rejected("no_equity", f"equity {s.equity} <= 0")
    return ALLOWED


def check_leverage(s: ProposedState) -> Verdict:
    limit = Decimal(str(max_leverage_for(s.symbol)))
    if s.leverage > limit:
        return Rejected("leverage", f"{s.symbol}: {s.leverage}x > {limit}x")
    return ALLOWED


def check_no_alt_shorts(s: ProposedState) -> Verdict:
    # Alts: spot a 1x, sin margen. Un corto en un alt es imposible por construcción.
    if s.position_qty < 0 and _base(s.symbol) not in MAJORS:
        return Rejected("alt_short", f"{s.symbol}: los alts son long/flat")
    return ALLOWED


def check_position_notional(s: ProposedState) -> Verdict:
    limit = MAX_NOTIONAL_PCT_EQUITY * float(s.equity)
    if float(s.position_notional) > limit:
        return Rejected(
            "position_notional", f"{s.symbol}: nocional {s.position_notional} > {limit:.2f}"
        )
    return ALLOWED


def check_exposure(s: ProposedState) -> Verdict:
    gross_limit = MAX_GROSS_EXPOSURE_PCT * float(s.equity)
    if float(s.gross_notional) > gross_limit:
        return Rejected("gross_exposure", f"bruto {s.gross_notional} > {gross_limit:.2f}")
    net_limit = MAX_NET_EXPOSURE_PCT * float(s.equity)
    if abs(float(s.net_notional)) > net_limit:
        return Rejected("net_exposure", f"neto {s.net_notional} > {net_limit:.2f}")
    return ALLOWED


def check_open_positions(s: ProposedState) -> Verdict:
    if s.open_positions > MAX_OPEN_POSITIONS:
        return Rejected("open_positions", f"{s.open_positions} > {MAX_OPEN_POSITIONS}")
    return ALLOWED


def check_risk_per_trade(s: ProposedState) -> Verdict:
    if s.risk_usd is None:
        # Un derivado apalancado sin stop no tiene pérdida acotada (CLAUDE.md §1.5).
        if s.asset_type in DERIVATIVES:
            return Rejected("no_stop", f"{s.symbol}: derivado sin stop")
        return ALLOWED
    limit = MAX_RISK_PER_TRADE_PCT * float(s.equity)
    if float(s.risk_usd) > limit:
        return Rejected("risk_per_trade", f"{s.symbol}: riesgo {s.risk_usd} > {limit:.2f}")
    return ALLOWED


def check_liquidation_distance(s: ProposedState) -> Verdict:
    """Distancia a liquidación (margen aislado, 1/L − m) >= MIN_LIQ_DISTANCE_SIGMAS·σ_diaria.

    Redundante bajo MAX_LEVERAGE_MAJORS hoy (1.5x da 27.6σ en BTC): se mantiene
    como red independiente por si algún día sube el apalancamiento.
    """
    if s.asset_type not in DERIVATIVES:
        return ALLOWED
    if s.maintenance_margin is None or s.daily_sigma is None or s.daily_sigma <= 0:
        return Rejected("liq_distance_unknown", f"{s.symbol}: falta margen o sigma")
    if s.leverage <= 0:
        return Rejected("liq_distance_unknown", f"{s.symbol}: apalancamiento {s.leverage}")
    distance = 1 / s.leverage - s.maintenance_margin
    required = Decimal(str(MIN_LIQ_DISTANCE_SIGMAS)) * s.daily_sigma
    if distance < required:
        return Rejected(
            "liq_distance",
            f"{s.symbol}: distancia {distance:.4f} < {MIN_LIQ_DISTANCE_SIGMAS}σ = {required:.4f}",
        )
    return ALLOWED


def check_correlation_adjusted_risk(s: ProposedState) -> Verdict:
    """Riesgo de cartera con la correlación, no por posición (informe §4.6).

    Sin correlación medida se asume la de estrés: no saber no puede salir más barato.
    """
    if not s.position_risks_pct:
        return ALLOWED
    rho = (
        float(s.mean_correlation)
        if s.mean_correlation is not None
        else CORRELATION_STRESS_ASSUMPTION
    )
    worst = float(max(s.position_risks_pct))
    portfolio = correlation_adjusted_risk(worst, len(s.position_risks_pct), rho)
    if portfolio > MAX_CORRELATION_ADJUSTED_RISK_PCT:
        return Rejected(
            "correlation_adjusted_risk",
            f"riesgo de cartera {portfolio:.4f} > {MAX_CORRELATION_ADJUSTED_RISK_PCT}"
            f" (n={len(s.position_risks_pct)}, rho={rho})",
        )
    return ALLOWED


def check_order(s: ProposedState) -> Verdict:
    """Todos los checks; el primer rechazo gana."""
    return first_rejection(
        check_equity(s),
        check_leverage(s),
        check_no_alt_shorts(s),
        check_position_notional(s),
        check_exposure(s),
        check_open_positions(s),
        check_risk_per_trade(s),
        check_liquidation_distance(s),
        check_correlation_adjusted_risk(s),
    )
