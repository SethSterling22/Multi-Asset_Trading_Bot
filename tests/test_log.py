"""Cada evento lleva los campos fijos, y un rechazo de guard exige reason."""

from __future__ import annotations

import io
import json

import pytest

from core import log


def _events(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_every_event_has_the_fixed_fields() -> None:
    buf = io.StringIO()
    log.configure("test-service", stream=buf)
    log.get_logger().info("arranque")
    (event,) = _events(buf)
    assert set(log.FIXED_FIELDS) <= event.keys()
    assert event["service"] == "test-service"
    assert event["level"] == "info"


def test_guard_rejection_records_reason() -> None:
    buf = io.StringIO()
    log.configure("engine", stream=buf)
    log.guard_rejected(
        log.get_logger(), strategy_id="wheel", symbol="AAPL", action="sell_csp",
        reason="cash 1200.00 < collateral 15000.00",
    )
    (event,) = _events(buf)
    assert event["event"] == "guard_rejected"
    assert event["level"] == "warning"
    assert (event["strategy_id"], event["symbol"], event["action"]) == ("wheel", "AAPL", "sell_csp")
    assert event["reason"] == "cash 1200.00 < collateral 15000.00"


@pytest.mark.parametrize("reason", ["", "   "])
def test_guard_rejection_without_reason_raises(reason: str) -> None:
    log.configure("engine", stream=io.StringIO())
    with pytest.raises(ValueError):
        log.guard_rejected(log.get_logger(), strategy_id="wheel", symbol=None,
                           action="sell_csp", reason=reason)
