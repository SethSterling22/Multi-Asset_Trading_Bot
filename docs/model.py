#!/usr/bin/env python3
"""Anexo A — todos los cálculos marcados [C] del informe, en un solo script.

Ejecuta:  python3 anexo_A_model.py
Sin dependencias externas. Determinista. Cada bloque imprime la sección del
informe a la que corresponde, de forma que cualquier cifra publicada se puede
localizar y reproducir.

Si una cifra del informe no aparece aquí, no debería llevar la marca [C].
"""
from __future__ import annotations
import math
from math import exp, lgamma, log, sqrt
from statistics import NormalDist

Z = NormalDist().inv_cdf
LINE = "=" * 78


def head(s: str) -> None:
    print(f"\n{LINE}\n{s}\n{LINE}")


# ===========================================================================
head("§2.2 · Techo de Sharpe:  SR_neto_max = sigma * SR_t^2 / (4c)")
# ===========================================================================
def sr_net_max(sigma: float, c: float, sr_t: float) -> tuple[float, float]:
    """Devuelve (SR_neto_max, h* en horas). c en fracción de retorno."""
    h = 4 * c * c / (sigma * sigma * sr_t * sr_t)      # años
    return sigma * sr_t * sr_t / (4 * c), h * 365 * 24


print(f"{'escenario':52s} {'SRmax':>7s} {'h* (h)':>8s} {'h* (d)':>7s}")
for name, sig, c, srt in [
    ("BTC perp taker/taker (10bps), señal floja", 0.45, 0.0010, 0.05),
    ("BTC perp maker/maker (4bps), señal floja",  0.45, 0.0004, 0.05),
    ("BTC perp taker/taker (10bps), señal buena", 0.45, 0.0010, 0.10),
    ("BTC perp maker/maker (4bps), señal buena",  0.45, 0.0004, 0.10),
    ("Alt top-50 taker (21bps), señal buena",     0.90, 0.0021, 0.10),
    ("Alt top-150 taker (57bps), señal buena",    0.90, 0.0057, 0.10),
]:
    s, h = sr_net_max(sig, c, srt)
    print(f"{name:52s} {s:7.2f} {h:8.1f} {h/24:7.1f}")

print("\nHorizonte mínimo de break-even:  h_min = (c / (sigma*SR_t))^2")
for sig, c, srt in [(0.45, 0.0010, 0.05), (0.45, 0.0010, 0.10),
                    (0.45, 0.0004, 0.05), (0.90, 0.0021, 0.10)]:
    h = (c / (sig * srt)) ** 2 * 365 * 24
    print(f"  sigma={sig:.2f} c={c*1e4:5.1f}bps SR_t={srt:.2f} -> h_min = {h:6.1f} h")


# ===========================================================================
head("§2.2 · Drag anual de comisiones sobre el notional")
# ===========================================================================
print(f"{'ops/día':>10} | {'4bps M/M':>10} | {'10bps BTC':>10} | "
      f"{'21bps alt':>10} | {'57bps a150':>11}")
for n in [0.06, 0.14, 0.25, 1, 2, 5, 10, 50]:
    v = [n * 365 * c / 100 for c in (4, 10, 21, 57)]      # bps -> %
    print(f"{n:>10} | {v[0]:9.1f}% | {v[1]:9.1f}% | {v[2]:9.1f}% | {v[3]:10.1f}%")
print("  (a 3x de apalancamiento, multiplicar por 3 para el drag sobre el equity)")


# ===========================================================================
head("§2.3 · El caso del vídeo: 35 operaciones, 7 días, +13%")
# ===========================================================================
R = 1.13
print(f"  retorno por operación (geom) = {(R**(1/35)-1)*1e4:7.1f} bps")
print(f"  retorno diario (geom)        = {(R**(1/7)-1)*100:7.3f} %")
print(f"  anualización naïve           = {(R**(365/7)-1)*100:,.0f} %")
print(f"\n{'sigma_op':>9} {'SR_t':>7} {'t(N=35)':>8} {'p(1 cola)':>10} "
      f"{'ops TOTAL t=2':>14} {'ops ADIC.':>10}")
