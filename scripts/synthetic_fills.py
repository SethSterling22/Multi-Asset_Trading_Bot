"""200 fills sintéticos -> equity curve diaria y P&L por sleeve, con comprobaciones.

Uso:
    uv run python -m scripts.synthetic_fills
    uv run python -m scripts.synthetic_fills --database-url postgresql+psycopg://...

Los fills son reproducibles (semilla fija). Las comprobaciones no reutilizan el
método de coste medio de core/ledger, para que un error en él no se confirme a
sí mismo:

1. Identidad de caja por sleeve, a partir de los fills crudos:
       equity = asignado − Σ(qty·price·mult con signo) + Σ(q_final·mark·mult)
                − fees + funding
2. Σ net_pnl diario == Σ realizado − fees + funding (el slippage no se resta dos veces).
3. Σ gross − Σ slippage == Σ realizado.
4. Ningún sleeve long/flat queda corto en ningún momento.
5. Con --database-url: lo releído de Postgres es idéntico a lo generado y la vista
   `positions` coincide con el replay. La transacción se deshace al final salvo
   con --commit.
"""

from __future__ import annotations

import argparse
import math
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from core.ledger.attribution import pnl_by_strategy_day
from core.ledger.equity import EquitySnapshot, snapshot
from core.ledger.fills import AssetType, Fill, Side
from core.ledger.positions import replay

SEED = 20261101
START = datetime(2026, 9, 1, tzinfo=UTC)
N_FILLS = 200
FEE_RATE = Decimal("0.0005")          # 5 bps del nocional: supuesto sintético
OPTION_FEE = Decimal("0.65")          # por contrato
CENT = Decimal("0.01")


class CheckFailed(AssertionError):
    """Una comprobación del script falló."""


def verify(ok: bool, detail: object) -> None:
    # No `assert`: desaparece con `python -O` y la comprobación dejaría de correr.
    if not ok:
        raise CheckFailed(detail)


class Direction(Enum):
    LONG_FLAT = "long/flat"
    SHORT_FLAT = "short/flat"
    ANY = "long/short"


@dataclass(frozen=True)
class Instrument:
    venue: str
    symbol: str
    asset_type: AssetType
    multiplier: Decimal
    price: float
    daily_vol: float
    lot: Decimal
    direction: Direction


SLEEVES: dict[str, list[Instrument]] = {
    "trend": [
        Instrument("coinbase", "BIP-20DEC30-CDE", AssetType.CRYPTO_PERP, Decimal("0.01"),
                   100_000, 0.025, Decimal(1), Direction.ANY),
    ],
    "funding_carry": [
        Instrument("coinbase", "BTC-USD", AssetType.CRYPTO_SPOT, Decimal(1),
                   100_000, 0.025, Decimal("0.01"), Direction.LONG_FLAT),
        Instrument("coinbase", "BIP-20DEC30-CDE", AssetType.CRYPTO_PERP, Decimal("0.01"),
                   100_000, 0.025, Decimal(1), Direction.SHORT_FLAT),
    ],
    "xsec_momentum": [
        Instrument("coinbase", "SOL-USD", AssetType.CRYPTO_SPOT, Decimal(1),
                   200, 0.05, Decimal(1), Direction.LONG_FLAT),
        Instrument("coinbase", "AVAX-USD", AssetType.CRYPTO_SPOT, Decimal(1),
                   30, 0.05, Decimal(5), Direction.LONG_FLAT),
        Instrument("coinbase", "ETP-20DEC30-CDE", AssetType.CRYPTO_PERP, Decimal("0.1"),
                   4_000, 0.03, Decimal(1), Direction.ANY),
    ],
    "crypto_rebalance": [
        Instrument("coinbase", "BTC-USD", AssetType.CRYPTO_SPOT, Decimal(1),
                   100_000, 0.025, Decimal("0.005"), Direction.LONG_FLAT),
        Instrument("coinbase", "ETH-USD", AssetType.CRYPTO_SPOT, Decimal(1),
                   4_000, 0.03, Decimal("0.1"), Direction.LONG_FLAT),
    ],
    "wheel": [
        Instrument("alpaca", "SPY", AssetType.STOCK, Decimal(1),
                   550, 0.01, Decimal(100), Direction.LONG_FLAT),
        Instrument("alpaca", "SPY261218P00550000", AssetType.OPTION, Decimal(100),
                   6, 0.08, Decimal(1), Direction.SHORT_FLAT),
    ],
}
ALLOCATED: dict[str, Decimal] = {
    "trend": Decimal(5_000), "funding_carry": Decimal(10_000),
    "xsec_momentum": Decimal(10_000), "crypto_rebalance": Decimal(5_000),
    "wheel": Decimal(60_000),
}

Key = tuple[str, str, str]          # (strategy_id, venue, symbol)


