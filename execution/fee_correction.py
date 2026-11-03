"""Corrección de comisiones en especie de fills ya registrados (DECISIONS.md D-015).

Hasta D-015 el router registraba la cantidad BRUTA de una compra cripto y comisión 0,
pero Alpaca entrega la cantidad neta: se queda el 0.25 % de cada ejecución en el
activo comprado. La reconciliación lo detectó y paró el sleeve.

`fills` es append-only, así que no se reescribe nada: por cada compra afectada se añade
un fill de corrección que representa lo que Alpaca se quedó, con
    side = sell, qty = comisión en especie, price = el del fill original,
    commission = qty · price,
de modo que la posición baja exactamente esa cantidad, el realizado es 0 (se "vende"
a su propio coste) y el flujo de caja es 0 (no entró ni salió dinero: se pagó en
especie). `venue_fill_id` = "<id de la orden>:fee", así que repetirlo es un no-op.

    python -m execution.fee_correction                      # muestra, no escribe
    python -m execution.fee_correction --apply              # escribe las correcciones
    python -m execution.fee_correction --apply --lift-kill-switch --actor seth

`--lift-kill-switch` solo levanta el kill switch del sleeve si, ya corregido, el ledger
cuadra con el venue. Si no cuadra, no lo levanta y sale con código 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import Connection, Engine, create_engine, text

from brokers.base import Execution, SpotVenue
from core.ledger.fills import Fill, Side, fetch_fills, insert_fill
from execution.reconcile import position_mismatches

FEE_SUFFIX = ":fee"


def corrections(conn: Connection, venue: SpotVenue, *, strategy_id: str,
                fee_in_kind: Callable[[list[Execution]], Decimal]) -> list[Fill]:
    """Las correcciones que faltan: compras sin comisión registrada y sin su ':fee'."""
    fills = [f for f in fetch_fills(conn, strategy_id) if f.venue == venue.name]
    done = {f.venue_fill_id.removesuffix(FEE_SUFFIX) for f in fills
            if f.venue_fill_id.endswith(FEE_SUFFIX)}
    out = []
    for f in fills:
        if f.side is not Side.BUY or f.commission != 0 or f.venue_fill_id in done:
            continue
        fee = fee_in_kind(venue.executions(f.venue_fill_id))
        if fee <= 0:
            continue
        out.append(Fill(
            ts=f.ts, venue=f.venue, symbol=f.symbol, asset_type=f.asset_type, side=Side.SELL,
            qty=fee, price=f.price, commission=fee * f.price, strategy_id=f.strategy_id,
            venue_fill_id=f"{f.venue_fill_id}{FEE_SUFFIX}", client_order_id=f.client_order_id,
            multiplier=f.multiplier))
    return out


def lift_kill_switch(engine: Engine, *, strategy_id: str, actor: str, reason: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO risk_events (kind, scope, reason, actor, metadata)
            VALUES ('kill_switch_off', :s, :reason, :actor, CAST(:meta AS jsonb))
        """), {"s": strategy_id, "reason": reason, "actor": actor,
               "meta": json.dumps({"tool": "execution.fee_correction"})})


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from brokers.alpaca_crypto import AlpacaCryptoVenue, fee_in_kind

    args = argparse.ArgumentParser(description="Corrige comisiones en especie (D-015)")
    args.add_argument("--strategy", default="crypto_rebalance")
    args.add_argument("--apply", action="store_true", help="escribe las correcciones")
    args.add_argument("--lift-kill-switch", action="store_true",
                      help="levanta el kill switch del sleeve si el ledger ya cuadra")
    args.add_argument("--actor", help="quién levanta el kill switch (obligatorio con él)")
    opts = args.parse_args(argv)
    if opts.lift_kill_switch and not (opts.apply and opts.actor):
        sys.exit("--lift-kill-switch necesita --apply y --actor <tu nombre>")

    load_dotenv()
    engine = create_engine(os.environ["DATABASE_URL"])
    venue = AlpacaCryptoVenue(os.environ["ALPACA_CRYPTO_API_KEY"],
                              os.environ["ALPACA_CRYPTO_API_SECRET"])
    with engine.connect() as conn:
        todo = corrections(conn, venue, strategy_id=opts.strategy, fee_in_kind=fee_in_kind)
    for f in todo:
        print(f"{f.symbol}: comisión en especie {f.qty} (≈ ${f.commission:.2f}) "
              f"-> {f.venue_fill_id}")
    if not todo:
        print("Nada que corregir.")
    if not opts.apply:
        print("Sin --apply: no se ha escrito nada.")
    else:
        with engine.begin() as conn:
            written = sum(insert_fill(conn, f) for f in todo)
        print(f"Escritas {written} correcciones.")

    mismatches = position_mismatches(engine, venue.name, opts.strategy, venue.positions())
    if mismatches:
        print("El ledger NO cuadra con el venue:\n  " + "\n  ".join(mismatches))
    else:
        print("El ledger cuadra con el venue.")
    if opts.lift_kill_switch:
        if mismatches:
            print("Kill switch NO levantado.")
            return 1
        lift_kill_switch(engine, strategy_id=opts.strategy, actor=opts.actor,
                         reason="comisión en especie registrada (D-015); el ledger cuadra")
        print(f"Kill switch de {opts.strategy} levantado por {opts.actor}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