for sig in (0.010, 0.015, 0.020, 0.030, 0.040):
    srt = (R ** (1 / 35) - 1) / sig
    t = srt * sqrt(35)
    # p de una cola con 34 gl, vía aproximación normal (suficiente aquí)
    p = 1 - NormalDist().cdf(t)
    ntot = (2.0 / srt) ** 2
    print(f"{sig*100:8.1f}% {srt:7.3f} {t:8.2f} {p:10.3f} {ntot:14.0f} "
          f"{max(0, ntot-35):10.0f}")
print("\n  Peso bayesiano de la muestra de 7 días:")
prior_sd, n_obs = 0.40, 7
se = sqrt(365 / n_obs)                       # SE del Sharpe anual con 7 obs
w_data, w_prior = 1 / se**2, 1 / prior_sd**2
print(f"    SE(SR_anual) con 7 obs = {se:.2f}")
print(f"    peso de los datos = {w_data:.4f} · peso del prior = {w_prior:.2f}")
print(f"    fracción de información aportada por la muestra = "
      f"{w_data/(w_data+w_prior)*100:.2f} %")
print("\n  Benchmark de la ventana: BTC +21.2% (dato del propio vídeo, [?] sin verificar)")
print(f"    diferencia = {21.2-13.0:.1f} pp en contra del bot")


# ===========================================================================
head("§2.3 · MinTRL (Bailey & López de Prado), alpha=0.05, SR*=0")
# ===========================================================================
def mintrl(sr: float, g3: float = 0.0, g4: float = 3.0, z: float = 1.645) -> float:
    return 1 + (1 - g3 * sr + (g4 - 1) / 4 * sr * sr) * (z / sr) ** 2


print(f"{'SR':>6} {'normal (años)':>15} {'cola gorda g3=-1,g4=6':>24}")
for sr in (0.3, 0.5, 0.8, 1.0, 1.5, 2.0):
    print(f"{sr:6.2f} {mintrl(sr):15.1f} {mintrl(sr,-1,6):24.1f}")


# ===========================================================================
head("§2.4 · Sharpe espurio esperado del máximo de N variantes (alpha real = 0)")
# ===========================================================================
def emax_z(n: int) -> float:
    """E[max de N normales estándar], aproximación de Gumbel (como en el DSR)."""
    g = 0.5772156649
    return (1 - g) * Z(1 - 1 / n) + g * Z(1 - 1 / (n * math.e))


print(f"{'datos':>10} {'SE(SR)':>8} " + "".join(f"{'N='+str(n):>10}" for n in (10, 100, 1000, 10000)))
for T, lab in ((126, "6 meses"), (252, "1 año"), (504, "2 años"), (1260, "5 años")):
    se = sqrt(252 / T)
    row = "".join(f"{se*emax_z(n):10.2f}" for n in (10, 100, 1000, 10000))
    print(f"{lab:>10} {se:8.3f} {row}")
print("\n  MinBTL (años) ~ 2*ln(N) / SR*^2,  con SR* = 1:")
for n in (45, 1000, 10000):
    print(f"    N={n:>6} -> {2*log(n):5.1f} años")


# ===========================================================================
head("§4.2 · Modelo de P&L neto — 3 escenarios × 5 tamaños de cuenta")
# ===========================================================================
# sleeve -> (peso, bruto anual, round-trips efectivos/año, coste round-trip)
SCEN = {
    "pesimista": {"carry": (0.40, 0.05,  6, 0.0033),
                  "xmom":  (0.40, 0.08, 21, 0.0021),
                  "trend": (0.20, 0.05, 24, 0.0010)},
    "base":      {"carry": (0.40, 0.10,  6, 0.0033),
                  "xmom":  (0.40, 0.16, 21, 0.0021),
                  "trend": (0.20, 0.12, 24, 0.0010)},
    "optimista": {"carry": (0.40, 0.15,  6, 0.0033),
                  "xmom":  (0.40, 0.24, 21, 0.0021),
                  "trend": (0.20, 0.20, 24, 0.0010)},
}
# Coste fijo mensual de la ARQUITECTURA RECOMENDADA (§4.4):
#   LLM event-gated con modelo Haiku-class + motor nocturno en batch
#   datos: stack gratuito por debajo de $50k
#   luz: perfil sin GPU dedicada
FIXED = {1000: (15, 0, 17), 5000: (15, 0, 17), 10000: (15, 0, 17),
         25000: (31, 0, 17), 100000: (56, 29, 25)}   # (llm, datos, luz) $/mes