@dataclass
class Market:
    """Precios de un paseo aleatorio por (venue, symbol), con su historia."""

    rng: random.Random
    price: dict[tuple[str, str], float]
    history: dict[tuple[str, str], list[tuple[datetime, Decimal]]]

    def step(self, inst: Instrument, ts: datetime, dt_days: float) -> Decimal:
        vs = (inst.venue, inst.symbol)
        p = self.price.get(vs, inst.price)
        p *= math.exp(self.rng.gauss(0.0, inst.daily_vol * math.sqrt(dt_days)))
        self.price[vs] = p
        quantized = Decimal(str(p)).quantize(CENT)
        self.history.setdefault(vs, []).append((ts, quantized))
        return quantized

    def mark(self, venue: str, symbol: str, ts: datetime) -> Decimal:
        return [p for t, p in self.history[(venue, symbol)] if t <= ts][-1]


def _side(inst: Instrument, buy: bool, closing: bool) -> Side:
    if inst.asset_type is AssetType.OPTION:
        if buy:
            return Side.BUY_TO_CLOSE if closing else Side.BUY_TO_OPEN
        return Side.SELL_TO_CLOSE if closing else Side.SELL_TO_OPEN
    return Side.BUY if buy else Side.SELL


def generate(seed: int = SEED) -> tuple[list[Fill], Market]:
    rng = random.Random(seed)
    market = Market(rng, {}, {})
    held: dict[Key, Decimal] = defaultdict(Decimal)
    fills: list[Fill] = []
    ts = START
    while len(fills) < N_FILLS:
        dt = timedelta(minutes=rng.randint(60, 780))
        ts += dt
        strategy_id = rng.choice(sorted(SLEEVES))
        inst = rng.choice(SLEEVES[strategy_id])
        key = (strategy_id, inst.venue, inst.symbol)
        price = market.step(inst, ts, dt.total_seconds() / 86_400)
        q = held[key]
        qty = inst.lot * rng.randint(1, 5)

        if inst.direction is Direction.LONG_FLAT:
            buy = q == 0 or rng.random() < 0.5
            if not buy:
                qty = min(qty, q)
        elif inst.direction is Direction.SHORT_FLAT:
            buy = q != 0 and rng.random() < 0.5
            if buy:
                qty = min(qty, -q)
        else:
            buy = rng.random() < 0.5
        closing = (q > 0 and not buy) or (q < 0 and buy)

        # Ejecución entre 2 bps mejor y 6 bps peor que el precio de decisión.
        adverse = Decimal(str(rng.uniform(-2, 6) / 10_000))
        fill_price = (price * (1 + adverse if buy else 1 - adverse)).quantize(CENT)
        notional = qty * fill_price * inst.multiplier
        fee = (OPTION_FEE * qty if inst.asset_type is AssetType.OPTION
               else (notional * FEE_RATE).quantize(CENT))
        fills.append(Fill(
            ts=ts, venue=inst.venue, symbol=inst.symbol, asset_type=inst.asset_type,
            side=_side(inst, buy, closing), qty=qty, price=fill_price,
            multiplier=inst.multiplier, commission=fee, decision_price=price,
            strategy_id=strategy_id, venue_fill_id=f"syn-{len(fills):03d}",
        ))
        held[key] += qty if buy else -qty

        # Cada ~12 fills, un pago de funding por cada perp abierto.
        if rng.random() < 1 / 12:
            for (sid, venue, symbol), q_open in sorted(held.items()):
                if len(fills) >= N_FILLS or q_open == 0 or not symbol.endswith("-CDE"):
                    continue
                perp = next(i for i in SLEEVES[sid] if i.symbol == symbol)
                mark = market.mark(venue, symbol, ts)
                rate = Decimal(str(round(rng.gauss(0.0001, 0.0001), 6)))
                # Con funding positivo, el largo paga y el corto cobra.
                payment = (-q_open * mark * perp.multiplier * rate).quantize(CENT)
                fills.append(Fill(
                    ts=ts, venue=venue, symbol=symbol, asset_type=AssetType.CRYPTO_PERP,
                    side=Side.SELL, qty=Decimal(0), price=mark, multiplier=perp.multiplier,
                    funding_payment=payment, strategy_id=sid,
                    venue_fill_id=f"fund-{sid}-{symbol}-{ts:%Y%m%d%H%M}",
                ))
    return fills, market


def daily_equity(fills: Sequence[Fill], market: Market) -> dict[str, list[EquitySnapshot]]:
    last_day = fills[-1].ts.date()
    days = (last_day - START.date()).days + 1
    curve: dict[str, list[EquitySnapshot]] = {}
    for sid in sorted(SLEEVES):
        snaps = []
        for d in range(days):
            eod = START + timedelta(days=d + 1) - timedelta(microseconds=1)
            marks = {
                vs: market.mark(*vs, eod)
                for vs in market.history
                if market.history[vs][0][0] <= eod
            }
            snaps.append(snapshot(fills, strategy_id=sid, allocated_usd=ALLOCATED[sid],
                                  marks=marks, ts=eod))
        curve[sid] = snaps
    return curve


