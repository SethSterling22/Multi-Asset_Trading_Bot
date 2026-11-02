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
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from decimal import Decimal
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
    OrderStatus,
    Quote,
    VenueOrder,
    VenueRejectedError,
    VenueUnavailableError,
    normalize_symbol,
)

VENUE = "alpaca"
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
        return to_venue_order(_call(lambda: self._trading.submit_order(request)))

    def get_order(self, client_order_id: str) -> VenueOrder | None:
        try:
            order = self._trading.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if exc.status_code == 404 or _api_code(exc) == NOT_FOUND_CODE:
                return None
            raise _translate(exc) from exc
        except requests.RequestException as exc:
            raise _translate(exc) from exc
        return to_venue_order(order)

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
