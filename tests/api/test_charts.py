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
    return [t.text or "" for t in parse(svg).findall("{http://www.w3.org/2000/svg}text")
            if t.get("text-anchor") == "end"]


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
