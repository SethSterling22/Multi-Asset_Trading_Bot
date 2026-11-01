"""Grupos 1 y 2 de CLAUDE.md §1.8 (CSP sin caja, CC sin 100 acciones), y la delta real.

Contra la lógica del Wheel con un broker falso: el mismo Protocol que implementa
el adaptador de Lumibot.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from core.ledger.fills import AssetType, Fill, Side
from strategies.base import check_cash_secured, check_collateral_cap, check_covered_call
from strategies.wheel.logic import PendingOrder, ShortOption, WheelLogic
from tests.guards.helpers import (
    NOW,
    Events,
    FakeOptionsBroker,
    FixedLossReference,
    MemoryLedger,
    call,
    put,
    setup,
    write_config,
)

pytestmark = pytest.mark.guards


def wheel(tmp_path: Path, broker: FakeOptionsBroker, *, fills: list[Fill] | None = None,
          whitelist: list[str] | None = None, **setup_kw: object) -> WheelLogic:
    cfg = write_config(tmp_path, **{"wheel_parameters.whitelist_assets": whitelist or ["AAPL"]})
    logic = WheelLogic(broker, fills=lambda: fills or [])
    setup(logic, cfg, **setup_kw)  # type: ignore[arg-type]
    return logic


# --- Grupo 1: CSP rechazada sin caja -----------------------------------------
def test_cash_secured_boundaries() -> None:
    assert check_cash_secured(cash=D(14_000), committed_collateral=D(0),
                              new_collateral=D(14_000)).ok
    v = check_cash_secured(cash=D(13_999), committed_collateral=D(0), new_collateral=D(14_000))
    assert not v.ok and v.code == "no_cash"
    v = check_cash_secured(cash=D(50_000), committed_collateral=D(40_000),
                           new_collateral=D(14_000))
    assert not v.ok and v.code == "no_cash"
    v = check_cash_secured(cash=D(50_000), committed_collateral=D(0), new_collateral=D(0))
    assert not v.ok and v.code == "invalid_collateral"


def test_csp_rejected_without_cash(tmp_path: Path) -> None:
    events = Events()
    broker = FakeOptionsBroker(cash_usd=D(10_000))
    wheel(tmp_path, broker).run_iteration()
    assert broker.orders == []
    (rej,) = events.rejections()
    assert (rej["reason"], rej["action"], rej["symbol"]) == ("no_cash", "sell_csp", "AAPL")


def test_csp_counts_collateral_of_other_underlyings(tmp_path: Path) -> None:
    # 30k de caja y una put corta de SPY a 500 (50k comprometidos): la caja libre es
    # −20k. El check viejo comparaba 30k contra los ~14k de AAPL y la dejaba pasar.
    events = Events()
    broker = FakeOptionsBroker(cash_usd=D(30_000), shorts=[ShortOption(put("SPY", 500), 1)])
    wheel(tmp_path, broker).run_iteration()
    assert broker.orders == []
    assert [e["reason"] for e in events.rejections()] == ["no_cash"]


def test_csp_counts_pending_orders(tmp_path: Path) -> None:
    pending = PendingOrder("MSFT", put("MSFT", 380), Side.SELL_TO_OPEN, 1)   # 38k
    broker = FakeOptionsBroker(cash_usd=D(50_000), pending=[pending])
    wheel(tmp_path, broker).run_iteration()
    assert broker.orders == []


def test_csp_allowed_with_cash(tmp_path: Path) -> None:
    broker = FakeOptionsBroker()
    wheel(tmp_path, broker).run_iteration()
    ((kind, contract, qty),) = broker.orders
    assert (kind, contract.underlying, contract.right.value, qty) == ("sell_to_open", "AAPL",
                                                                      "put", 1)
    assert contract.strike < D(150)          # OTM


def test_collateral_cap(tmp_path: Path) -> None:
    # ~14k de colateral sobre 50k de cartera = ~28% > 20% configurado.
    events = Events()
    broker = FakeOptionsBroker(value_usd=D(50_000))
    # Equity de referencia = la actual: sin pérdida del día, para aislar el cap.
    flat = FixedLossReference(day=D(50_000), week=D(50_000))
    wheel(tmp_path, broker, loss=flat).run_iteration()
    assert broker.orders == []
    assert [e["reason"] for e in events.rejections()] == ["collateral_cap"]


def test_collateral_cap_never_looser_than_floor() -> None:
    v = check_collateral_cap(collateral=D(26), portfolio_value=D(100), max_pct=0.90)
    assert not v.ok and "0.25" in v.detail
    v = check_collateral_cap(collateral=D(1), portfolio_value=D(0), max_pct=0.2)
    assert not v.ok and v.code == "no_equity"
    assert check_collateral_cap(collateral=D(20), portfolio_value=D(100), max_pct=0.2).ok


# --- Grupo 2: CC rechazada sin 100 acciones por contrato ---------------------
def test_covered_call_needs_100_shares_per_contract() -> None:
    assert check_covered_call(shares=D(100), open_short_calls=0, pending_short_calls=0,
                              new_contracts=1).ok
    for kw in (
        {"shares": D(99), "open_short_calls": 0, "pending_short_calls": 0},
        {"shares": D(100), "open_short_calls": 1, "pending_short_calls": 0},
        {"shares": D(150), "open_short_calls": 0, "pending_short_calls": 1},
    ):
        v = check_covered_call(new_contracts=1, **kw)  # type: ignore[arg-type]
        assert not v.ok and v.code == "not_covered"
    v = check_covered_call(shares=D(500), open_short_calls=0, pending_short_calls=0,
                           new_contracts=0)
    assert not v.ok and v.code == "invalid_contracts"


def test_wheel_sells_call_only_when_covered(tmp_path: Path) -> None:
    broker = FakeOptionsBroker(held={"AAPL": D(100)})
    wheel(tmp_path, broker).run_iteration()
    ((kind, contract, _),) = broker.orders
    assert (kind, contract.right.value) == ("sell_to_open", "call")
    assert contract.strike > D(150)


def test_wheel_cc_guard_counts_existing_calls(tmp_path: Path) -> None:
    # Con 100 acciones y una call ya vendida en OTRO símbolo no cuenta; en el mismo
    # subyacente, el ciclo ni siquiera llega a vender (una opción viva por subyacente).
    events = Events()
    logic = wheel(tmp_path, FakeOptionsBroker(held={"AAPL": D(100)}))
    pending = PendingOrder("AAPL", call("AAPL", 170), Side.SELL_TO_OPEN, 1)
    logic.broker.pending = [pending]                       # type: ignore[attr-defined]
    assert logic.sell_covered_call("AAPL", D(100), logic.config().wheel_parameters) is None  # type: ignore[union-attr]
    assert [e["reason"] for e in events.rejections()] == ["not_covered"]


def test_with_99_shares_wheel_sells_put_not_call(tmp_path: Path) -> None:
    broker = FakeOptionsBroker(held={"AAPL": D(99)})
    wheel(tmp_path, broker).run_iteration()
    assert [c.right.value for _, c, _ in broker.orders] == ["put"]


# --- Delta real (Fase 0.3) ---------------------------------------------------
def test_no_greeks_and_no_iv_means_zero_orders_and_one_rejection(tmp_path: Path) -> None:
    """GATE de la Sesión 2: sin greeks y sin IV ⇒ 0 órdenes y 1 log de rechazo."""
    events = Events()
    broker = FakeOptionsBroker(quotes=False)          # ni precio de opción ni delta del broker
    wheel(tmp_path, broker).run_iteration()
    assert broker.orders == []
    (rej,) = events.rejections()
    assert (rej["reason"], rej["action"], rej["symbol"]) == ("no_greeks", "select_put", "AAPL")


def test_no_risk_free_rate_and_no_broker_delta_is_no_greeks(tmp_path: Path) -> None:
    events = Events()
    broker = FakeOptionsBroker(rate=None)
    wheel(tmp_path, broker).run_iteration()
    assert broker.orders == []
    assert [e["reason"] for e in events.rejections()] == ["no_greeks"]


def test_implausible_broker_delta_is_rejected(tmp_path: Path) -> None:
    # Todas las puts OTM con delta −0.95 del broker: imposible para su moneyness.
    events = Events()
    broker = FakeOptionsBroker()
    logic = wheel(tmp_path, broker)
    for strike in broker.strikes("AAPL", broker.expiry, put("AAPL", 1).right):
        broker.deltas[put("AAPL", int(strike))] = -0.95
    logic.run_iteration()
    assert broker.orders == []
    assert [e["reason"] for e in events.rejections()] == ["delta_outside_band"]


def test_broker_delta_is_preferred_when_plausible(tmp_path: Path) -> None:
    broker = FakeOptionsBroker()
    broker.deltas[put("AAPL", 120)] = -0.25        # exactamente el objetivo
    wheel(tmp_path, broker).run_iteration()
    ((_, contract, _),) = broker.orders
    assert contract == put("AAPL", 120)


def test_missing_spot_and_expiration_are_logged(tmp_path: Path) -> None:
    events = Events()
    broker = FakeOptionsBroker(spots={"AAPL": D(150)})
    logic = wheel(tmp_path, broker, whitelist=["AAPL", "TSLA"])
    broker.expiry = broker.expiry + timedelta(days=60)   # fuera de la ventana de DTE
    logic.run_iteration()
    reasons = sorted(e["reason"] for e in events.rejections())
    assert reasons == ["no_expiration", "no_spot"]


# --- Cierre anticipado desde el ledger (sobrevive a reinicios) ---------------
def _sold(contract_price: str) -> list[Fill]:
    c = put("AAPL", 130)
    return [Fill(ts=NOW - timedelta(days=10), venue="alpaca", symbol=c.occ_symbol,
                 asset_type=AssetType.OPTION, side=Side.SELL_TO_OPEN, qty=D(1),
                 price=D(contract_price), multiplier=D(100), strategy_id="wheel",
                 venue_fill_id="sto-1")]


def test_early_close_uses_entry_premium_from_ledger(tmp_path: Path) -> None:
    c = put("AAPL", 130)
    broker = FakeOptionsBroker(shorts=[ShortOption(c, 1)], mids={c: D("0.40")})
    # prima 2.00, ahora 0.40 -> capturado 80% -> recompra
    wheel(tmp_path, broker, fills=_sold("2.00")).run_iteration()
    assert broker.orders == [("buy_to_close", c, 1)]


def test_no_early_close_below_target(tmp_path: Path) -> None:
    c = put("AAPL", 130)
    broker = FakeOptionsBroker(shorts=[ShortOption(c, 1)], mids={c: D("0.50")})
    wheel(tmp_path, broker, fills=_sold("2.00")).run_iteration()   # 75% < 80%
    assert broker.orders == []


def test_early_close_without_ledger_entry_is_logged_not_guessed(tmp_path: Path) -> None:
    events = Events()
    c = put("AAPL", 130)
    broker = FakeOptionsBroker(shorts=[ShortOption(c, 1)], mids={c: D("0.01")})
    wheel(tmp_path, broker, fills=[]).run_iteration()
    assert broker.orders == []
    assert [e["reason"] for e in events.rejections()] == ["no_entry_premium"]


def test_early_close_without_quote_is_logged(tmp_path: Path) -> None:
    events = Events()
    c = put("AAPL", 130)
    broker = FakeOptionsBroker(shorts=[ShortOption(c, 1)], quotes=False)
    wheel(tmp_path, broker, fills=_sold("2.00")).run_iteration()
    assert broker.orders == []
    assert [e["reason"] for e in events.rejections()] == ["no_quote"]


def test_pending_close_is_not_resubmitted(tmp_path: Path) -> None:
    c = put("AAPL", 130)
    broker = FakeOptionsBroker(shorts=[ShortOption(c, 1)], mids={c: D("0.10")},
                               pending=[PendingOrder("AAPL", c, Side.BUY_TO_CLOSE, 1)])
    wheel(tmp_path, broker, fills=_sold("2.00")).run_iteration()
    assert broker.orders == []


def test_pending_open_blocks_a_second_sale(tmp_path: Path) -> None:
    broker = FakeOptionsBroker(pending=[PendingOrder("AAPL", put("AAPL", 140),
                                                     Side.SELL_TO_OPEN, 1)])
    wheel(tmp_path, broker).run_iteration()
    assert broker.orders == []


def test_gate_rejection_stops_everything(tmp_path: Path) -> None:
    events = Events()
    c = put("AAPL", 130)
    broker = FakeOptionsBroker(shorts=[ShortOption(c, 1)], mids={c: D("0.10")})
    wheel(tmp_path, broker, fills=_sold("2.00"),
          loss=FixedLossReference(day=None)).run_iteration()
    assert broker.orders == []
    assert [e["reason"] for e in events.rejections()] == ["loss_limit_unknown"]


def test_one_symbol_failing_does_not_stop_the_others(tmp_path: Path) -> None:
    events = Events()
    broker = FakeOptionsBroker()
    logic = wheel(tmp_path, broker, whitelist=["BOOM", "AAPL"])
    broker.spots["BOOM"] = D(10)
    original = broker.strikes

    def strikes(symbol, expiration, right):  # type: ignore[no-untyped-def]
        if symbol == "BOOM":
            raise RuntimeError("broker caído para BOOM")
        return original(symbol, expiration, right)

    broker.strikes = strikes  # type: ignore[method-assign]
    logic.run_iteration()
    assert [c.underlying for _, c, _ in broker.orders] == ["AAPL"]
    assert any(e["event"] == "wheel_cycle_error" for e in events.all())


def test_ledger_records_only_own_strategy(tmp_path: Path) -> None:
    ledger = MemoryLedger()
    logic = wheel(tmp_path, FakeOptionsBroker(), ledger=ledger)
    fill = _sold("2.00")[0]
    assert logic.record_fill(fill) is True
    assert logic.record_fill(fill) is False
    from dataclasses import replace
    with pytest.raises(ValueError):
        logic.record_fill(replace(fill, strategy_id="trend", venue_fill_id="x"))


def test_invalid_config_is_logged_and_nothing_is_sent(tmp_path: Path) -> None:
    events = Events()
    broker = FakeOptionsBroker()
    logic = WheelLogic(broker, fills=lambda: [])
    cfg = write_config(tmp_path, **{"wheel_parameters.delta_limit_csp": 0.3})
    setup(logic, cfg)
    logic.run_iteration()
    assert broker.orders == []
    assert [(e["reason"], e["action"]) for e in events.rejections()] == [
        ("params_invalid", "iteration")
    ]