for sname, sl in SCEN.items():
    gr = sum(w * g for w, g, _, _ in sl.values())
    tr = sum(w * t * c for w, _, t, c in sl.values())
    print(f"\n[{sname}]  bruto ponderado {gr*100:5.2f}%   drag de trading "
          f"{tr*100:5.2f}%   margen {(gr-tr)*100:5.2f}%")
    for cap, (llm, dat, kwh) in FIXED.items():
        fixed = (llm + dat + kwh) * 12
        net = cap * (gr - tr) - fixed
        print(f"  ${cap:>7,}: bruto ${cap*gr:>8,.0f}  tc ${cap*tr:>7,.0f}  "
              f"fijo ${fixed:>6,.0f}  NETO ${net:>9,.0f}  ({net/cap*100:>7.2f}%)  "
              f"[coste fijo = {fixed/cap*100:4.2f}% del capital]")

print("\n§4.2bis · Desglose por sleeve a $25,000 (escenario base)")
sl = SCEN["base"]; cap = 25000
tn = tg = tc = 0.0
for k, (w, g, t, c) in sl.items():
    n = cap * w; G = n * g; C = n * t * c
    print(f"  {k:6s} asignado ${n:>8,.0f} | bruto {g*100:5.1f}% = ${G:>7,.0f} | "
          f"{t:>3} RT × {c*1e4:4.1f}bps = ${C:>7,.0f} | neto ${G-C:>7,.0f} "
          f"({(G-C)/n*100:5.2f}%)")
    tn += n; tg += G; tc += C
fixed = (31 + 0 + 17) * 12
print(f"  TOTAL  ${tn:>8,.0f} | bruto ${tg:>7,.0f} | costes trading ${tc:>7,.0f} | "
      f"fijos ${fixed:>6,.0f} | NETO ${tg-tc-fixed:>7,.0f} "
      f"({(tg-tc-fixed)/cap*100:5.2f}%)")


# ===========================================================================
head("§4.3 · Capital de break-even y presupuesto de coste fijo")
# ===========================================================================
for sname in ("pesimista", "base", "optimista"):
    sl = SCEN[sname]
    gr = sum(w * g for w, g, _, _ in sl.values())
    tr = sum(w * t * c for w, _, t, c in sl.values())
    margin = gr - tr
    print(f"\n[{sname}] margen = {margin*100:.2f}%")
    print("  Break-even (neto = 0):  capital = coste_anual / margen")
    for m in (17, 32, 48, 67, 100):
        print(f"    ${m:>3}/mes (${m*12:>5}/año) -> ${m*12/margin:>10,.0f}")

BUDGET_PCT = 0.04   # = MAX_FIXED_COST_PCT_YEAR en core/risk_floor.py
print(f"\n  Presupuesto de disciplina: coste fijo <= {BUDGET_PCT*100:.0f}% anual del capital")
print("  (≈ 40% del margen del escenario base; deja el 60% como retorno)")
print(f"  Capital mínimo implícito = coste_anual / {BUDGET_PCT}:")
for m in (17, 32, 48, 67, 100):
    print(f"    ${m:>3}/mes -> ${m*12/BUDGET_PCT:>10,.0f}")
print("  Nota: el break-even es donde el neto es cero; el 4% es la disciplina")
print("        que deja margen. La segunda es más exigente, a propósito.")


