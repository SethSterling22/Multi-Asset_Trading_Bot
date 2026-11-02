"""El adaptador de Alpaca Crypto sin red: traducción de errores, estados y el
endurecimiento del cliente (sin reintentos del SDK, con timeout)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderStatus as AlpacaStatus

from brokers.alpaca_crypto import (
    HTTP_TIMEOUT_SECONDS,
    AlpacaCryptoVenue,
    _translate,
    harden,
    pair,
    to_venue_order,
)
from brokers.base import OrderStatus, VenueRejectedError, VenueUnavailableError


def api_error(status: int, message: str, code: int = 40010000) -> APIError:
    response = SimpleNamespace(status_code=status)
    return APIError(json.dumps({"code": code, "message": message}),
                    SimpleNamespace(response=response, request=None))


@pytest.mark.parametrize("raw", ["BTC", "btc", "BTCUSD", "BTC/USD", "BTC-USD"])
def test_pair(raw: str) -> None:
    assert pair(raw) == "BTC/USD"


@pytest.mark.parametrize(("exc", "expected"), [
    (api_error(500, "internal"), VenueUnavailableError),
    (api_error(503, "unavailable"), VenueUnavailableError),
    (api_error(429, "rate limit"), VenueUnavailableError),
    (api_error(422, "client_order_id must be unique"), VenueUnavailableError),
    (api_error(403, "insufficient balance for USD"), VenueRejectedError),
    (api_error(422, "qty must be >= 0.0001"), VenueRejectedError),
    (requests.ReadTimeout("read timed out"), VenueUnavailableError),
    (requests.ConnectionError("reset"), VenueUnavailableError),
])
def test_error_translation(exc: Exception, expected: type[Exception]) -> None:
    assert isinstance(_translate(exc), expected)


def test_duplicate_id_means_it_arrived() -> None:
    # Un id repetido NO es un rechazo: la orden llegó antes y hay que consultarla.
    assert isinstance(_translate(api_error(422, "client_order_id must be unique")),
                      VenueUnavailableError)


def order(status: AlpacaStatus, filled: str = "0", price: str | None = None) -> Any:
    return SimpleNamespace(client_order_id="c-1", id="uuid-1", status=status,
                           filled_qty=filled, filled_avg_price=price,
                           filled_at=datetime(2026, 11, 2, tzinfo=UTC) if price else None)


@pytest.mark.parametrize(("raw", "ours"), [
    (AlpacaStatus.PENDING_NEW, OrderStatus.NEW),
    (AlpacaStatus.ACCEPTED, OrderStatus.NEW),
    (AlpacaStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED),
    (AlpacaStatus.FILLED, OrderStatus.FILLED),
    (AlpacaStatus.CANCELED, OrderStatus.CANCELED),
    (AlpacaStatus.REJECTED, OrderStatus.REJECTED),
    (AlpacaStatus.PENDING_CANCEL, OrderStatus.UNKNOWN),
    (AlpacaStatus.HELD, OrderStatus.UNKNOWN),
])
def test_status_mapping(raw: AlpacaStatus, ours: OrderStatus) -> None:
    assert to_venue_order(order(raw)).status is ours


def test_filled_order_carries_venue_time_and_decimals() -> None:
    o = to_venue_order(order(AlpacaStatus.FILLED, "0.0123", "60000.5"))
    assert (o.filled_qty, o.filled_avg_price) == (D("0.0123"), D("60000.5"))
    assert o.filled_at == datetime(2026, 11, 2, tzinfo=UTC)
    assert o.venue_order_id == "uuid-1"


def test_harden_disables_sdk_retries_and_sets_a_timeout() -> None:
    client = harden(TradingClient("k", "s", paper=True))
    assert client._retry == 0
    seen: dict[str, Any] = {}

    def fake_send(self: Any, request: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise requests.ReadTimeout("stop")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(requests.Session, "send", fake_send)
        with pytest.raises(requests.ReadTimeout):
            client._session.request("GET", "https://paper-api.alpaca.markets/v2/clock")
    assert seen["timeout"] == HTTP_TIMEOUT_SECONDS


def test_harden_refuses_an_sdk_it_does_not_recognise() -> None:
    with pytest.raises(RuntimeError, match="alpaca-py"):
        harden(object())


def test_live_money_is_not_a_constructor_flag() -> None:
    with pytest.raises(ValueError, match="paper"):
        AlpacaCryptoVenue("k", "s", paper=False)
