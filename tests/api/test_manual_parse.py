"""Validación del formulario del diario manual, sin base de datos."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

from api.manual import ManualEntry, parse, same_origin
from core.ledger.fills import Side

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
OK = {"fecha": "2026-11-03", "hora": "10:30", "zona": "America/Puerto_Rico", "cuenta": "Webull",
      "simbolo": "aapl", "tipo": "stock", "lado": "buy", "cantidad": "10", "precio": "1,234.50",
      "comision": "1", "multiplicador": "", "motivo": "ruptura del máximo de ayer"}


def test_valid_entry_is_normalised_and_in_utc() -> None:
    e = parse(OK, now=NOW)
    assert isinstance(e, ManualEntry)
    assert e.ts == datetime(2026, 11, 3, 14, 30, tzinfo=UTC)      # Puerto Rico = UTC−4
    assert (e.venue, e.symbol, e.side, e.qty, e.price) == ("webull", "AAPL", Side.BUY, 10,
                                                          D("1234.50"))
    assert e.multiplier == 1 and e.notional == D("12345.00")
    # La confirmación reenvía exactamente lo validado, y vuelve a dar lo mismo.
    assert parse(e.as_form("America/Puerto_Rico"), now=NOW) == e


def test_options_default_to_100_and_need_option_sides() -> None:
    form = {**OK, "simbolo": "SPY261218P00550000", "tipo": "option", "lado": "sell_to_open"}
    e = parse(form, now=NOW)
    assert isinstance(e, ManualEntry) and e.multiplier == 100
    errors = parse({**form, "lado": "buy"}, now=NOW)
    assert isinstance(errors, list) and any("sell_to_open" in x for x in errors)
    errors = parse({**OK, "lado": "sell_to_open"}, now=NOW)
    assert isinstance(errors, list) and any("buy, sell" in x for x in errors)


def test_every_bad_field_is_reported() -> None:
    errors = parse({**OK, "cantidad": "0", "precio": "-1", "comision": "abc", "motivo": " ",
                    "cuenta": "mi cuenta!", "fecha": "2026-11-04"}, now=NOW)
    assert isinstance(errors, list)
    for needle in ("cantidad > 0", "precio >= 0", "comisión: no es un número", "motivo",
                   "cuenta/broker", "futuro"):
        assert any(needle in x for x in errors), needle


def test_garbage_does_not_crash() -> None:
    assert isinstance(parse({}, now=NOW), list)
    assert isinstance(parse({**OK, "precio": "NaN"}, now=NOW), list)
    assert isinstance(parse({**OK, "tipo": "x", "lado": "y", "zona": "Mars"}, now=NOW), list)


def test_same_origin() -> None:
    assert same_origin({"host": "100.1.2.3:8000", "origin": "http://100.1.2.3:8000"})
    assert same_origin({"host": "h:8000", "referer": "http://h:8000/diario/nueva"})
    assert not same_origin({"host": "h:8000", "origin": "http://evil.example"})
    assert not same_origin({"host": "h:8000"})
