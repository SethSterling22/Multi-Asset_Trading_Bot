"""Registro de operaciones manuales desde el dashboard (DECISIONS.md D-017).

Es el ÚNICO camino de escritura del dashboard, y va por una conexión aparte con el rol
`manual_journal`: Postgres solo le deja INSERTAR filas del sleeve `manual` en `orders`
y `fills` (row-level security, migración 0005). El resto del dashboard sigue con su
conexión de solo lectura.

Las operaciones manuales se hacen en otra cuenta o broker: el sistema las registra para
el diario y las estadísticas, sin reconciliarlas con ningún venue. Cada una es una
orden FILLED con su motivo y un fill; `fills` es append-only, así que un error se
corrige con la operación contraria, no editando.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from core.ledger.fills import AssetType, Fill, Side, insert_fill
from core.risk_floor import floor_hash

STRATEGY_ID = "manual"
ZONES = ("America/Puerto_Rico", "America/New_York", "UTC")
OPTION_SIDES = (Side.SELL_TO_OPEN, Side.BUY_TO_CLOSE, Side.BUY_TO_OPEN, Side.SELL_TO_CLOSE)
PLAIN_SIDES = (Side.BUY, Side.SELL)
_SYMBOL = re.compile(r"^[A-Z0-9./\-]{1,40}$")
_VENUE = re.compile(r"^[a-z0-9_\-]{1,40}$")
MAX_REASON = 500


@dataclass(frozen=True, slots=True)
class ManualEntry:
    ts: datetime
    venue: str
    symbol: str
    asset_type: AssetType
    side: Side
    qty: Decimal
    price: Decimal
    commission: Decimal
    multiplier: Decimal
    reason: str

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price * self.multiplier

    def as_form(self, zone: str) -> dict[str, str]:
        """Los campos para reenviarlos en la confirmación, tal como se validaron."""
        local = self.ts.astimezone(ZoneInfo(zone))
        return {"fecha": f"{local:%Y-%m-%d}", "hora": f"{local:%H:%M}", "zona": zone,
                "cuenta": self.venue, "simbolo": self.symbol, "tipo": self.asset_type.value,
                "lado": self.side.value, "cantidad": str(self.qty), "precio": str(self.price),
                "comision": str(self.commission), "multiplicador": str(self.multiplier),
                "motivo": self.reason}


def _dec(form: Mapping[str, str], name: str, label: str, errors: list[str],
         default: str | None = None) -> Decimal | None:
    raw = (form.get(name) or "").strip().replace(",", "") or (default or "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        errors.append(f"{label}: no es un número")
        return None
    if not value.is_finite():
        errors.append(f"{label}: no es un número")
        return None
    return value


def parse(form: Mapping[str, str], *, now: datetime) -> ManualEntry | list[str]:
    """La entrada validada, o la lista de errores para enseñarlos en el formulario."""
    errors: list[str] = []
    zone = form.get("zona") or ZONES[0]
    if zone not in ZONES:
        errors.append("zona horaria desconocida")
        zone = "UTC"
    ts: datetime | None = None
    try:
        local = datetime.fromisoformat(f"{form.get('fecha', '')}T{form.get('hora', '')}")
        ts = local.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)
        if ts > now + timedelta(minutes=5):
            errors.append("la fecha y hora están en el futuro")
    except ValueError:
        errors.append("fecha u hora inválidas")

    venue = (form.get("cuenta") or "").strip().lower()
    if not _VENUE.match(venue):
        errors.append("cuenta/broker: letras, números, _ o -, hasta 40 (p.ej. webull)")
    symbol = (form.get("simbolo") or "").strip().upper()
    if not _SYMBOL.match(symbol):
        errors.append("símbolo: letras, números y . / -, hasta 40")
    try:
        asset_type = AssetType(form.get("tipo") or "")
    except ValueError:
        errors.append("tipo de activo desconocido")
        asset_type = AssetType.STOCK
    try:
        side = Side(form.get("lado") or "")
    except ValueError:
        errors.append("lado desconocido")
        side = Side.BUY
    allowed = OPTION_SIDES if asset_type is AssetType.OPTION else PLAIN_SIDES
    if side not in allowed:
        errors.append(f"con {asset_type.value}, el lado es uno de: "
                      + ", ".join(s.value for s in allowed))

    qty = _dec(form, "cantidad", "cantidad", errors)
    price = _dec(form, "precio", "precio", errors)
    commission = _dec(form, "comision", "comisión", errors, default="0")
    multiplier = _dec(form, "multiplicador", "multiplicador", errors,
                      default="100" if asset_type is AssetType.OPTION else "1")
    if qty is not None and qty <= 0:
        errors.append("cantidad > 0")
    if price is not None and price < 0:
        errors.append("precio >= 0")
    if commission is not None and commission < 0:
        errors.append("comisión >= 0: es un coste")
    if multiplier is not None and multiplier <= 0:
        errors.append("multiplicador > 0")
    reason = (form.get("motivo") or "").strip()
    if not reason:
        errors.append("el motivo es obligatorio: es lo que el diario guarda de la decisión")
    elif len(reason) > MAX_REASON:
        errors.append(f"motivo: como mucho {MAX_REASON} caracteres")

    if (errors or ts is None or qty is None or price is None or commission is None
            or multiplier is None):
        return errors or ["datos incompletos"]
    return ManualEntry(ts, venue, symbol, asset_type, side, qty, price, commission,
                       multiplier, reason)


def record(engine: Engine, entry: ManualEntry) -> str:
    """Escribe la orden y su fill con el rol `manual_journal`. Devuelve el id de la orden."""
    coid = f"manual-{uuid4().hex[:16]}"
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO orders (client_order_id, ts_intent, ts_ack, venue, symbol, asset_type,
                                side, qty, order_type, status, strategy_id, risk_floor_hash,
                                reason)
            VALUES (:coid, :ts, :ts, :venue, :symbol, CAST(:asset_type AS asset_type), :side,
                    :qty, 'manual', 'FILLED', :sid, decode(:h, 'hex'), :reason)
        """), {"coid": coid, "ts": entry.ts, "venue": entry.venue, "symbol": entry.symbol,
               "asset_type": entry.asset_type.value, "side": entry.side.value, "qty": entry.qty,
               "sid": STRATEGY_ID, "h": floor_hash(), "reason": entry.reason})
        insert_fill(conn, Fill(
            ts=entry.ts, venue=entry.venue, symbol=entry.symbol, asset_type=entry.asset_type,
            side=entry.side, qty=entry.qty, price=entry.price, commission=entry.commission,
            multiplier=entry.multiplier, strategy_id=STRATEGY_ID, venue_fill_id=coid,
            client_order_id=coid))
    return coid


def same_origin(headers: Mapping[str, str]) -> bool:
    """El POST viene de una página de este mismo dashboard: Origin (o, si falta, Referer)
    con el mismo host que la petición. Sin ninguno de los dos, se rechaza."""
    host = headers.get("host", "")
    source = headers.get("origin") or headers.get("referer") or ""
    m = re.match(r"^https?://([^/]+)", source)
    return bool(host) and m is not None and m.group(1) == host
