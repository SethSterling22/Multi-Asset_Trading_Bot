"""El router de órdenes: el ÚNICO camino por el que una orden llega a un venue.

Por cada orden, en este orden y sin excepciones:
  1. kill switch de la estrategia (fail-closed, risk/breakers.py);
  2. checks pre-trade contra el risk floor, IN-PROCESS (CLAUDE.md §1.6);
  3. breakers de tasa de órdenes, horizonte mínimo y turnover (risk floor);
  4. write-ahead intent: INSERT 'PENDING_NEW' + COMMIT ANTES de la red (§1.4);
  5. envío con client_order_id;
  6. timeout o 5xx: se consulta por client_order_id; NUNCA se reenvía a ciegas.
     Si tampoco se puede consultar, la orden queda UNKNOWN y la reconciliación
     decide (y activa el kill switch si no cuadra).
  7. con la orden terminada, el fill va al ledger (idempotente).

Un rechazo en 1–3 no escribe nada en `orders` y deja un log con su reason.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from brokers.base import (
    OrderStatus,
    SpotVenue,
    VenueOrder,
    VenueRejectedError,
    VenueUnavailableError,
    normalize_symbol,
)
from core import log
from core.ledger.fills import AssetType, Fill, Side, insert_fill
from execution.idempotency import (
    OrderIntent,
    get_order,
    recent_order_times,
    record_intent,
    update_order,
)
from risk.breakers import KillSwitch, min_hold, order_rate, turnover
from risk.pretrade import ProposedState, check_order
from risk.verdict import Rejected, Verdict

POLL_SECONDS = 0.5
POLL_MAX_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class RouteResult:
    client_order_id: str
    status: OrderStatus | None          # None = rechazada antes de escribir nada
    reason: str | None = None
    order: VenueOrder | None = None


def fill_from(symbol: str, asset_type: AssetType, strategy_id: str, side: str,
              decision_price: Decimal | None, venue: str, order: VenueOrder,
              fallback_ts: datetime) -> Fill:
    """El fill agregado de una orden terminada, neto de comisiones (DECISIONS.md D-015).

    Una comisión en especie reduce lo que llega a la cuenta: el fill registra esa
    cantidad neta, y la comisión en dólares es su valor al precio del fill. El flujo de
    caja queda igual a lo pagado de verdad (filled_qty · price).
    """
    if order.filled_avg_price is None:
        raise ValueError(f"{order.client_order_id}: orden con qty llena y sin precio medio")
    qty = order.filled_qty - order.fee_qty
    if qty < 0:
        raise ValueError(f"{order.client_order_id}: comisión mayor que la cantidad llena")
    return Fill(
        ts=order.filled_at or fallback_ts, venue=venue, symbol=symbol, asset_type=asset_type,
        side=Side(side), qty=qty, price=order.filled_avg_price,
        commission=order.fee_qty * order.filled_avg_price + order.fee_usd,
        decision_price=decision_price, strategy_id=strategy_id,
        venue_fill_id=order.venue_order_id, client_order_id=order.client_order_id,
    )


class OrderRouter:
    def __init__(
        self,
        engine: Engine,
        venue: SpotVenue,
        kill_switch: KillSwitch,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        logger: Any | None = None,
    ) -> None:
        self._engine = engine
        self._venue = venue
        self._kill_switch = kill_switch
        self._clock = clock
        self._sleep = sleep
        self._log = logger if logger is not None else log.get_logger()

    # -- registro --------------------------------------------------------------
    def _reject(self, intent: OrderIntent, verdict: Rejected) -> RouteResult:
        log.guard_rejected(self._log, strategy_id=intent.strategy_id, symbol=intent.symbol,
                           action=f"order_{intent.side}", reason=verdict.code,
                           detail=verdict.detail, client_order_id=intent.client_order_id)
        return RouteResult(intent.client_order_id, None, verdict.code)

    # -- el camino de una orden --------------------------------------------------
    def submit(self, intent: OrderIntent, proposed: ProposedState) -> RouteResult:
        now = self._clock()
        gates: list[Verdict] = [self._kill_switch.check(intent.strategy_id),
                                check_order(proposed)]
        with self._engine.connect() as conn:
            times = recent_order_times(conn, venue=self._venue.name,
                                       since=now - timedelta(days=1))
            fills = conn.execute(text("""
                SELECT ts, symbol, side FROM fills
                WHERE strategy_id = :s AND ts > :since
            """), {"s": intent.strategy_id, "since": now - timedelta(days=365)}).all()
        def same(symbol: str) -> bool:
            return normalize_symbol(symbol) == normalize_symbol(intent.symbol)

        gates += [
            order_rate(times, now),
            min_hold(intent.side, [(f.ts, f.side) for f in fills if same(f.symbol)], now),
            turnover(intent.strategy_id, [f.ts for f in fills], now),
        ]
        for verdict in gates:
            if isinstance(verdict, Rejected):
                return self._reject(intent, verdict)

        with self._engine.begin() as conn:                      # 4. INSERT + COMMIT
            fresh = record_intent(conn, intent)
        if not fresh:
            with self._engine.connect() as conn:
                existing = get_order(conn, intent.client_order_id)
            self._log.warning("order_duplicate", strategy_id=intent.strategy_id,
                              symbol=intent.symbol, action="skip",
                              reason="client_order_id ya registrado: no se reenvía")
            return RouteResult(intent.client_order_id,
                               existing.status if existing else None, "duplicate")

        try:                                                    # 5. la red
            order = self._venue.submit_market_order(
                client_order_id=intent.client_order_id, symbol=intent.symbol,
                side=intent.side, qty=intent.qty)
        except VenueRejectedError as exc:
            with self._engine.begin() as conn:
                update_order(conn, intent.client_order_id, status=OrderStatus.REJECTED,
                             reject_reason=str(exc)[:500])
            self._log.warning("order_rejected_by_venue", strategy_id=intent.strategy_id,
                              symbol=intent.symbol, action=f"order_{intent.side}",
                              reason=str(exc)[:200])
            return RouteResult(intent.client_order_id, OrderStatus.REJECTED, str(exc))
        except VenueUnavailableError as exc:                    # 6. nunca reenviar
            self._log.warning("order_ack_unknown", strategy_id=intent.strategy_id,
                              symbol=intent.symbol, action="lookup", reason=str(exc)[:200])
            found = self._lookup(intent)
            if found is None:
                with self._engine.begin() as conn:
                    update_order(conn, intent.client_order_id, status=OrderStatus.UNKNOWN)
                return RouteResult(intent.client_order_id, OrderStatus.UNKNOWN,
                                   "sin confirmación del venue: la reconciliación decide")
            order = found

        order = self._wait_until_terminal(order)
        self.apply(intent.symbol, intent.side, intent.decision_price, intent.asset_type,
                   intent.strategy_id, order)
        return RouteResult(intent.client_order_id, order.status, order=order)

    def _lookup(self, intent: OrderIntent) -> VenueOrder | None:
        try:
            return self._venue.get_order(intent.client_order_id)
        except VenueUnavailableError:
            return None

    def _wait_until_terminal(self, order: VenueOrder) -> VenueOrder:
        """Una orden market de cripto se llena en segundos; si no, la recoge la reconciliación."""
        waited = 0.0
        while not order.status.is_terminal and waited < POLL_MAX_SECONDS:
            self._sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            try:
                latest = self._venue.get_order(order.client_order_id)
            except VenueUnavailableError:
                break
            if latest is None:
                break
            order = latest
        return order

    def apply(self, symbol: str, side: str, decision_price: Decimal | None,
              asset_type: AssetType, strategy_id: str, order: VenueOrder) -> None:
        """Estado de la orden y, si ha terminado con algo lleno, su fill al ledger."""
        with self._engine.begin() as conn:
            update_order(conn, order.client_order_id, status=order.status,
                         venue_order_id=order.venue_order_id)
            if order.status.is_terminal and order.filled_qty > 0:
                insert_fill(conn, fill_from(symbol, asset_type, strategy_id, side,
                                            decision_price, self._venue.name, order,
                                            self._clock()))
