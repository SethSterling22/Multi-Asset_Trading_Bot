"""Gráficos SVG del dashboard: válidos, escapados y sin romperse con datos raros."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from api.charts import legend, line_chart, waterfall

T = datetime(2026, 11, 2, tzinfo=UTC)


def parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)  # noqa: S314 — SVG generado por el propio test


def test_line_chart_is_valid_svg_with_one_polyline_per_series() -> None:
    svg = line_chart({"wheel": [(T, 100.0), (T + timedelta(hours=1), 110.0)],
                      "trend": [(T, 50.0), (T + timedelta(hours=1), 40.0)]})
    root = parse(svg)
    lines = root.findall("{http://www.w3.org/2000/svg}polyline")
    assert len(lines) == 2


def test_names_are_escaped() -> None:
    svg = line_chart({"<script>alert(1)</script>": [(T, 1.0), (T + timedelta(1), 2.0)]})
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    parse(svg)
    assert "&lt;b&gt;" in legend(["<b>"])


def test_degenerate_inputs() -> None:
    assert "Sin datos" in line_chart({})
    assert "Sin datos" in line_chart({"x": []})
    parse(line_chart({"flat": [(T, 5.0), (T + timedelta(1), 5.0)]}))   # sin rango en y
    parse(line_chart({"one": [(T, 5.0)]}))                              # un solo punto
    assert "Sin datos" in waterfall([])


def axis_labels(svg: str) -> list[str]:
    """Las etiquetas del eje Y: las que están a la izquierda del área del gráfico."""
    return [t.text or "" for t in parse(svg).findall("{http://www.w3.org/2000/svg}text")
            if float(t.get("x", "0")) < 64]


def x_labels(svg: str) -> list[tuple[str, str]]:
    return [(t.text or "", t.get("text-anchor", "")) for t in
            parse(svg).findall("{http://www.w3.org/2000/svg}text") if float(t.get("x", "0")) >= 64]


def test_x_axis_is_utc_shows_the_date_once_and_stays_inside() -> None:
    pts = [(T + timedelta(hours=h), 1.0 + h) for h in range(0, 9)]
    labels = x_labels(line_chart({"s": pts}))
    assert labels[0] == ("02 Nov 00:00", "start")           # T es medianoche UTC
    assert labels[-1] == ("08:00", "end")
    assert all(":" in text and "Nov" not in text for text, _ in labels[1:])


def test_x_axis_repeats_the_date_when_the_day_changes() -> None:
    pts = [(T + timedelta(hours=h), 1.0) for h in (0, 48)]
    texts = [t for t, _ in x_labels(line_chart({"s": pts}))]
    assert texts[0].startswith("02 Nov") and texts[-1].startswith("04 Nov")


def test_markers_are_drawn_with_their_text() -> None:
    svg = line_chart({"s": [(T, 1.0), (T + timedelta(hours=2), 2.0)]},
                     markers=[(T + timedelta(hours=1), "buy <BTC>")])
    root = parse(svg)
    assert len(root.findall("{http://www.w3.org/2000/svg}line[@class='mk']")) == 1
    assert "buy &lt;BTC&gt;" in svg


def test_bars_are_green_up_and_red_down() -> None:
    from api.charts import CRITICAL, GOOD, bars
    rects = parse(bars([("a", 5.0), ("b", -3.0)])).findall("{http://www.w3.org/2000/svg}rect")
    assert [r.get("fill") for r in rects] == [GOOD, CRITICAL]
    assert "Sin datos" in bars([])


def test_flat_equity_does_not_repeat_axis_labels() -> None:
    svg = line_chart({"crypto": [(T, 10000.0), (T + timedelta(hours=1), 10000.0)]},
                     y_format=lambda v: f"${v:,.0f}")
    labels = axis_labels(svg)
    assert len(labels) == len(set(labels)) >= 2
    assert "$10,000" in labels


def test_axis_ticks_are_round_numbers() -> None:
    svg = line_chart({"s": [(T, 9873.0), (T + timedelta(hours=1), 10412.0)]},
                     y_format=lambda v: f"{v:.0f}")
    assert all(int(label) % 100 == 0 for label in axis_labels(svg))


def test_single_point_is_drawn_as_a_dot() -> None:
    root = parse(line_chart({"one": [(T, 10000.0)]}))
    assert len(root.findall("{http://www.w3.org/2000/svg}circle")) == 1


def test_waterfall_ends_on_the_net_figure() -> None:
    steps = [("Bruto", 10.0), ("Comisiones", -0.21), ("Funding", 0.5), ("Slippage", -2.0),
             ("IA", 0.0), ("Neto", 8.29)]
    svg = waterfall(steps)
    root = parse(svg)
    rects = root.findall("{http://www.w3.org/2000/svg}rect")
    assert len(rects) == len(steps)
    assert "$8.29" in svg and "$10.00" in svg


def test_venue_colors_are_fixed_and_distinct() -> None:
    from api.queries import VENUE_COLORS
    assert len(set(VENUE_COLORS.values())) == len(VENUE_COLORS) == 5
    a = line_chart({"okx": [(T, 1.0), (T + timedelta(1), 2.0)]}, colors=VENUE_COLORS)
    b = line_chart({"binance": [(T, 1.0)], "okx": [(T, 1.0), (T + timedelta(1), 2.0)]},
                   colors=VENUE_COLORS)
    assert f'stroke="{VENUE_COLORS["okx"]}"' in a and f'stroke="{VENUE_COLORS["okx"]}"' in b


def test_waterfall_uses_the_given_money_format() -> None:
    from api.app import money
    svg = waterfall([("Bruto", 10.0), ("Comisiones", -12.0), ("Neto", -2.0)], money=money)
    assert "−$12.00" in svg and "$-" not in svg


def test_money_never_prints_a_signed_zero() -> None:
    from api.app import money, signed_class
    assert money(-0.0000001) == money(-0.0) == money(0.004) == "$0.00"
    assert money(-0.006) == "−$0.01"
    assert signed_class(-0.001) == signed_class(0.001) == "neu"
