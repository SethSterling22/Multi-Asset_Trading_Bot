"""El ledger de fills: el tipo `Fill` y su escritura idempotente.

`fills` es append-only (CLAUDE.md §1.3). Todo lo demás —posición, P&L realizado,
equity— se deriva de aquí. Los importes son `Decimal`, nunca `float`: el P&L
tiene que cuadrar al centavo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"
    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"

    @property
    def sign(self) -> int:
        """+1 si aumenta la posición larga, −1 si la reduce o abre un corto."""
        return 1 if self.value.startswith("buy") else -1


class AssetType(StrEnum):
    STOCK = "stock"
    OPTION = "option"
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERP = "crypto_perp"
    FUTURE = "future"


class FillConflictError(RuntimeError):
    """Un fill con el mismo (venue, venue_fill_id) ya existe con otro contenido."""


@dataclass(frozen=True, slots=True)
class Fill:
    ts: datetime
    venue: str
    symbol: str
    asset_type: AssetType
    side: Side
    qty: Decimal
    price: Decimal
    strategy_id: str
    # Obligatorio: es la clave de idempotencia. Los fills sin id del venue
    # (funding, paper) llevan uno determinista.
    venue_fill_id: str
    multiplier: Decimal = Decimal(1)
    commission: Decimal = Decimal(0)
    funding_payment: Decimal = Decimal(0)   # con signo: positivo = cobrado
    decision_price: Decimal | None = None
    decision_id: UUID | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("Fill.ts debe llevar zona horaria")
        for name in ("qty", "price", "multiplier", "commission", "funding_payment"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"Fill.{name} debe ser Decimal, no float")
        if self.decision_price is not None and not isinstance(self.decision_price, Decimal):
            raise TypeError("Fill.decision_price debe ser Decimal")
        if self.qty < 0:
            raise ValueError("Fill.qty >= 0: el signo va en side")
        if self.price < 0:
            raise ValueError("Fill.price >= 0")
        if self.multiplier <= 0:
            raise ValueError("Fill.multiplier > 0")
        if self.commission < 0:
            raise ValueError("Fill.commission >= 0: es un coste y se resta")
        if not self.venue_fill_id:
            raise ValueError("Fill.venue_fill_id es obligatorio: es la clave de idempotencia")

    @property
    def signed_qty(self) -> Decimal:
        return self.qty * self.side.sign


_COLUMNS = tuple(f.name for f in fields(Fill))

_INSERT = text(f"""
    INSERT INTO fills ({", ".join(_COLUMNS)})
    VALUES ({", ".join(
        "CAST(:asset_type AS asset_type)" if c == "asset_type" else f":{c}" for c in _COLUMNS
    )})
    ON CONFLICT (venue, venue_fill_id) DO NOTHING
    RETURNING id
""")  # noqa: S608 — las columnas salen del dataclass, no de input externo

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM fills"  # noqa: S608


def _params(fill: Fill) -> dict[str, Any]:
    return {c: getattr(fill, c) for c in _COLUMNS}


def _from_row(row: Any) -> Fill:
    values = dict(row._mapping)
    values["asset_type"] = AssetType(values["asset_type"])
    values["side"] = Side(values["side"])
    return Fill(**values)


def insert_fill(conn: Connection, fill: Fill) -> bool:
    """Inserta un fill. Devuelve True si se escribió y False si ya existía.

    Reinsertar el mismo fill es un no-op (gap-fill, reintentos). Reinsertar el
    mismo (venue, venue_fill_id) con OTRO contenido es un bug o un venue que
    reescribe la historia, y lanza FillConflictError en lugar de ignorarlo.
    """
    inserted = conn.execute(_INSERT, _params(fill)).first()
    if inserted is not None:
        return True
    existing = conn.execute(
        text(f"{_SELECT} WHERE venue = :venue AND venue_fill_id = :venue_fill_id"),
        {"venue": fill.venue, "venue_fill_id": fill.venue_fill_id},
    ).one()
    if _from_row(existing) != fill:
        raise FillConflictError(
            f"fill {fill.venue}/{fill.venue_fill_id} ya existe con otro contenido"
        )
    return False


def fetch_fills(conn: Connection, strategy_id: str | None = None) -> list[Fill]:
    """Fills en el orden en que se replayan: por ts, y por id a igual ts."""
    where = "WHERE strategy_id = :strategy_id" if strategy_id is not None else ""
    rows = conn.execute(
        text(f"{_SELECT} {where} ORDER BY ts, id"), {"strategy_id": strategy_id}
    )
    return [_from_row(row) for row in rows]


def insert_fills(conn: Connection, fills: Sequence[Fill]) -> int:
    """Inserta varios fills; devuelve cuántos eran nuevos."""
    return sum(insert_fill(conn, f) for f in fills)
