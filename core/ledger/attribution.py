"""P&L por estrategia y día. Toda cifra es NETA o no se reporta (CLAUDE.md §3.4).

Convención de signo, la de la vista `pnl_by_strategy_day` del schema de referencia:

    net_pnl = gross_pnl − fees + funding − slippage

- fees >= 0 siempre: es un coste y se resta.
- funding con signo: positivo = cobrado, se suma.
- slippage >= 0 para ejecución adversa, se resta. Compra: (price − decision_price);
  venta: (decision_price − price); por qty y multiplier. Negativo = ejecución mejor
  que el precio de decisión, y se ve, no se absorbe.

`gross_pnl` es el P&L realizado a precios de fill MÁS el slippage del día, es
decir, lo que habrían dado las decisiones a su precio de decisión. Así el
slippage aparece en el waterfall sin contarse dos veces: el P&L realizado ya
está calculado con los precios de ejecución, y `net_pnl` queda igual al P&L
realizado − fees + funding (DECISIONS.md D-004).

Los días son UTC.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from core.ledger.fills import Fill
from core.ledger.positions import ZERO, replay

StrategyDay = tuple[str, date]


def slippage(fill: Fill) -> Decimal:
    """Coste de ejecución frente al precio de decisión, con signo de coste."""
    if fill.decision_price is None or fill.qty == 0:
        return ZERO
    diff = fill.price - fill.decision_price if fill.side.sign > 0 else (
        fill.decision_price - fill.price
    )
    return diff * fill.qty * fill.multiplier


def utc_day(ts: datetime) -> date:
    return ts.astimezone(UTC).date()


@dataclass(frozen=True, slots=True)
class DailyPnl:
    strategy_id: str
    day: date
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    ai_cost: Decimal
    n_fills: int

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees + self.funding - self.slippage

    @property
    def net_pnl_after_ai(self) -> Decimal:
        return self.net_pnl - self.ai_cost


def pnl_by_strategy_day(
    fills: Iterable[Fill], ai_cost: Mapping[StrategyDay, Decimal] | None = None
) -> list[DailyPnl]:
    """Una fila por (estrategia, día UTC) con fills, ordenadas por ambos."""
    ai_cost = ai_cost or {}
    _, per_fill = replay(fills)
    acc: dict[StrategyDay, list[Decimal]] = defaultdict(lambda: [ZERO] * 4)
    n_fills: dict[StrategyDay, int] = defaultdict(int)
    for fp in per_fill:
        f = fp.fill
        key = (f.strategy_id, utc_day(f.ts))
        slip = slippage(f)
        row = acc[key]
        row[0] += fp.realized_pnl + slip
        row[1] += f.commission
        row[2] += f.funding_payment
        row[3] += slip
        if f.qty != 0:
            n_fills[key] += 1
    return [
        DailyPnl(
            strategy_id=strategy_id,
            day=day,
            gross_pnl=gross,
            fees=fees,
            funding=funding,
            slippage=slip,
            ai_cost=ai_cost.get((strategy_id, day), ZERO),
            n_fills=n_fills[(strategy_id, day)],
        )
        for (strategy_id, day), (gross, fees, funding, slip) in sorted(acc.items())
    ]


@dataclass(frozen=True, slots=True)
class LlmCallCost:
    cost_usd: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionUse:
    decision_id: UUID
    llm_call_id: UUID
    strategy_id: str


def allocate_ai_cost(
    calls: Mapping[UUID, LlmCallCost], decisions: Iterable[DecisionUse]
) -> tuple[dict[StrategyDay, Decimal], Decimal]:
    """Reparte el coste de cada llm_call a partes iguales entre las decisiones que la usaron.

    Una llamada sirve a varios símbolos y varios arms: atribuirla íntegra a cada
    decisión la contaría N veces. El día es el de la llamada (UTC).

    Devuelve (coste por (estrategia, día), coste no atribuido). El no atribuido
    son llamadas que ninguna decisión usó: siguen siendo coste, y cuentan contra
    el presupuesto aunque no caigan en ningún sleeve.
    """
    users: dict[UUID, list[DecisionUse]] = defaultdict(list)
    for d in decisions:
        if d.llm_call_id not in calls:
            raise KeyError(f"decisión {d.decision_id} usa una llm_call desconocida")
        users[d.llm_call_id].append(d)

    by_strategy_day: dict[StrategyDay, Decimal] = defaultdict(lambda: ZERO)
    unattributed = ZERO
    for call_id, call in calls.items():
        used_by = users.get(call_id, [])
        if not used_by:
            unattributed += call.cost_usd
            continue
        # Partes iguales; la última decisión (en orden de id, determinista) se
        # lleva el resto de la división, para que la suma sea EXACTAMENTE el coste.
        used_by.sort(key=lambda d: d.decision_id)
        share = call.cost_usd / len(used_by)
        day = utc_day(call.created_at)
        for d in used_by[:-1]:
            by_strategy_day[(d.strategy_id, day)] += share
        by_strategy_day[(used_by[-1].strategy_id, day)] += (
            call.cost_usd - share * (len(used_by) - 1)
        )
    return dict(by_strategy_day), unattributed