def check(fills: Sequence[Fill], market: Market,
          curve: dict[str, list[EquitySnapshot]]) -> dict[str, dict[str, Decimal]]:
    """Comprobaciones 1-4. Devuelve el resumen por sleeve."""
    _, per_fill = replay(fills)
    daily = pnl_by_strategy_day(fills)
    end = curve[next(iter(curve))][-1].ts
    summary: dict[str, dict[str, Decimal]] = {}
    for sid in sorted(SLEEVES):
        mine = [f for f in fills if f.strategy_id == sid]
        cash = sum((f.signed_qty * f.price * f.multiplier for f in mine), Decimal(0))
        fees = sum((f.commission for f in mine), Decimal(0))
        funding = sum((f.funding_payment for f in mine), Decimal(0))
        final_qty: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        running: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        long_flat = {(i.venue, i.symbol) for i in SLEEVES[sid]
                     if i.direction is Direction.LONG_FLAT}
        for f in sorted(mine, key=lambda f: f.ts):
            running[(f.venue, f.symbol)] += f.signed_qty
            if (f.venue, f.symbol) in long_flat:
                verify(running[(f.venue, f.symbol)] >= 0, f"{sid} {f.symbol} quedó corto")
            final_qty[(f.venue, f.symbol)] += f.signed_qty
        position_value = sum(
            (q * market.mark(v, s, end) * next(i.multiplier for i in SLEEVES[sid]
                                               if i.symbol == s)
             for (v, s), q in final_qty.items()),
            Decimal(0),
        )
        identity = ALLOCATED[sid] - cash + position_value - fees + funding
        equity = curve[sid][-1].equity_usd
        verify(abs(identity - equity) < Decimal("1e-9"), (sid, identity, equity))

        realized = sum((fp.realized_pnl for fp in per_fill if fp.fill.strategy_id == sid),
                       Decimal(0))
        rows = [r for r in daily if r.strategy_id == sid]
        net = sum((r.net_pnl for r in rows), Decimal(0))
        gross = sum((r.gross_pnl for r in rows), Decimal(0))
        slip = sum((r.slippage for r in rows), Decimal(0))
        verify(net == realized - fees + funding, (sid, net, realized - fees + funding))
        verify(gross - slip == realized, (sid, gross - slip, realized))

        summary[sid] = {
            "fills": Decimal(sum(1 for f in mine if f.qty != 0)),
            "funding_rows": Decimal(sum(1 for f in mine if f.qty == 0)),
            "realized": realized, "fees": fees, "funding": funding, "slippage": slip,
            "net": net, "unrealized": curve[sid][-1].unrealized_pnl,
            "equity": equity, "allocated": ALLOCATED[sid],
        }
    return summary


def check_database(url: str, fills: Sequence[Fill],
                   curve: dict[str, list[EquitySnapshot]], commit: bool) -> None:
    """Comprobación 5: escribir, releer y comparar."""
    from sqlalchemy import create_engine

    from core.ledger.equity import write_snapshot
    from core.ledger.fills import fetch_fills, insert_fills
    from core.ledger.positions import load_positions_view, reconstruct

    engine = create_engine(url)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            verify(insert_fills(conn, fills) == len(fills), "fills ya presentes en la base")
            back = fetch_fills(conn)
            verify(back == sorted(fills, key=lambda f: f.ts), "lo releído difiere")
            view = load_positions_view(conn)
            replayed = reconstruct(back)
            verify(view.keys() == replayed.keys(), "la vista y el replay difieren en claves")
            for key, row in view.items():
                verify(row.net_qty == replayed[key].net_qty, key)
                verify(row.net_cost == replayed[key].net_cost, key)
            for snaps in curve.values():
                for snap in snaps:
                    write_snapshot(conn, snap)
            print(f"Postgres: {len(back)} fills releídos, {len(view)} posiciones en la vista,"
                  " todo idéntico.")
        finally:
            if commit:
                trans.commit()
                print("Postgres: transacción confirmada (--commit).")
            else:
                trans.rollback()
                print("Postgres: transacción deshecha (sin --commit).")
    engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--database-url")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    fills, market = generate()
    curve = daily_equity(fills, market)
    summary = check(fills, market, curve)

    days = len(next(iter(curve.values())))
    print(f"{len(fills)} fills sintéticos, {fills[0].ts:%Y-%m-%d} -> {fills[-1].ts:%Y-%m-%d},"
          f" {days} snapshots diarios por sleeve. Comprobaciones 1-4: OK.\n")
    cols = ["fills", "funding_rows", "realized", "fees", "funding", "slippage", "net",
            "unrealized", "allocated", "equity"]
    print(f"{'sleeve':<17}" + "".join(f"{c:>13}" for c in cols))
    for sid, row in summary.items():
        print(f"{sid:<17}" + "".join(
            f"{row[c]:>13,.0f}" if c in {"fills", "funding_rows"} else f"{row[c]:>13,.2f}"
            for c in cols))

    if args.database_url:
        print()
        check_database(args.database_url, fills, curve, args.commit)


if __name__ == "__main__":
    main()