# ===========================================================================
head("§4.4 · Coste del LLM según cadencia y formato de salida")
# ===========================================================================
def llm_cost(calls_day: int, fresh_tok: int, out_tok: int,
             in_price: float, out_price: float,
             prefix_tok: int = 10_000) -> tuple[float, float, dict]:
    """Caché de 1h: write = 2.0x input, read = 0.1x input. Batch se aplica aparte."""
    writes = min(384, max(16, calls_day // 4))     # refresco solo con actividad
    w = writes * prefix_tok / 1e6 * in_price * 2.0
    r = calls_day * prefix_tok / 1e6 * in_price * 0.1
    f = calls_day * fresh_tok / 1e6 * in_price
    o = calls_day * out_tok / 1e6 * out_price
    tot = w + r + f + o
    return tot, tot * 30, {"cache_write": w, "cache_read": r, "fresh": f, "output": o}


print("Modelo Sonnet-class ($2 / $10 por MTok)  [precios [?]: verificar en la doc]")
print(f"{'configuración':38s} {'$/día':>8s} {'$/mes':>9s}  "
      f"{'write%':>7s} {'read%':>6s} {'fresh%':>7s} {'out%':>6s}")
rows = [("rejilla 1536/día, prosa 1k tok", 1536, 1000),
        ("rejilla 1536/día, JSON 250 tok", 1536, 250),
        ("event-gated 8% = 123/día, JSON", 123, 250),
        ("event-gated 3% = 46/día, JSON",   46, 250)]
for lab, c_, o_ in rows:
    d, m, comp = llm_cost(c_, 2000, o_, 2.0, 10.0)
    pc = {k: v / d * 100 for k, v in comp.items()}
    print(f"{lab:38s} {d:8.2f} {m:9.2f}  {pc['cache_write']:6.1f}% "
          f"{pc['cache_read']:5.1f}% {pc['fresh']:6.1f}% {pc['output']:5.1f}%")

d_prosa = llm_cost(1536, 2000, 1000, 2.0, 10.0)[0]
d_json = llm_cost(1536, 2000, 250, 2.0, 10.0)[0]
d_ev = llm_cost(123, 2000, 250, 2.0, 10.0)[0]
print(f"\n  Palanca CADENCIA (JSON -> JSON):  {(1-d_ev/d_json)*100:5.1f}% de ahorro")
print(f"  Palanca FORMATO (prosa -> JSON):  {(1-d_json/d_prosa)*100:5.1f}% de ahorro")
print(f"  Ambas combinadas:                 {(1-d_ev/d_prosa)*100:5.1f}% de ahorro")
print("  Conclusión: la cadencia domina. El componente mayor de la factura con")
print("  caché activo son las ESCRITURAS de caché (53-62% en la config recomendada),")
print("  no el output (11-14%).")

print("\nModelo Haiku-class ($1 / $5 por MTok):")
for lab, c_, o_ in rows[2:]:
    d, m, _ = llm_cost(c_, 2000, o_, 1.0, 5.0)
    print(f"  {lab:38s} {d:7.2f}/día  {m:8.2f}/mes")

n = 30
d = (n * 30_000 / 1e6) * 2.0 * 0.5 + (n * 3_000 / 1e6) * 10.0 * 0.5
print(f"\nMotor nocturno L5: {n} llamadas/día (30k in / 3k out) con Batch -50%:")
print(f"  ${d:.2f}/día  =  ${d*30:.2f}/mes")
print(f"\nArquitectura recomendada: Haiku event-gated 3% (${llm_cost(46,2000,250,1.0,5.0)[1]:.0f}/mes)")
print(f"  + motor nocturno en batch (${d*30:.0f}/mes) = "
      f"${llm_cost(46,2000,250,1.0,5.0)[1] + d*30:.0f}/mes")
print(f"  frente a la implementación naïve (rejilla + prosa, Sonnet): "
      f"${llm_cost(1536,2000,1000,2.0,10.0)[1]:.0f}/mes")


# ===========================================================================
head("§4.5 · Kelly: crecimiento logarítmico esperado g(f) = f*mu - f^2*sigma^2/2")
# ===========================================================================
def g(f: float, sr: float, sigma: float) -> float:
    return f * sr * sigma - f * f * sigma * sigma / 2


print(f"{'estrategia':16s} {'f*':>6s} {'½K':>6s} {'g(1x)':>8s} {'g(1.5x)':>9s} "
      f"{'g(2x)':>9s} {'g(3x)':>9s}")
for sr, sig, lbl in [(0.3, 0.45, "SR0.3 BTC"), (0.5, 0.45, "SR0.5 BTC"),
                     (0.8, 0.45, "SR0.8 BTC"), (1.0, 0.45, "SR1.0 BTC"),
                     (0.5, 0.90, "SR0.5 alt")]:
    fs = sr / sig
    print(f"{lbl:16s} {fs:6.2f} {fs/2:6.2f} {g(1,sr,sig)*100:7.1f}% "
          f"{g(1.5,sr,sig)*100:8.1f}% {g(2,sr,sig)*100:8.1f}% {g(3,sr,sig)*100:8.1f}%")

print("\n§4.5bis · Distancia a liquidación (margen aislado):  1/L - m")
print(f"{'L':>5} {'BTC m=0.4%':>11} {'σ_d 2.4%':>10} {'alt m=1.0%':>11} {'σ_d 5.0%':>10}")
for L in (1, 1.5, 2, 3, 5, 10, 20):
    db, da = 1 / L - 0.004, 1 / L - 0.010
    print(f"{L:5.1f} {db*100:10.1f}% {db/0.024:9.1f}σ {da*100:10.1f}% {da/0.050:9.1f}σ")


# ===========================================================================
head("§4.6 · Diversificación efectiva con N posiciones correlacionadas")
# ===========================================================================
print(f"{'N':>4} " + "".join(f"{'rho='+str(r):>12}" for r in (0.7, 0.8, 0.9, 0.95)))
for N in (5, 8, 10, 16, 20):
    row = ""
    for rho in (0.7, 0.8, 0.9, 0.95):
        neff = N / (1 + (N - 1) * rho)
        row += f"{neff:12.2f}"
    print(f"{N:4d}{row}   <- N_efectivo")
for N in (16,):
    for rho in (0.7, 0.8, 0.9, 0.95):
        ratio = sqrt((1 + (N - 1) * rho) / N)
        print(f"  N={N} rho={rho}: sigma_cartera/sigma = {ratio:.3f} · "
              f"cap del 1%/posición -> riesgo de cartera {1*sqrt(ratio**2*N):.2f}%")


# ===========================================================================
head("§4.7 · Racha perdedora esperada y drawdown implicado")
# ===========================================================================
def streak(N: int, q: float) -> float:
    return log(N * (1 - q)) / log(1 / q)


print(f"{'win rate':>9} {'q':>6} {'N=100':>8} {'N=500':>8} {'N=1000':>8} {'N=2000':>8}")
for wr in (0.55, 0.45, 0.35):
    q = 1 - wr
    print(f"{wr*100:8.0f}% {q:6.2f} {streak(100,q):8.1f} {streak(500,q):8.1f} "
          f"{streak(1000,q):8.1f} {streak(2000,q):8.1f}")
print("\nDrawdown implicado por la racha, según fracción de riesgo r:")
print(f"{'r':>7} " + "".join(f"{'racha '+str(k):>12}" for k in (6, 10, 14)))
for r in (0.005, 0.01, 0.02, 0.05):
    row = "".join(f"{((1-r)**k-1)*100:11.1f}%" for k in (6, 10, 14))
    print(f"{r*100:6.1f}%{row}")


# ===========================================================================
head("§7.3 · n_required derivado (nunca literal)")
# ===========================================================================
ZA = {0.05: 1.959964, 0.005: 2.807034, 0.001: 3.290527}
zb = 0.841621                                   # potencia 0.80
print("(a) Test de proporción, p0 = 0.50, potencia 0.80")
for p1 in (0.55, 0.60, 0.65, 0.70, 0.80):
    out = []
    for a, za in ZA.items():
        nn = ((za * sqrt(0.25) + zb * sqrt(p1 * (1 - p1))) ** 2) / ((p1 - 0.5) ** 2)
        out.append(f"a={a}: {math.ceil(nn):5d}")
    print(f"  p1={p1:4.2f} (+{(p1-0.5)*100:4.1f}pp)  " + " | ".join(out))

print("\n(b) Test t sobre retornos, alpha=0.05, potencia 0.80:  n = 7.849 / d^2")
num = (1.959964 + 0.841621) ** 2
for d in (0.05, 0.10, 0.15, 0.20, 0.30, 0.50):
    print(f"  d={d:4.2f} -> n={math.ceil(num/d/d):5d}   "
          f"(SR anual equivalente a 500 trades/año = {d*sqrt(500):4.2f})")

print("\n(c) Binomial exacto para DEFECT:  p-valor = p0^k")
for p0 in (0.01, 0.02, 0.05, 0.10):
    print(f"  p0={p0:4.2f}: " + " | ".join(f"k={k} {p0**k:.2e}" for k in (2, 3, 4, 5)))
print("\n  n mínimo para alcanzar alpha:  n = ceil(ln alpha / ln p0)")
for p0 in (0.021, 0.05):
    for a in (0.01, 0.001):
        print(f"    p0={p0} alpha={a} -> n = {math.ceil(log(a)/log(p0))}")

print("\n(d) Deflación por clúster:  n_eff = n / (1 + (m-1)*rho_ICC)")
for n_, m_, rho in ((5, 5, 0.9), (20, 5, 0.9), (38, 4, 0.8), (52, 3, 0.8), (100, 2, 0.5)):
    print(f"    n={n_:3d} m={m_} rho={rho} -> n_eff = {n_/(1+(m_-1)*rho):6.2f}")


# ===========================================================================
head("§7.5 · El prior decide: Beta-Binomial con 5 de 5 éxitos")
# ===========================================================================
def betacdf(x: float, a: float, b: float, n: int = 20000) -> float:
    """Simpson sobre la densidad Beta. Suficiente para 4 decimales."""
    h = x / n
    lb = lgamma(a + b) - lgamma(a) - lgamma(b)
    s = 0.0
    for i in range(n + 1):
        t = i * h
        v = 0.0 if (t <= 0 or t >= 1) else exp(lb + (a - 1) * log(t) + (b - 1) * log(1 - t))
        w = 1 if i in (0, n) else (4 if i % 2 else 2)
        s += w * v
    return s * h / 3


for a0, b0 in ((1, 1), (10, 10), (50, 50), (100, 100), (200, 200)):
    a, b = a0 + 5, b0
    mean = a / (a + b)
    p_gt = 1 - betacdf(0.6, a, b)
    print(f"  prior Beta({a0:3d},{b0:3d}) (fuerza {a0+b0:3d}) -> posterior media "
          f"{mean:.4f} · P(theta > 0.60 | datos) = {p_gt:.4f}")
print("  Lectura: con el prior uniforme por defecto, 5 de 5 'confirman' al 95%.")
print("  Con un prior escéptico de fuerza 100, la misma evidencia da 0.058.")
print("  La diferencia entre aprender y autoengañarse es el prior.")


# ===========================================================================
head("§5.1 · Presupuesto de latencia: sigma de BTC por horizonte")
# ===========================================================================
sig_a = 0.45
for lab, frac in (("1 min", 1/(365*24*60)), ("5 min", 5/(365*24*60)),
                  ("1 h", 1/(365*24)), ("4 h", 4/(365*24)), ("1 d", 1/365)):
    print(f"  sigma({lab:5s}) = {sig_a*sqrt(frac)*1e4:8.1f} bps")
print("  Coste round-trip de un alt top-50 taker/taker: 21 bps")
print("  -> el coste supera la sigma a 5 min de BTC en un factor "
      f"{21/(sig_a*sqrt(5/(365*24*60))*1e4):.1f}x")


# ===========================================================================
head("§12.5 · Coste eléctrico:  kWh/mes = W * 0.7296")
# ===========================================================================
print("  ⚠ tarifa $/kWh de Puerto Rico [?] SIN VERIFICAR (lumapr.com bloqueado)")
print(f"{'W':>6} {'kWh/mes':>9} {'@$0.22':>9} {'@$0.25':>9} {'@$0.28':>9} {'año @$0.25':>12}")
for W in (15, 25, 50, 74, 90, 300):
    k = W * 0.7296
    print(f"{W:6d} {k:9.1f} {k*0.22:8.2f}$ {k*0.25:8.2f}$ {k*0.28:8.2f}$ "
          f"{k*0.25*12:11.0f}$")


# ===========================================================================
head("§12.6 · Drag anual del stack de datos según capital")
# ===========================================================================
print(f"{'$/mes':>7} " + "".join(f"{'$'+f'{c:,}':>11}" for c in (1000, 10000, 25000, 50000, 100000)))
for m in (0, 29, 58, 100, 200):
    row = "".join(f"{m*12/c*100:10.1f}%" for c in (1000, 10000, 25000, 50000, 100000))
    print(f"{m:7d}{row}")


# ===========================================================================
head("§2.2bis · Aritmética del spread: por qué el market making retail no sale")
# ===========================================================================
for price, tick, sym in ((110_000, 0.10, "BTCUSDT perp"), (4_000, 0.01, "ETHUSDT perp")):
    bps = tick / price * 1e4
    print(f"  {sym:14s} precio ${price:,} tick {tick} -> 1 tick = {bps:.4f} bps")
    print(f"                 spread 1-3 ticks = {bps:.4f}-{3*bps:.4f} bps")
    print(f"                 fee maker VIP0 = 2.0 bps = {2.0/bps:,.0f}x el tick")
print("  (tamaños de tick [K], verificar en la doc del venue)")
print("  Conclusión: capturas centésimas de bps de spread y pagas 2 bps de fee.")
print("  El market making en majors solo sale con rebate maker negativo.")


print(f"\n{LINE}\nFin. Todas las cifras marcadas [C] en el informe salen de aquí.\n{LINE}")
