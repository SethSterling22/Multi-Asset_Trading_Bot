"""`client_order_id` determinista y el write-ahead intent (CLAUDE.md §1.4).

El orden no es negociable:
    1. INSERT INTO orders (client_order_id, …, status='PENDING_NEW')
    2. COMMIT                    <- antes de tocar la red
    3. POST de la orden con ese client_order_id
    4. timeout o 5xx: NUNCA reenviar a ciegas; consultar por client_order_id

El id es `{strategy}-{symbol}-{bar}-{attempt}`: la misma decisión en el mismo ciclo
produce el mismo id, así que un reinicio que vuelve a planificar no duplica la orden,
y el venue la rechazaría igualmente por id repetido.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, text

from brokers.base import OPEN_STATUSES, OrderStatus, normalize_symbol
from core.ledger.fills import AssetType

# Límite conservador: el máximo real de cada venue no está verificado, y 48 caracteres
# caben en cualquiera de los que se han mirado.
MAX_CLIENT_ORDER_ID = 48
_ALLOWED = re.compile(r"^[A-Za-z0-9_-]+$")


def client_order_id(strategy_id: str, symbol: str, bar_id: int, attempt: int = 1) -> str:
    if bar_id < 0 or attempt < 1:
        raise ValueError("bar_id >= 0 y attempt >= 1")
    coid = f"{strategy_id}-{normalize_symbol(symbol)}-{bar_id}-{attempt}"
    if len(coid) > MAX_CLIENT_ORDER_ID or not _ALLOWED.match(coid):
        raise ValueError(f"client_order_id inválido: {coid!r}")
    return coid


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    venue: str
    symbol: str
    asset_type: AssetType
    side: str                       # 'buy' | 'sell'
    qty: Decimal
    strategy_id: str
    decision_price: Decimal
    risk_floor_hash: str            # hex de core.risk_floor.floor_hash()
    order_type: str = "market"

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError("side debe ser 'buy' o 'sell'")
        if self.qty <= 0:
            raise ValueError("qty > 0")


@dataclass(frozen=True, slots=True)
class StoredOrder:
    client_order_id: str
    venue: str
    symbol: str
    side: str
    qty: Decimal
    status: OrderStatus
    strategy_id: str
    decision_price: Decimal | None
    ts_intent: datetime
    venue_order_id: str | None


def record_intent(conn: Connection, intent: OrderIntent) -> bool:
    """Paso 1. Devuelve False si el id ya existía: esa orden NO se vuelve a enviar."""
    row = conn.execute(text("""
        INSERT INTO orders (client_order_id, venue, symbol, asset_type, side, qty, order_type,
                            status, strategy_id, decision_price, risk_floor_hash)
        VALUES (:coid, :venue, :symbol, CAST(:asset_type AS asset_type), :side, :qty,
                :order_type, 'PENDING_NEW', :strategy_id, :decision_price,
                decode(:risk_floor_hash, 'hex'))
        ON CONFLICT (client_order_id) DO NOTHING
        RETURNING client_order_id
    """), {"coid": intent.client_order_id, "venue": intent.venue, "symbol": intent.symbol,
           "asset_type": intent.asset_type.value, "side": intent.side, "qty": intent.qty,
           "order_type": intent.order_type, "strategy_id": intent.strategy_id,
           "decision_price": intent.decision_price,
           "risk_floor_hash": intent.risk_floor_hash}).first()
    return row is not None


def update_order(conn: Connection, client_order_id: str, *, status: OrderStatus,
                 venue_order_id: str | None = None, reject_reason: str | None = None) -> None:
    conn.execute(text("""
        UPDATE orders SET status = CAST(:status AS order_status),
               venue_order_id = COALESCE(:venue_order_id, venue_order_id),
               reject_reason = COALESCE(:reject_reason, reject_reason),
               ts_ack = CASE WHEN :acked THEN COALESCE(ts_ack, now()) ELSE ts_ack END
        WHERE client_order_id = :coid
    """), {"coid": client_order_id, "status": status.value, "venue_order_id": venue_order_id,
           "reject_reason": reject_reason,
           "acked": status not in (OrderStatus.PENDING_NEW, OrderStatus.UNKNOWN)})


def _from_row(r: object) -> StoredOrder:
    m = r._mapping  # type: ignore[attr-defined]
    return StoredOrder(
        client_order_id=m["client_order_id"], venue=m["venue"], symbol=m["symbol"],
        side=m["side"], qty=m["qty"], status=OrderStatus(m["status"]),
        strategy_id=m["strategy_id"], decision_price=m["decision_price"],
        ts_intent=m["ts_intent"], venue_order_id=m["venue_order_id"],
    )


_COLUMNS = ("client_order_id, venue, symbol, side, qty, status, strategy_id, decision_price,"
            " ts_intent, venue_order_id")


def get_order(conn: Connection, client_order_id: str) -> StoredOrder | None:
    r = conn.execute(text(f"SELECT {_COLUMNS} FROM orders WHERE client_order_id = :c"),  # noqa: S608
                     {"c": client_order_id}).first()
    return None if r is None else _from_row(r)


def open_orders(conn: Connection, *, venue: str, strategy_id: str) -> list[StoredOrder]:
    rows = conn.execute(text(f"""
        SELECT {_COLUMNS} FROM orders
        WHERE venue = :venue AND strategy_id = :strategy_id
          AND status = ANY(CAST(:statuses AS order_status[]))
        ORDER BY ts_intent
    """), {"venue": venue, "strategy_id": strategy_id,  # noqa: S608
           "statuses": [s.value for s in OPEN_STATUSES]})
    return [_from_row(r) for r in rows]


def recent_order_times(conn: Connection, *, venue: str, since: datetime) -> list[datetime]:
    """Para el breaker de tasa de órdenes: cuenta TODAS las intenciones, también las fallidas."""
    return [r[0] for r in conn.execute(text(
        "SELECT ts_intent FROM orders WHERE venue = :venue AND ts_intent >= :since"),
        {"venue": venue, "since": since})]
