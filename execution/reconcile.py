"""Reconciliación: gate de arranque y bucle periódico (informe §12.4, patrón 2).

1. Toda orden abierta en la base se consulta en el venue por `client_order_id`:
   - si existe, se actualiza y, si terminó, su fill entra al ledger (idempotente);
   - si no existe y estaba escrita (PENDING_NEW / UNKNOWN), es un mismatch: se
     escribió la intención pero no consta que llegase. No se reenvía.
2. La posición del ledger (vista `positions`, derivada de fills) se compara con la
   del venue. El venue manda sobre la posición; si no cuadran, es un mismatch.
3. Cada ejecución se registra en `reconciliation_runs`. Con mismatches, se activa el
   kill switch de la estrategia (fila `kill_switch_on` en `risk_events`): modo seguro
   hasta que una persona lo revise y lo levante. Nada se "arregla" solo.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from brokers.base import OrderStatus, SpotVenue, VenueUnavailableError, normalize_symbol
from core import log
from core.ledger.fills import AssetType
from core.ledger.positions import load_positions_view
from execution.idempotency import open_orders, update_order
from execution.router import OrderRouter

# Lo más fino que dan los venues mirados (incremento de 1e-9 en Alpaca cripto).
QTY_TOLERANCE = Decimal("1e-9")


@dataclass
class ReconcileReport:
    venue: str
    strategy_id: str
    mismatches: list[str] = field(default_factory=list)
    orders_resolved: int = 0
    unreachable: int = 0

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.unreachable


def position_mismatches(engine: Engine, venue_name: str, strategy_id: str,
                        remote: dict[str, Decimal]) -> list[str]:
    """Posición del ledger (vista `positions`) contra la del venue, símbolo a símbolo."""
    remote = {normalize_symbol(s): q for s, q in remote.items()}
    with engine.connect() as conn:
        ledger = {normalize_symbol(k.symbol): p.net_qty
                  for k, p in load_positions_view(conn).items()
                  if k.venue == venue_name and k.strategy_id == strategy_id}
    out = []
    for sym in sorted(set(ledger) | set(remote)):
        mine, theirs = ledger.get(sym, Decimal(0)), remote.get(sym, Decimal(0))
        if abs(mine - theirs) > QTY_TOLERANCE:
            out.append(f"{sym}: ledger {mine} ≠ venue {theirs}")
    return out


def _kill_switch_on(conn: Any, strategy_id: str) -> bool:
    kind = conn.execute(text("""
        SELECT kind FROM risk_events WHERE scope = :s
          AND kind IN ('kill_switch_on', 'kill_switch_off')
        ORDER BY ts DESC, id DESC LIMIT 1
    """), {"s": strategy_id}).scalar_one_or_none()
    return bool(kind == "kill_switch_on")


def reconcile(
    engine: Engine,
    venue: SpotVenue,
    router: OrderRouter,
    *,
    strategy_id: str,
    asset_type: AssetType = AssetType.CRYPTO_SPOT,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    logger: Any | None = None,
) -> ReconcileReport:
    logger = logger if logger is not None else log.get_logger()
    report = ReconcileReport(venue.name, strategy_id)

    # 1. Órdenes abiertas en la base contra el venue.
    with engine.connect() as conn:
        pending = open_orders(conn, venue=venue.name, strategy_id=strategy_id)
    for o in pending:
        try:
            remote = venue.get_order(o.client_order_id)
        except VenueUnavailableError:
            report.unreachable += 1
            continue
        if remote is None:
            report.mismatches.append(
                f"{o.client_order_id}: registrada como {o.status.value} y sin rastro en el venue")
            if o.status is not OrderStatus.UNKNOWN:
                with engine.begin() as conn:
                    update_order(conn, o.client_order_id, status=OrderStatus.UNKNOWN)
            continue
        router.apply(o.symbol, o.side, o.decision_price, asset_type, strategy_id, remote)
        report.orders_resolved += 1

    # 2. Posición del ledger contra la del venue.
    if not report.unreachable:
        try:
            remote_pos = venue.positions()
        except VenueUnavailableError:
            report.unreachable += 1
            remote_pos = None
        if remote_pos is not None:
            report.mismatches += position_mismatches(engine, venue.name, strategy_id, remote_pos)

    # 3. Registro y, si no cuadra, modo seguro.
    action = "safe_mode" if report.mismatches else "none"
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO reconciliation_runs (venue, mismatches, detail, action_taken)
            VALUES (:venue, :n, CAST(:detail AS jsonb), :action)
        """), {"venue": venue.name, "n": len(report.mismatches), "action": action,
               "detail": json.dumps({"strategy_id": strategy_id,
                                     "mismatches": report.mismatches,
                                     "orders_resolved": report.orders_resolved,
                                     "unreachable": report.unreachable,
                                     "at": clock().isoformat()})})
        if report.mismatches and not _kill_switch_on(conn, strategy_id):
            conn.execute(text("""
                INSERT INTO risk_events (kind, scope, reason, actor, metadata)
                VALUES ('kill_switch_on', :s, :reason, 'reconciler', CAST(:meta AS jsonb))
            """), {"s": strategy_id, "reason": "reconciliación: " + report.mismatches[0][:200],
                   "meta": json.dumps({"mismatches": report.mismatches})})
    if report.mismatches:
        logger.error("reconciliation_mismatch", strategy_id=strategy_id, action="safe_mode",
                     reason="; ".join(report.mismatches)[:500])
    return report
