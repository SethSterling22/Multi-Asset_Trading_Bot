"""Gráficos en SVG generado en el servidor, con el lenguaje visual del mockup.

Sin librerías de JavaScript ni build step (informe §8.1): el navegador recibe SVG
y HTMX lo refresca. Todo texto que entra en el SVG se escapa.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from html import escape

# Paleta categórica del mockup, en orden: --s1 … --s5.
PALETTE = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181")
GOOD, CRITICAL, AXIS, GRID, INK3 = "#0ca30c", "#d03b3b", "#33373d", "#22252a", "#898781"

Point = tuple[datetime, float]


def _ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi == lo:
        return [lo]
    step = (hi - lo) / n
    return [lo + i * step for i in range(n + 1)]


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """Marcas en múltiplos de 1, 2, 2.5 o 5 × 10^k dentro de [lo, hi]."""
    if not hi > lo:
        return [lo]
    raw = (hi - lo) / n
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(int((hi - first) / step + 1e-9) + 1)]


def line_chart(
    series: Mapping[str, Sequence[Point]],
    *,
    width: int = 760,
    height: int = 220,
    y_format: Callable[[float], str] = lambda v: f"{v:,.0f}",
    colors: Mapping[str, str] | None = None,
    markers: Sequence[tuple[datetime, str]] = (),
) -> str:
    """Varias series temporales en un mismo eje. Devuelve un <svg> o un aviso si no hay datos.

    `markers` son instantes a señalar (p.ej. fills) con una línea vertical y su texto.
    Las horas del eje X son UTC, como todo el ledger.
    """
    points = [p for s in series.values() for p in s]
    if not points:
        return '<p class="hint">Sin datos todavía.</p>'
    left, right, top, bottom = 64, 12, 10, 26
    w, h = width - left - right, height - top - bottom
    times = [p[0] for p in points] + [m[0] for m in markers]
    t0 = min(times).timestamp()
    t1 = max(times).timestamp()
    y0 = min(p[1] for p in points)
    y1 = max(p[1] for p in points)
    if y1 == y0:
        y0, y1 = y0 - 1, y1 + 1
    pad = (y1 - y0) * 0.06
    y0, y1 = y0 - pad, y1 + pad

    def x(t: float) -> float:
        return left + (0 if t1 == t0 else (t - t0) / (t1 - t0) * w)

    def y(v: float) -> float:
        return top + (1 - (v - y0) / (y1 - y0)) * h

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'xmlns="http://www.w3.org/2000/svg">']
    previous = None
    for v in _nice_ticks(y0, y1):
        # Con un rango estrecho, dos marcas pueden dar la misma etiqueta al redondear
        # (equity plana: "$10,000" tres veces). Se dibuja solo la primera.
        label = y_format(v)
        if label == previous:
            continue
        previous = label
        yy = y(v)
        out.append(f'<line class="gl" x1="{left}" x2="{left + w}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        out.append(f'<text class="ax" x="{left - 6}" y="{yy + 3.5:.1f}" '
                   f'text-anchor="end">{escape(label)}</text>')
    ticks = _ticks(t0, t1, 4)
    last_day = None
    for i, t in enumerate(ticks):
        moment = datetime.fromtimestamp(t, UTC)
        # La fecha solo en la primera marca y cuando cambia de día; el resto, la hora.
        label = moment.strftime("%H:%M" if moment.date() == last_day else "%d %b %H:%M")
        last_day = moment.date()
        # Los extremos se anclan hacia dentro para no salirse del gráfico.
        anchor = "start" if i == 0 and len(ticks) > 1 else (
            "end" if i == len(ticks) - 1 and len(ticks) > 1 else "middle")
        out.append(f'<text class="ax" x="{x(t):.1f}" y="{height - 6}" '
                   f'text-anchor="{anchor}">{escape(label)}</text>')
    for when, text in markers:
        mx = x(when.timestamp())
        out.append(f'<line class="mk" x1="{mx:.1f}" x2="{mx:.1f}" y1="{top}" y2="{top + h}">'
                   f'<title>{escape(text)}</title></line>')
        out.append(f'<path class="mkh" d="M{mx - 4:.1f},{top} L{mx + 4:.1f},{top} '
                   f'L{mx:.1f},{top + 6}Z"><title>{escape(text)}</title></path>')
    out.append(f'<line class="bl" x1="{left}" x2="{left + w}" y1="{top + h}" y2="{top + h}"/>')
    for i, (name, pts) in enumerate(series.items()):
        if not pts:
            continue
        color = (colors or {}).get(name, PALETTE[i % len(PALETTE)])
        path = " ".join(f"{x(p[0].timestamp()):.1f},{y(p[1]):.1f}" for p in pts)
        out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.8" '
                   f'points="{path}"><title>{escape(name)}</title></polyline>')
        if len(pts) == 1:   # una polyline de un punto no se ve
            cx, cy = x(pts[0][0].timestamp()), y(pts[0][1])
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{color}">'
                       f'<title>{escape(name)}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def bars(steps: Sequence[tuple[str, float]], *, width: int = 760, height: int = 180,
         money: Callable[[float], str] = lambda v: f"${v:,.2f}") -> str:
    """Una barra por etiqueta (p.ej. el cambio de equity de cada día), verde o roja según
    el signo, desde cero."""
    if not steps:
        return '<p class="hint">Sin datos todavía.</p>'
    lo = min(0.0, *(v for _, v in steps))
    hi = max(0.0, *(v for _, v in steps))
    if hi == lo:
        hi = lo + 1
    left, right, top, bottom = 64, 12, 18, 26
    w, h = width - left - right, height - top - bottom
    slot = w / len(steps)

    def y(v: float) -> float:
        return top + (1 - (v - lo) / (hi - lo)) * h

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'xmlns="http://www.w3.org/2000/svg">']
    out.append(f'<line class="bl" x1="{left}" x2="{left + w}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    label_every = max(1, len(steps) // 10)
    for i, (label, v) in enumerate(steps):
        bw = max(slot * 0.64, 2)
        x0 = left + i * slot + (slot - bw) / 2
        top_y, bot_y = y(max(v, 0)), y(min(v, 0))
        color = GOOD if v >= 0 else CRITICAL
        out.append(f'<rect x="{x0:.1f}" y="{top_y:.1f}" width="{bw:.1f}" '
                   f'height="{max(bot_y - top_y, 1):.1f}" rx="2" fill="{color}">'
                   f'<title>{escape(label)}: {escape(money(v))}</title></rect>')
        if i % label_every == 0 or i == len(steps) - 1:
            out.append(f'<text class="ax" x="{x0 + bw / 2:.1f}" y="{height - 8}" '
                       f'text-anchor="middle">{escape(label)}</text>')
    out.append(f'<text class="ax" x="{left - 6}" y="{y(hi) + 3.5:.1f}" text-anchor="end">'
               f'{escape(money(hi))}</text>')
    if lo < 0:
        out.append(f'<text class="ax" x="{left - 6}" y="{y(lo) + 3.5:.1f}" text-anchor="end">'
                   f'{escape(money(lo))}</text>')
    out.append("</svg>")
    return "".join(out)


def legend(names: Sequence[str], colors: Mapping[str, str] | None = None) -> str:
    items = []
    for i, name in enumerate(names):
        color = (colors or {}).get(name, PALETTE[i % len(PALETTE)])
        items.append(f'<li><i class="k" style="background:{color}"></i>{escape(name)}</li>')
    return f'<ul class="legend">{"".join(items)}</ul>'


def waterfall(steps: Sequence[tuple[str, float]], *, width: int = 760, height: int = 240,
              money: Callable[[float], str] = lambda v: f"${v:,.2f}") -> str:
    """Cascada bruto → costes → neto. El último paso es el total, dibujado desde cero.

    Los pasos intermedios son variaciones con signo: negativo resta (rojo), positivo
    suma (verde). El primero y el último son niveles.
    """
    if not steps:
        return '<p class="hint">Sin datos todavía.</p>'
    levels: list[tuple[str, float, float]] = []      # (etiqueta, desde, hasta)
    running = 0.0
    for i, (label, value) in enumerate(steps):
        if i == 0 or i == len(steps) - 1:
            levels.append((label, 0.0, value))
            running = value
        else:
            levels.append((label, running, running + value))
            running += value
    lo = min(0.0, *(min(a, b) for _, a, b in levels))
    hi = max(0.0, *(max(a, b) for _, a, b in levels))
    if hi == lo:
        hi = lo + 1
    left, right, top, bottom = 64, 12, 18, 30
    w, h = width - left - right, height - top - bottom
    slot = w / len(levels)

    def y(v: float) -> float:
        return top + (1 - (v - lo) / (hi - lo)) * h

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'xmlns="http://www.w3.org/2000/svg">']
    out.append(f'<line class="bl" x1="{left}" x2="{left + w}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    for i, (label, a, b) in enumerate(levels):
        x0 = left + i * slot + slot * 0.18
        bw = slot * 0.64
        is_total = i in (0, len(levels) - 1)
        color = PALETTE[0] if is_total else (GOOD if b >= a else CRITICAL)
        top_y, bot_y = y(max(a, b)), y(min(a, b))
        out.append(f'<rect x="{x0:.1f}" y="{top_y:.1f}" width="{bw:.1f}" '
                   f'height="{max(bot_y - top_y, 1):.1f}" rx="2" fill="{color}">'
                   f'<title>{escape(label)}: {escape(money(b - a if not is_total else b))}'
                   f'</title></rect>')
        shown = b if is_total else b - a
        out.append(f'<text class="dlab{" em" if is_total else ""}" x="{x0 + bw / 2:.1f}" '
                   f'y="{top_y - 5:.1f}" text-anchor="middle">{escape(money(shown))}</text>')
        out.append(f'<text class="ax" x="{x0 + bw / 2:.1f}" y="{height - 10}" '
                   f'text-anchor="middle">{escape(label)}</text>')
    out.append("</svg>")
    return "".join(out)
