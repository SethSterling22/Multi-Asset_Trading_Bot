"""core/risk_floor.py — límites de riesgo INMUTABLES.

⚠️  ESTE FICHERO ES LA ÚNICA FUENTE DE VERDAD DE LOS LÍMITES DE RIESGO.

No se lee de YAML. No se lee de la base de datos. No es mutable en runtime.
No hay setters. No hay `override`. No hay `if os.environ.get(...)`.

Cambiarlo requiere un commit, una revisión y un despliegue — que es exactamente
la fricción que se quiere, porque estos números son la diferencia entre una
pérdida acotada y una cuenta a cero.

Si una tarea parece requerir hacer esto configurable, la tarea está mal
planteada. Pregunta antes de escribir código.

Justificación de cada número: docs/INFORME_TALOS.md §4.5, §4.6, §4.7.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================================
# APALANCAMIENTO
# ============================================================================
# Kelly: una estrategia con Sharpe VERDADERO de 0.5 sobre sigma=45% tiene
# apalancamiento growth-óptimo de 1.11x. A 3x, su crecimiento logarítmico
# esperado es -23.6%/año AUNQUE EL EDGE SEA REAL. En alts (sigma=90%), 3x da
# -229%/año.  [informe §4.5]
#
# Y f* se estima con SR_estimado, que está sesgado al alza por el sobreajuste
# de multiple testing. Usar 1.5x en lugar de 2x no es conservadurismo: es
# corrección del sesgo de estimación.
#
# Distancia a liquidación: 3x aislado en un alt liquida a -32.3%. El 10 de
# octubre de 2025 muchos alt perps imprimieron wicks de -40% a -90%, con ADL
# socializando pérdidas incluso en libros cubiertos. 3x en alts NO era
# superviviente ese día.
MAX_LEVERAGE_MAJORS: float = 1.5      # BTC, ETH
MAX_LEVERAGE_ALTS: float = 1.0        # spot, sin margen. No negociable.
MAJORS: frozenset[str] = frozenset({"BTC", "ETH"})

# Distancia mínima a liquidación, en sigmas de la volatilidad DIARIA del
# instrumento. Con 8 sigmas, una posición en BTC (sigma_d ~2.4%) exige ~19% de
# margen de seguridad.
#
# NOTA IMPORTANTE sobre por qué esta constante existe aunque hoy no pueda
# activarse: con MAX_LEVERAGE_MAJORS = 1.5 la distancia real es de 27.6 sigmas, y
# en alts a 1x spot no hay liquidación posible. Es decir, este limite es
# REDUNDANTE bajo los otros dos. Se mantiene deliberadamente como red de
# seguridad independiente: si alguna vez se sube el apalancamiento, este chequeo
# ya está en el camino de cada orden y no hay que acordarse de añadirlo.
# Un guard redundante que nunca dispara es gratis; uno que falta cuesta la cuenta.
MIN_LIQ_DISTANCE_SIGMAS: float = 8.0

# ============================================================================
# TAMAÑO Y EXPOSICIÓN
# ============================================================================
# Riesgo por operación. Racha perdedora esperada de una estrategia de trend con
# 35% de win rate: 13.6 pérdidas consecutivas sobre 1000 operaciones y 15.2
# sobre 2000, por pura secuencia y sin que nada esté roto. A r=1% una racha de
# 14 es -13.1% de drawdown; a r=5% sería -51.2%.  [informe §4.7]
MAX_RISK_PER_TRADE_PCT: float = 0.01

MAX_NOTIONAL_PCT_EQUITY: float = 0.25   # por posición individual
# Exposición BRUTA = suma de |notional| / equity. El carry delta-neutral consume
# 2x bruto por 1x neto (largo spot + corto perp), así que el bruto tiene que dejar
# sitio: con la asignación de §4.2 (carry 40%, xmom 40%, trend 20%) el bruto es
# 0.8 + 0.4 + 0.2 = 1.4.
MAX_GROSS_EXPOSURE_PCT: float = 1.60
# Exposición NETA (delta agregado) = suma algebraica de notional / equity.
# Esta es la que limita el riesgo direccional, y es la que no debe pasar de 1x
# salvo en majors, donde MAX_LEVERAGE_MAJORS permite 1.5x en una sola posición.
MAX_NET_EXPOSURE_PCT: float = 1.00
MAX_OPEN_POSITIONS: int = 8

# Cap de exposición AJUSTADO POR CORRELACIÓN. Con MAX_OPEN_POSITIONS = 8 y
# correlación media 0.8, son 1.19 apuestas independientes, no 8: la volatilidad
# de la cartera es el 91.7% de una única posición. En estrés, rho > 0.95 y
# N_efectivo -> 1.04.  [informe §4.6]
# A 16 posiciones (el diseño del vídeo) y rho=0.8, un cap del 1% por posición da
# un riesgo de cartera de 3.61%; a rho=0.9 (estrés) da 3.81%. El 4% de aquí está
# fijado contra el caso de estrés CON margen, no contra el caso normal.
# El allocator debe usar N_efectivo = N / (1 + (N-1)*rho) con la matriz rodante.
MAX_CORRELATION_ADJUSTED_RISK_PCT: float = 0.04
CORRELATION_STRESS_ASSUMPTION: float = 0.95   # el rho que se asume en estrés

# ============================================================================
# PARADAS
# ============================================================================
# El límite de pérdida diaria NO está aquí por alpha: la evidencia de que
# mejore el Sharpe es débil o inexistente, y mecánicamente elimina también la
# recuperación por reversión. Está aquí por RIESGO OPERACIONAL: acota el daño
# de un bug de estado, de datos rancios o de un websocket muerto que no cerró.
# No lo "optimices" creyendo que es una fuente de retorno.  [informe §4.7]
DAILY_LOSS_STOP_PCT: float = 0.03
WEEKLY_LOSS_STOP_PCT: float = 0.07
MAX_CONSECUTIVE_LOSSES: int = 8       # circuit breaker, no stop permanente

# Tasa máxima de órdenes. Protege contra bucles de bug, que es un modo de fallo
# mucho más probable que un movimiento adverso del mercado.
MAX_ORDERS_PER_MINUTE: int = 6
MAX_ORDERS_PER_DAY: int = 60

# ============================================================================
# HORIZONTE MÍNIMO
# ============================================================================
# SR_neto_max = sigma * SR_t^2 / (4c). Con fees taker y un SR por operación
# realista (0.05-0.10), ningún horizonte por debajo de ~4h tiene esperanza
# positiva. Un bot a 5 ops/día con órdenes market en BTC quema 182% del
# notional al año solo en comisiones.  [informe §2.2]
MIN_HOLD_SECONDS: int = 4 * 3600
# Presupuesto de turnover por sleeve, en round-trips equivalentes al año.
# Si un backtest quiere rotar más que esto, el backtest está sobreajustado.
MAX_ROUNDTRIPS_PER_YEAR: dict[str, int] = {
    "funding_carry": 12,
    "xsec_momentum": 21,   # == el gate de la Fase 2.6; el floor no lo relaja
    "trend": 30,
    "crypto_rebalance": 24,
    "wheel": 36,
}

# ============================================================================
# PRESUPUESTO DE COSTE  (una restricción, no una sugerencia)
# ============================================================================
# Break-even (neto = 0) con el stack recomendado de $32/mes: $3,933 en el
# escenario base, $12,137 en el pesimista. Por debajo de ~$3,000 este módulo
# pierde dinero por construcción, sin importar la calidad de la señal. [§4.3]
# 4% anual del capital asignado ~= 40% del margen del escenario base, dejando el
# 60% como retorno. Implica un capital mínimo de $9,600 con el stack recomendado
# de $32/mes. El break-even puro (neto = 0) es mas bajo, $3,933 en el escenario
# base: esta constante es DISCIPLINA, no el punto de equilibrio.
# Exención documentada: los pasos 5.1 y 5.2 de la Fase 5 ($500 y $2,000) estan
# exentos, porque ahi el coste es I+D operacional. La exencion caduca en 5.3.
MAX_FIXED_COST_PCT_YEAR: float = 0.04
LLM_MONTHLY_BUDGET_DEGRADE_AT: float = 0.80  # menos contexto, modelo más pequeño
LLM_MONTHLY_BUDGET_HALT_AT: float = 0.95     # corte: solo modelo local

# ============================================================================
# LÍMITES DEL LLM  (espacio de acción monótono hacia menos riesgo)
# ============================================================================
# Con esta única restricción, el peor caso de un LLM completamente alucinado es
# un bot que no opera — un fallo recuperable. Sin ella, el peor caso es la
# cuenta a cero.  [informe §5.3]
LLM_SIZE_MULTIPLIER_MIN: float = 0.25
LLM_SIZE_MULTIPLIER_MAX: float = 1.00       # NUNCA > 1.0. Es el invariante.
LLM_MAX_PROPOSALS_PER_WEEK: int = 5         # mitiga la fatiga de aprobación
LLM_PROPOSAL_TTL_DAYS: int = 7              # una propuesta no revisada EXPIRA

# Evidencia mínima para que una hipótesis pueda afectar a una decisión.
HYPOTHESIS_ALPHA: float = 0.01              # umbral de Ville: E_t >= 100
HYPOTHESIS_FDR_Q: float = 0.05              # e-BH bajo dependencia arbitraria
HYPOTHESIS_MAX_TTL_DAYS: int = 90           # re-confirmación desde cero, sin excepciones
HYPOTHESIS_EDGE_PRIOR_STRENGTH: int = 100   # Beta(a0+b0) escéptico para clase EDGE
# Con Beta(1,1), 5 aciertos de 5 dan P(theta>0.6)=0.953 — el falso
# descubrimiento tipico de un sistema auto-aprendiente. Con Beta(50,50) (fuerza
# 100) la misma evidencia da 0.058, que ni siquiera cruza un umbral del 5%.
# La diferencia entre aprender y autoenganarse es el prior.  [informe §7.5]

# ============================================================================
# VERIFICACIÓN DE INTEGRIDAD
# ============================================================================

_THIS_FILE = Path(__file__)


def floor_hash() -> str:
    """SHA-256 del contenido de este fichero.

    Cada servicio lo llama al arrancar y lo registra en
    `decision.risk_floor_version` y `orders.risk_floor_hash`, de forma que
    cualquier operación histórica es trazable al conjunto exacto de límites
    que estaba vigente cuando se tomó.
    """
    return hashlib.sha256(_THIS_FILE.read_bytes()).hexdigest()


def assert_no_mutation() -> None:
    """Comprueba que nadie ha parcheado estas constantes en runtime.

    Se llama en el arranque de cada servicio y periódicamente desde
    risk-monitor. Un monkeypatch de MAX_LEVERAGE_MAJORS es exactamente el tipo
    de cosa que un test descuidado deja puesto.
    """
    module = inspect.getmodule(assert_no_mutation)
    # El namespace necesita __file__ y __name__: el módulo los usa al ejecutarse
    # (_THIS_FILE = Path(__file__)). Sin ellos esto lanzaba NameError y el guard
    # no detectaba nada — el modo de fallo silencioso que este guard existe para
    # evitar, así que está cubierto por tests/guards/test_risk_floor.py.
    source_ns: dict[str, object] = {
        "__file__": str(_THIS_FILE),
        "__name__": "_risk_floor_verification",
    }
    exec(compile(_THIS_FILE.read_text(), str(_THIS_FILE), "exec"), source_ns)  # noqa: S102
    for name, expected in list(source_ns.items()):
        if name.isupper() and not name.startswith("_"):
            actual = getattr(module, name, None)
            if actual != expected:
                raise RuntimeError(
                    f"RISK FLOOR MUTADO EN RUNTIME: {name} = {actual!r}, "
                    f"esperado {expected!r}. Abortando."
                )


def max_leverage_for(symbol: str) -> float:
    """Apalancamiento máximo permitido para un símbolo."""
    base = symbol.split("-")[0].split("/")[0].upper()
    return MAX_LEVERAGE_MAJORS if base in MAJORS else MAX_LEVERAGE_ALTS


def correlation_adjusted_risk(per_position_risk: float, n_positions: int,
                              mean_correlation: float) -> float:
    """Riesgo de cartera con N posiciones equiponderadas y correlación media.

    sigma_cartera / sigma = sqrt((1 + (N-1)*rho) / N)
    riesgo de cartera = r * sqrt(ratio^2 * N)

    16 posiciones al 1% con rho=0.8 -> 3.61%, no 1% ni 4%.
    """
    if n_positions <= 0:
        return 0.0
    ratio_sq = (1.0 + (n_positions - 1) * mean_correlation) / n_positions
    return per_position_risk * math.sqrt(ratio_sq * n_positions)


def effective_bets(n_positions: int, mean_correlation: float) -> float:
    """Apuestas independientes efectivas. N / (1 + (N-1)*rho).

    16 posiciones a rho=0.8 -> 1.23. No 16.
    """
    if n_positions <= 1:
        return float(n_positions)
    return n_positions / (1.0 + (n_positions - 1) * mean_correlation)


_HASH_AT_IMPORT = floor_hash()
logger.info("risk_floor cargado. hash=%s", _HASH_AT_IMPORT[:16])
