"""Alpaca Crypto (spot) detrás del port `SpotVenue`. Solo paper hasta la fase 5.

Dos cosas de alpaca-py que este adaptador desactiva a propósito:

- **Reintentos del SDK.** `RESTClient` reintenta 429/504 por su cuenta, también en
  el POST de una orden: eso es exactamente el "reenviar a ciegas" que prohíbe
  CLAUDE.md §1.4. `retry_attempts=0` no lo desactiva (el SDK trata 0 como "usa el
  valor por defecto"), así que se pone `_retry = 0` a mano y un test comprueba que
  el atributo sigue existiendo en la versión instalada.
- **Sin timeout.** El SDK llama a `requests` sin timeout: un socket colgado
  bloquearía el router para siempre. Se envuelve la sesión con uno.

Errores:
- timeout, red, 429, 5xx -> `VenueUnavailableError` (puede haber llegado);
- `client_order_id` repetido -> `VenueUnavailableError`: SÍ llegó antes, y el router
  la consulta por id en lugar de darla por rechazada;
- cualquier otro 4xx -> `VenueRejectedError`.

Comprobado contra paper el 2026-11-02: `get_order_by_client_id` de un id que no
existe da 404 con código 40410000; las posiciones vienen sin "/" (BTCUSD).

Comisión (DECISIONS.md D-015): en una compra, Alpaca cobra el 0.25 % de CADA
ejecución en el activo recibido, redondeado hacia arriba a 1e-9, y la orden sigue
informando la cantidad bruta. Comprobado en paper el 2026-11-02 con 4 ejecuciones de
2 órdenes: la cantidad neta calculada así coincide con la posición al último dígito.
Por eso la comisión sale de las ejecuciones (actividades FILL), no del agregado de la
orden. En una venta la comisión va en dólares; eso aún NO está observado.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, TimeInForce
from alpaca.trading.enums import OrderStatus as AlpacaStatus
from alpaca.trading.requests import MarketOrderRequest

from brokers.base import (
    AssetRules,
    Execution,
    OrderStatus,
    Quote,
    VenueOrder,
    VenueRejectedError,
    VenueUnavailableError,
    normalize_symbol,
)

VENUE = "alpaca"
# Tier 1 (< 100k $ de volumen a 30 días), taker: las órdenes market siempre lo son.
# Si el tier cambia, la reconciliación lo detecta (cantidades que no cuadran) y para.
TAKER_FEE_RATE = Decimal("0.0025")
FEE_QTY_INCREMENT = Decimal("1e-9")
QUOTE_CURRENCY = "USD"
HTTP_TIMEOUT_SECONDS = 10.0
NOT_FOUND_CODE = 40410000

# Lo que no está aquí (pending_cancel, held, suspended…) es UNKNOWN: la orden
# existe pero su resultado no está claro, y la reconciliación vuelve a mirarla.
_STATUS = {
    AlpacaStatus.PENDING_NEW: OrderStatus.NEW,
    AlpacaStatus.ACCEPTED: OrderStatus.NEW,
    AlpacaStatus.NEW: OrderStatus.NEW,
    AlpacaStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaStatus.FILLED: OrderStatus.FILLED,
    AlpacaStatus.CANCELED: OrderStatus.CANCELED,
    AlpacaStatus.EXPIRED: OrderStatus.EXPIRED,
    AlpacaStatus.REJECTED: OrderStatus.REJECTED,
    AlpacaStatus.DONE_FOR_DAY: OrderStatus.CANCELED,
}


def pair(symbol: str) -> str:
    """'BTC', 'BTCUSD' o 'BTC/USD' -> 'BTC/USD', el formato de órdenes de Alpaca."""
    s = normalize_symbol(symbol)
    base = s[: -len(QUOTE_CURRENCY)] if s.endswith(QUOTE_CURRENCY) and s != QUOTE_CURRENCY else s
    return f"{base}/{QUOTE_CURRENCY}"


def _api_code(exc: APIError) -> int | None:
    try:
        return int(exc.code)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _is_duplicate_id(exc: APIError) -> bool:
    return "client_order_id" in str(exc).lower()


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, requests.RequestException) and not isinstance(exc, requests.HTTPError):
        return VenueUnavailableError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, APIError):
        status = exc.status_code
        if status is None or status == 429 or status >= 500 or _is_duplicate_id(exc):
            return VenueUnavailableError(f"HTTP {status}: {exc}")
        return VenueRejectedError(f"HTTP {status}: {exc}")
    return exc


def _call[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except (APIError, requests.RequestException) as exc:
        raise _translate(exc) from exc


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def to_venue_order(o: Any) -> VenueOrder:
    return VenueOrder(
        client_order_id=o.client_order_id,
        venue_order_id=str(o.id),
        status=_STATUS.get(o.status, OrderStatus.UNKNOWN),
        filled_qty=_dec(o.filled_qty),
        filled_avg_price=None if o.filled_avg_price is None else _dec(o.filled_avg_price),
        filled_at=o.filled_at,
    )


def fee_in_kind(executions: list[Execution]) -> Decimal:
    """Lo que Alpaca se queda del activo comprado: 0.25 % de cada ejecución, hacia arriba."""
    return sum(((e.qty * TAKER_FEE_RATE).quantize(FEE_QTY_INCREMENT, rounding=ROUND_CEILING)
                for e in executions), Decimal(0))


def fee_in_usd(executions: list[Execution]) -> Decimal:
    """Venta: la comisión va en dólares. Sin observar todavía (D-015)."""
    return sum((e.qty * e.price * TAKER_FEE_RATE for e in executions), Decimal(0))


def harden(client: Any, timeout: float = HTTP_TIMEOUT_SECONDS) -> Any:
    """Sin reintentos del SDK y con timeout en cada petición. Ver el docstring del módulo."""
    if not hasattr(client, "_retry") or not hasattr(client, "_session"):
        raise RuntimeError("alpaca-py cambió: no se pueden desactivar sus reintentos")
    client._retry = 0
    client._session.request = functools.partial(client._session.request, timeout=timeout)
    return client


class AlpacaCryptoVenue:
    name = VENUE

    def __init__(self, api_key: str, secret_key: str, *, paper: bool = True) -> None:
        if not paper:
            # Dinero real es la fase 5, con su gate. No es un flag que se cambie aquí.
            raise ValueError("AlpacaCryptoVenue solo opera en paper hasta la fase 5")
        self._trading = harden(TradingClient(api_key, secret_key, paper=True))
        self._data = harden(CryptoHistoricalDataClient(api_key, secret_key))

    def submit_market_order(self, *, client_order_id: str, symbol: str, side: str,
                            qty: Decimal) -> VenueOrder:
        request = MarketOrderRequest(
            symbol=pair(symbol), qty=float(qty), client_order_id=client_order_id,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        )
        return self._with_fees(_call(lambda: self._trading.submit_order(request)))

    def get_order(self, client_order_id: str) -> VenueOrder | None:
        try:
            order = self._trading.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if exc.status_code == 404 or _api_code(exc) == NOT_FOUND_CODE:
                return None
            raise _translate(exc) from exc
        except requests.RequestException as exc:
            raise _translate(exc) from exc
        return self._with_fees(order)

    def _with_fees(self, raw: Any) -> VenueOrder:
        """La orden con sus comisiones, calculadas de las ejecuciones cuando ha terminado."""
        order = to_venue_order(raw)
        if not order.status.is_terminal or order.filled_qty <= 0:
            return order
        execs = self.executions(order.venue_order_id, raw=raw)
        if raw.side == OrderSide.BUY:
            return replace(order, fee_qty=fee_in_kind(execs))
        return replace(order, fee_usd=fee_in_usd(execs))

    def executions(self, venue_order_id: str, *, raw: Any | None = None) -> list[Execution]:
        """Las ejecuciones de una orden (actividades FILL). Si no suman la cantidad llena,
        el venue aún no las ha publicado todas: VenueUnavailableError, y la reconciliación
        vuelve a mirarla en el siguiente ciclo."""
        o = raw if raw is not None else _call(lambda: self._trading.get_order_by_id(venue_order_id))
        start = (o.submitted_at or o.created_at) - timedelta(minutes=1)
        end = (o.filled_at or o.updated_at or start) + timedelta(minutes=1)
        params: dict[str, Any] = {"after": start.isoformat(), "until": end.isoformat(),
                                  "direction": "asc", "page_size": 100}
        found: list[Execution] = []
        for _ in range(20):                              # 2000 ejecuciones como mucho
            page = _call(functools.partial(self._trading.get, "/account/activities/FILL",
                                           dict(params)))
            if not page:
                break
            found += [Execution(_dec(a["qty"]), _dec(a["price"])) for a in page
                      if a.get("order_id") == str(o.id)]
            if len(page) < params["page_size"]:
                break
            params["page_token"] = page[-1]["id"]
        if sum((e.qty for e in found), Decimal(0)) != _dec(o.filled_qty):
            raise VenueUnavailableError(
                f"orden {o.id}: ejecuciones publicadas ({len(found)}) no suman {o.filled_qty}")
        return found

    def positions(self) -> dict[str, Decimal]:
        """Solo cripto: la cuenta paper también tiene las posiciones de la Wheel."""
        rows = _call(self._trading.get_all_positions)
        return {normalize_symbol(p.symbol): _dec(p.qty) for p in rows
                if p.asset_class == AssetClass.CRYPTO}

    def cash(self) -> Decimal:
        return _dec(_call(self._trading.get_account).cash)

    def quote(self, symbol: str) -> Quote:
        sym = pair(symbol)
        quotes = _call(lambda: self._data.get_crypto_latest_quote(
            CryptoLatestQuoteRequest(symbol_or_symbols=sym)))
        q = quotes[sym]
        return Quote(bid=_dec(q.bid_price), ask=_dec(q.ask_price))

    def rules(self, symbol: str) -> AssetRules:
        asset = _call(lambda: self._trading.get_asset(pair(symbol)))
        return AssetRules(min_order_qty=_dec(asset.min_order_size),
                          qty_increment=_dec(asset.min_trade_increment))
