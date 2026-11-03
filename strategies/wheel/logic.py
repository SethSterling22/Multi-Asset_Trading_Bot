"""The Wheel: venta de CSP y, tras la asignación, covered calls (ex wheel_strategy.py).

Ciclo:
  1. Vender una put cubierta por caja (CSP) por subyacente de la whitelist.
  2. Con >= 100 acciones (asignación), vender covered calls.
  3. Recomprar una opción corta cuando se ha capturado `early_close_percentage_gain`
     de la prima.

La lógica no conoce Lumibot: habla con el broker a través del Protocol
`OptionsBroker` (el mismo patrón que SpotBroker), así que los guards se prueban
sin broker ni red. strategies/wheel/strategy.py es el adaptador de Lumibot.

Lo que cambió respecto al original, por la auditoría de la Sesión 0:
- La delta sale de core/greeks.py (broker -> Black-Scholes con IV -> se salta).
  El proxy de moneyness ya no existe.
- El check de caja cuenta el colateral de TODAS las CSP abiertas y pendientes.
- Las órdenes pendientes cuentan: no se reenvía una venta ni un cierre sin llenar.
- La prima de entrada sale del ledger (coste medio de la posición), no de memoria:
  el cierre al 80% sobrevive a un reinicio.
- El gate (kill switch, pérdida diaria con equity persistida, parámetros) es el de
  strategies/base.py.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import ClassVar, Protocol

from core.config.models import TradingConfig, WheelParameters
from core.greeks import Delta, NoDelta, Right, resolve_delta
from core.ledger.fills import AssetType, Fill, Side
from core.ledger.positions import PositionKey, replay
from strategies.base import (
    OPTION_MULTIPLIER,
    Rejected,
    StrategyBase,
    check_cash_secured,
    check_collateral_cap,
    check_covered_call,
)

VENUE = "alpaca"
DAYS_PER_YEAR = 365


@dataclass(frozen=True, slots=True, order=True)
class OptionContract:
    underlying: str
    expiration: date
    strike: Decimal
    right: Right

    @property
    def occ_symbol(self) -> str:
        """Símbolo OCC: AAPL261218P00150000."""
        code = "C" if self.right is Right.CALL else "P"
        return f"{self.underlying}{self.expiration:%y%m%d}{code}{int(self.strike * 1000):08d}"

    @classmethod
    def from_occ(cls, symbol: str) -> OptionContract:
        """Inversa de `occ_symbol`: subyacente, AAMMDD, C/P y strike·1000 en 8 dígitos."""
        m = _OCC.fullmatch(symbol)
        if m is None:
            raise ValueError(f"símbolo OCC inválido: {symbol!r}")
        root, ymd, code, strike = m.groups()
        expiration = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
        return cls(root, expiration, Decimal(strike) / 1000,
                   Right.CALL if code == "C" else Right.PUT)


_OCC = re.compile(r"([A-Z.]{1,6})(\d{6})([CP])(\d{8})")


@dataclass(frozen=True, slots=True)
class ShortOption:
    contract: OptionContract
    quantity: int            # contratos vendidos, > 0


@dataclass(frozen=True, slots=True)
class PendingOrder:
    underlying: str
    contract: OptionContract | None   # None = orden sobre la acción
    side: Side
    quantity: int


class OptionsBroker(Protocol):
    def today(self) -> date: ...
    def cash(self) -> Decimal: ...
    def portfolio_value(self) -> Decimal: ...
    def spot(self, symbol: str) -> Decimal | None: ...
    def shares(self, symbol: str) -> Decimal: ...
    def short_options(self) -> list[ShortOption]: ...
    def pending_orders(self) -> list[PendingOrder]: ...
    def expirations(self, symbol: str) -> list[date]: ...
    def strikes(self, symbol: str, expiration: date, right: Right) -> list[Decimal]: ...
    def option_mid(self, contract: OptionContract) -> Decimal | None: ...
    def broker_delta(self, contract: OptionContract) -> float | None: ...
    def risk_free_rate(self) -> float | None: ...
    def sell_to_open(self, contract: OptionContract, quantity: int) -> str | None: ...
    def buy_to_close(self, contract: OptionContract, quantity: int) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    contract: OptionContract
    delta: Delta


class WheelLogic(StrategyBase):
    strategy_id: ClassVar[str] = "wheel"
    # Los datos se piden al broker en cada iteración (no hay stream que caducar):
    # el fallo es un None, y cada None se rechaza explícitamente abajo.
    required_feeds = ()

    def __init__(self, broker: OptionsBroker, fills: Callable[[], Sequence[Fill]]) -> None:
        self.broker = broker
        self._fills = fills

    # -- iteración -----------------------------------------------------------
    def run_iteration(self) -> None:
        cfg = self.config()
        if isinstance(cfg, Rejected):
            self.reject(cfg, action="iteration")
            return
        if isinstance(self.trading_gate(equity=self.broker.portfolio_value()), Rejected):
            return
        for symbol in cfg.wheel_parameters.whitelist_assets:
            try:
                self.run_cycle(symbol, cfg)
            except Exception:
                # Un subyacente no puede tumbar el bucle de los demás.
                self._log.exception("wheel_cycle_error", strategy_id=self.strategy_id,
                                    symbol=symbol, action="cycle", reason="exception")

    def run_cycle(self, symbol: str, cfg: TradingConfig) -> None:
        wheel = cfg.wheel_parameters
        shorts = [s for s in self.broker.short_options() if s.contract.underlying == symbol]
        pending = [p for p in self.broker.pending_orders() if p.underlying == symbol]

        for short in shorts:
            self.maybe_close_early(short, pending, wheel)

        # Una sola opción corta viva por subyacente, contando las pendientes.
        if shorts or pending:
            return

        shares = self.broker.shares(symbol)
        if shares >= OPTION_MULTIPLIER:
            self.sell_covered_call(symbol, shares, wheel)
        else:
            self.sell_cash_secured_put(symbol, wheel)

    # -- cierre anticipado -----------------------------------------------------
    def entry_premium(self, contract: OptionContract) -> Decimal | None:
        """Prima media de la posición corta, desde el ledger (CLAUDE.md §1.3)."""
        positions, _ = replay(self._fills())
        key = PositionKey(VENUE, contract.occ_symbol, AssetType.OPTION, self.strategy_id)
        pos = positions.get(key)
        if pos is None or pos.net_qty >= 0:
            return None
        return pos.avg_cost

    def maybe_close_early(
        self, short: ShortOption, pending: Sequence[PendingOrder], wheel: WheelParameters
    ) -> None:
        symbol = short.contract.occ_symbol
        if any(p.contract == short.contract and p.side is Side.BUY_TO_CLOSE for p in pending):
            return   # ya hay un cierre en curso
        entry = self.entry_premium(short.contract)
        if entry is None or entry <= 0:
            self.reject(Rejected("no_entry_premium", f"{symbol} no está en el ledger"),
                        action="early_close", symbol=symbol)
            return
        mid = self.broker.option_mid(short.contract)
        if mid is None:
            self.reject(Rejected("no_quote", f"{symbol} sin precio"),
                        action="early_close", symbol=symbol)
            return
        captured = (entry - mid) / entry
        if captured >= Decimal(str(wheel.early_close_percentage_gain)):
            self._log.info("early_close", strategy_id=self.strategy_id, symbol=symbol,
                           action="buy_to_close", reason=f"captured {captured:.2%}")
            self.broker.buy_to_close(short.contract, short.quantity)

    # -- aperturas -----------------------------------------------------------
    def committed_collateral(self) -> Decimal:
        """Colateral de todas las puts cortas abiertas y pendientes, de cualquier subyacente."""
        total = Decimal(0)
        for s in self.broker.short_options():
            if s.contract.right is Right.PUT:
                total += s.contract.strike * OPTION_MULTIPLIER * s.quantity
        for p in self.broker.pending_orders():
            if p.contract is not None and p.contract.right is Right.PUT and p.side in (
                Side.SELL_TO_OPEN, Side.SELL
            ):
                total += p.contract.strike * OPTION_MULTIPLIER * p.quantity
        return total

    def sell_cash_secured_put(self, symbol: str, wheel: WheelParameters) -> str | None:
        chosen = self.select_contract(symbol, Right.PUT, wheel.delta_limit_csp, wheel)
        if chosen is None:
            return None
        collateral = chosen.contract.strike * OPTION_MULTIPLIER
        if not self.guard(
            check_cash_secured(
                cash=self.broker.cash(),
                committed_collateral=self.committed_collateral(),
                new_collateral=collateral,
            ),
            action="sell_csp", symbol=symbol,
        ):
            return None
        if not self.guard(
            check_collateral_cap(
                collateral=collateral,
                portfolio_value=self.broker.portfolio_value(),
                max_pct=wheel.max_collateral_pct_per_trade,
            ),
            action="sell_csp", symbol=symbol,
        ):
            return None
        return self.broker.sell_to_open(chosen.contract, 1)

    def sell_covered_call(
        self, symbol: str, shares: Decimal, wheel: WheelParameters
    ) -> str | None:
        chosen = self.select_contract(symbol, Right.CALL, wheel.delta_limit_cc, wheel)
        if chosen is None:
            return None
        open_calls = sum(
            s.quantity for s in self.broker.short_options()
            if s.contract.underlying == symbol and s.contract.right is Right.CALL
        )
        pending_calls = sum(
            p.quantity for p in self.broker.pending_orders()
            if p.underlying == symbol and p.contract is not None
            and p.contract.right is Right.CALL and p.side is Side.SELL_TO_OPEN
        )
        if not self.guard(
            check_covered_call(shares=shares, open_short_calls=open_calls,
                               pending_short_calls=pending_calls, new_contracts=1),
            action="sell_cc", symbol=symbol,
        ):
            return None
        return self.broker.sell_to_open(chosen.contract, 1)

    # -- selección de contrato -------------------------------------------------
    def pick_expiration(self, symbol: str, wheel: WheelParameters) -> date | None:
        today = self.broker.today()
        first = today + timedelta(days=wheel.target_expiration_days_min)
        last = today + timedelta(days=wheel.target_expiration_days_max)
        candidates = [e for e in self.broker.expirations(symbol) if first <= e <= last]
        return min(candidates) if candidates else None

    def select_contract(
        self, symbol: str, right: Right, target_delta: float, wheel: WheelParameters
    ) -> Candidate | None:
        """El contrato OTM de la ventana de DTE con la delta más cercana al objetivo.

        Sin delta fiable para ningún strike, no hay contrato: se registra UN
        rechazo con el motivo más frecuente (p.ej. 'no_greeks') y no se emite orden.
        """
        action = "select_put" if right is Right.PUT else "select_call"
        spot = self.broker.spot(symbol)
        if spot is None or spot <= 0:
            self.reject(Rejected("no_spot", f"{symbol} sin precio"), action=action, symbol=symbol)
            return None
        expiration = self.pick_expiration(symbol, wheel)
        if expiration is None:
            self.reject(Rejected("no_expiration", f"{symbol}: ninguna en la ventana de DTE"),
                        action=action, symbol=symbol)
            return None

        t_years = (expiration - self.broker.today()).days / DAYS_PER_YEAR
        rate = self.broker.risk_free_rate()
        candidates: list[Candidate] = []
        failures: Counter[str] = Counter()
        for strike in self.broker.strikes(symbol, expiration, right):
            otm = strike < spot if right is Right.PUT else strike > spot
            if not otm:
                continue
            contract = OptionContract(symbol, expiration, strike, right)
            mid = self.broker.option_mid(contract)
            delta = resolve_delta(
                right,
                spot=float(spot),
                strike=float(strike),
                t_years=t_years,
                risk_free_rate=rate,
                broker_delta=self.broker.broker_delta(contract),
                option_price=None if mid is None else float(mid),
            )
            if isinstance(delta, NoDelta):
                failures[delta.reason] += 1
                continue
            candidates.append(Candidate(contract, delta))

        if not candidates:
            reason = failures.most_common(1)[0][0] if failures else "no_otm_strikes"
            self.reject(
                Rejected(reason, f"{symbol} {expiration}: ningún strike con delta fiable "
                                 f"({dict(failures)})"),
                action=action, symbol=symbol,
            )
            return None
        return min(candidates, key=lambda c: (abs(c.delta.value - target_delta), c.contract))
