"""El adaptador de Alpaca Crypto sin red: traducción de errores, estados y el
endurecimiento del cliente (sin reintentos del SDK, con timeout)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

import pytest

# CI corre los guards solo con el grupo `dev`, sin el SDK de Alpaca.
pytest.importorskip("alpaca")
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


# -- comisión en especie (D-015) ---------------------------------------------------
# Las ejecuciones reales de paper del 2026-11-02 y las posiciones que dejó Alpaca.
BTC_EXECUTIONS = [("0.000057245", "78056.71"), ("0.00012811", "78058.8"),
                  ("0.025432347", "78077")]
ETH_EXECUTIONS = [("0.876044135", "2283.76")]


def executions(rows: list[tuple[str, str]]) -> list[Any]:
    from brokers.base import Execution
    return [Execution(D(q), D(p)) for q, p in rows]


def test_fee_in_kind_reproduces_alpaca_to_the_last_digit() -> None:
    from brokers.alpaca_crypto import fee_in_kind
    btc, eth = executions(BTC_EXECUTIONS), executions(ETH_EXECUTIONS)
    assert sum(e.qty for e in btc) - fee_in_kind(btc) == D("0.025553656")   # la posición
    assert sum(e.qty for e in eth) - fee_in_kind(eth) == D("0.873854024")
    # Por ejecución, no sobre el agregado: el agregado se queda 1e-9 corto en BTC.
    assert fee_in_kind(btc) == D("0.000064046")


class StubTrading:
    def __init__(self, pages: list[list[dict[str, str]]]) -> None:
        self.pages, self.calls = pages, []

    def get(self, path: str, params: dict[str, Any]) -> list[dict[str, str]]:
        self.calls.append(params)
        return self.pages[len(self.calls) - 1] if len(self.calls) <= len(self.pages) else []


def raw_order(side: Any, filled: str = "0.025617702") -> Any:
    return SimpleNamespace(
        id="cc5cf991", client_order_id="c-1", status=AlpacaStatus.FILLED, side=side,
        filled_qty=filled, filled_avg_price="78076.86",
        submitted_at=datetime(2026, 11, 2, 6, 38, tzinfo=UTC),
        created_at=datetime(2026, 11, 2, 6, 38, tzinfo=UTC),
        filled_at=datetime(2026, 11, 2, 6, 38, 44, tzinfo=UTC), updated_at=None)


def venue_with(pages: list[list[dict[str, str]]]) -> Any:
    v = object.__new__(AlpacaCryptoVenue)
    v._trading = StubTrading(pages)
    return v


def activity(qty: str, price: str, order_id: str = "cc5cf991", i: int = 0) -> dict[str, str]:
    return {"id": f"a{i}", "order_id": order_id, "qty": qty, "price": price}


def test_buy_order_carries_its_fee_in_kind() -> None:
    from alpaca.trading.enums import OrderSide
    page = [activity(q, p, i=i) for i, (q, p) in enumerate(BTC_EXECUTIONS)]
    page.append(activity("1", "1", order_id="otra"))         # de otra orden: se ignora
    o = venue_with([page])._with_fees(raw_order(OrderSide.BUY))
    assert o.fee_qty == D("0.000064046") and o.fee_usd == 0


def test_sell_order_carries_a_fee_in_dollars() -> None:
    from alpaca.trading.enums import OrderSide
    o = venue_with([[activity("0.01", "80000")]])._with_fees(
        raw_order(OrderSide.SELL, filled="0.01"))
    assert o.fee_qty == 0 and o.fee_usd == D("0.01") * 80000 * D("0.0025")


def test_unpublished_executions_mean_ask_again_later() -> None:
    from alpaca.trading.enums import OrderSide
    v = venue_with([[activity("0.000057245", "78056.71")]])        # falta el resto
    with pytest.raises(VenueUnavailableError, match="no suman"):
        v._with_fees(raw_order(OrderSide.BUY))


def test_executions_follow_pagination() -> None:
    from alpaca.trading.enums import OrderSide
    first = [activity("0.0001", "1", i=i) for i in range(100)]
    v = venue_with([first, [activity("0.015617702", "1", i=100)]])
    o = v._with_fees(raw_order(OrderSide.BUY))
    last_id = first[-1]["id"]
    assert len(v._trading.calls) == 2 and v._trading.calls[1]["page_token"] == last_id
    assert o.fee_qty > 0
