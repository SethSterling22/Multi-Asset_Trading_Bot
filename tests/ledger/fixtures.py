"""Secuencias de fills con resultados calculados a mano.

Los números esperados están en los comentarios; si un test falla, se puede
rehacer la cuenta con lápiz desde aquí.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from core.ledger.fills import AssetType, Fill, Side

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def fill(i: int, side: Side, qty: str, price: str, **kw: object) -> Fill:
    defaults: dict[str, object] = {
        "ts": at(i),
        "venue": "coinbase",
        "symbol": "BIP-20DEC30-CDE",
        "asset_type": AssetType.CRYPTO_PERP,
        "strategy_id": "trend",
        "venue_fill_id": f"f-{i}",
    }
    defaults.update(kw)
    return Fill(side=side, qty=D(qty), price=D(price), **defaults)  # type: ignore[arg-type]


# Largo, ampliación, reducción, cruce a corto y recompra parcial.
#   t1 buy  2 @ 100 -> q= 2, coste 100,              realizado   0
#   t2 buy  1 @ 130 -> q= 3, coste (200+130)/3 = 110, realizado   0
#   t3 sell 2 @ 120 -> q= 1, coste 110,  2·(120−110) = +20
#   t4 sell 3 @  90 -> cierra 1: 1·(90−110) = −20; cruza: q=−2, coste 90
#   t5 buy  1 @  80 -> cierra 1 del corto: 1·(80−90)·(−1) = +10; q=−1, coste 90
# Final: q = −1, coste 90, realizado +10.
# net_cost = 200 + 130 − 240 − 270 + 80 = −100.
TRACED = [
    fill(1, Side.BUY, "2", "100"),
    fill(2, Side.BUY, "1", "130"),
    fill(3, Side.SELL, "2", "120"),
    fill(4, Side.SELL, "3", "90"),
    fill(5, Side.BUY, "1", "80"),
]
TRACED_REALIZED = [D(0), D(0), D(20), D(-20), D(10)]

# Put vendida y recomprada, multiplier 100, comisión 0.65 por contrato.
#   realizado = 1·(0.50 − 2.50)·(−1)·100 = +200;  comisiones 1.30
OPTION = [
    fill(6, Side.SELL_TO_OPEN, "1", "2.50", venue="alpaca", symbol="SPY261218P00550000",
         asset_type=AssetType.OPTION, strategy_id="wheel", multiplier=D(100),
         commission=D("0.65"), venue_fill_id="o-1"),
    fill(7, Side.BUY_TO_CLOSE, "1", "0.50", venue="alpaca", symbol="SPY261218P00550000",
         asset_type=AssetType.OPTION, strategy_id="wheel", multiplier=D(100),
         commission=D("0.65"), venue_fill_id="o-2"),
]

# Pago de funding sobre el corto de trend: qty 0, pagado (negativo).
FUNDING = fill(8, Side.SELL, "0", "85", funding_payment=D("-3.25"), venue_fill_id="fund-1")
