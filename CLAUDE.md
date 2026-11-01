# CLAUDE.md — contrato de trabajo para `Multi-Asset_Trading_Bot`

Este fichero es normativo. Léelo al empezar cada sesión, junto con `DECISIONS.md`.
Si una tarea parece requerir violar algo marcado **INVARIANTE**, la tarea está mal
planteada: **pregunta antes de escribir código**.

---

## 0. Qué es este proyecto

Plataforma de trading multi-activo y multi-estrategia, operada por una sola persona
desde un homelab en Puerto Rico. Cinco sleeves de ingreso:

| Sleeve | Activo | Dirección | Estado |
|---|---|---|---|
| `wheel` | Equities + opciones (Alpaca) | Venta de prima | Implementado, con delta pendiente |
| `crypto_rebalance` | Cripto spot | Long/flat | Implementado, sin broker en vivo |
| `funding_carry` | Perps regulados CFTC (Coinbase/Kraken) | **Delta-neutral** | Por construir |
| `xsec_momentum` | Perps de BTC/ETH + spot de alts | **Long/short solo en majors**; alts long/flat (§1.1: el floor limita alts a spot 1×, y en spot sin margen no se puede vender en corto) | Por construir |
| `trend` | BTC/ETH perps | Long/short | Por construir |

**El objetivo no es maximizar el Sharpe de ningún sleeve. Es que los sleeves estén
descorrelacionados entre sí.** Si una propuesta mejora un sleeve a costa de
correlacionarlo con otro, es una mala propuesta.

---

## 1. INVARIANTES — no negociables

### 1.1 El risk floor son constantes de Python

`core/risk_floor.py` contiene **constantes de módulo**. Nunca YAML, nunca base de
datos, nunca mutable en runtime. Al arrancar, cada servicio verifica el hash del
fichero y lo registra en `decision.risk_floor_version`.

```python
# core/risk_floor.py
MAX_LEVERAGE_MAJORS                = 1.5    # BTC, ETH
MAX_LEVERAGE_ALTS                  = 1.0    # spot, sin margen -> sin cortos
MIN_LIQ_DISTANCE_SIGMAS            = 8.0    # en sigmas de la vol DIARIA
MAX_RISK_PER_TRADE_PCT             = 0.01
MAX_NOTIONAL_PCT_EQUITY            = 0.25   # por posición
MAX_GROSS_EXPOSURE_PCT             = 1.60   # el carry consume 2x bruto por 1x neto
MAX_NET_EXPOSURE_PCT               = 1.00   # delta agregado
MAX_OPEN_POSITIONS                 = 8
MAX_CORRELATION_ADJUSTED_RISK_PCT  = 0.04
CORRELATION_STRESS_ASSUMPTION      = 0.95
DAILY_LOSS_STOP_PCT                = 0.03
WEEKLY_LOSS_STOP_PCT               = 0.07
MAX_CONSECUTIVE_LOSSES             = 8
MAX_ORDERS_PER_MINUTE              = 6      # protege contra bucles de bug
MAX_ORDERS_PER_DAY                 = 60
MIN_HOLD_SECONDS                   = 14400  # 4 h: por debajo, esperanza negativa
MAX_ROUNDTRIPS_PER_YEAR            = {...}  # por sleeve
MAX_FIXED_COST_PCT_YEAR            = 0.04   # ~40% del margen del escenario base
LLM_SIZE_MULTIPLIER_MIN/MAX        = 0.25 / 1.00
LLM_MAX_PROPOSALS_PER_WEEK         = 5
HYPOTHESIS_ALPHA / FDR_Q / TTL     = 0.01 / 0.05 / 90 d
HYPOTHESIS_EDGE_PRIOR_STRENGTH     = 100
```

El fichero completo, con la justificación de cada número en comentarios, es
`anexo_C_risk_floor.py` del informe. **Cópialo literalmente a `core/risk_floor.py`**,
no lo reconstruyas desde esta lista: la lista es un resumen, y las fases 2, 3 y 5
dependen de constantes que aquí aparecen abreviadas.

Si una tarea dice "hazlo configurable", la respuesta es no. La justificación está en
el informe §9.1 y en el principio que ya está en `ROADMAP.md`: *guards are code,
not configuration*.

### 1.2 Todo efecto derivado del LLM es monótono hacia menos riesgo

El LLM **puede**: vetar una entrada, reducir tamaño (multiplicador en `[0.25, 1.00]`),
estrechar un stop, bloquear un componente, ampliar un cooldown, forzar abstención.

El LLM **nunca puede**: abrir una posición que la lógica determinista no propuso,
aumentar tamaño por encima de `1.0×`, ensanchar un stop, levantar un límite, aumentar
apalancamiento, tocar el risk floor.

Esto se hace cumplir en tres sitios a la vez, no en uno:
- `CHECK` en la tabla `hypothesis`: `no_size_increase`, `cooldown_only_extends`,
  `stop_only_tightens`, `act_requires_evidence`
- type system: `SizeMultiplier = NewType('SizeMultiplier', float)` con factory validada
- test: `tests/guards/test_llm_monotone.py` prueba exhaustivamente que ningún
  `effect_kind` puede producir un `Intent'` de mayor riesgo que el `Intent`

**Con esta restricción, el peor caso de un LLM completamente alucinado es un bot que
no opera. Sin ella, el peor caso es la cuenta a cero.**

### 1.3 La posición se deriva de `fills`

`fills` es append-only. La posición es una **vista derivada**. Ningún proceso guarda
la posición en memoria como fuente de verdad. Si el proceso muere, el estado no se
pierde porque nunca estuvo ahí.

### 1.4 Idempotencia de órdenes: write-ahead intent

```python
# El orden importa y no es negociable.
1. INSERT INTO orders (client_order_id, ..., status='PENDING_NEW')
2. COMMIT                                    # <- ANTES de tocar la red
3. POST /orders   con ese client_order_id
4. on timeout/5xx: NUNCA reenviar a ciegas.
   GET /orders?client_order_id=...  y reconciliar.
```

`client_order_id` = `f"{strategy_id}-{symbol}-{bar_id}-{attempt}"`, determinista y
consultable. Antes de operar con tamaño en un venue nuevo, **valida empíricamente**
que el venue acepta el id, lo devuelve en el ack y lo permite consultar después.

### 1.5 Los stops protectores viven en el exchange

`reduce-only`, residentes en el venue, colocados inmediatamente después del fill de
entrada, en la misma unidad de trabajo. **Un bot que necesita estar vivo para
gestionar riesgo es una apuesta a tu uptime**, y el uptime aquí depende de LUMA.

### 1.6 Los checks pre-trade son una librería in-process

`risk/pretrade.py` se importa dentro de `execution/router.py`. **Nunca** es un
servicio HTTP. Un check de riesgo que puede hacer timeout fallará abierto justo
cuando importa.

### 1.7 El kill switch es fail-closed y triple redundante

Activo si **cualquiera** de estas tres cosas lo dice: fila en `risk_events`, clave en
Redis, o el fichero `/run/trading/HALT`. **Un error al comprobarlo cuenta como
activo.** Es el único control que debe funcionar cuando todo lo demás está roto.

### 1.8 `tests/guards/` no puede fallar nunca

Cubre: CSP rechazado sin caja, CC rechazado sin 100 acciones por contrato, límite de
pérdida diaria, kill switch, venta cripto > holdings, compra > USDC, cada permutación
inválida de config, monotonía del LLM, distancia mínima a liquidación, cap de
exposición ajustado por correlación.

**Si un cambio los rompe, el cambio está mal, no el test.** Corren en CI en cada push
y son requisito de merge. No los "optimices": un `return` temprano que parece
redundante es un guard.

### 1.9 Nada de números mágicos estadísticos

Prohibido escribir `n_required = 20`. Se **deriva** de `(p₀, p₁, α, potencia)` con
`core/stats/power.py`, y la fórmula se guarda en `n_required_formula` para auditoría.
Lo mismo para umbrales de confianza: el único número que puede llamarse "confianza"
es `cs_lower`, la cota inferior de una confidence sequence.

Y una consecuencia que hay que aceptar por adelantado: a la frecuencia de operación
que impone la aritmética de costes (§2.2 del informe), una hipótesis de clase `EDGE`
necesita del orden de **785 observaciones, que son años**. El motor de hipótesis es
en la práctica **un detector de defectos y un filtro de riesgo**, no un descubridor
de alpha. Las hipótesis `EDGE` nacen con `effect_kind = 'observe_only'` y se quedan
ahí. Si una tarea pide que una `EDGE` "aprenda" en semanas, la tarea está pidiendo
un falso positivo.

### 1.10 La muestra de descubrimiento nunca cuenta como evidencia

Toda hipótesis lleva `discovery_cutoff_ts`. Solo las observaciones **posteriores** al
registro cuentan. Hay un `CHECK` que lo impone. Esto, solo esto, elimina la mayor
parte de los falsos positivos de un sistema de auto-aprendizaje.

### 1.11 Registra `N`

Cada configuración y cada hipótesis evaluada, **incluidas las descartadas**. El
informe de backtest **no imprime Sharpe si `N` no está registrado**; imprime Deflated
Sharpe Ratio y PBO. Un LLM que propone hipótesis barato es una máquina de inflar `N`
sin aportar evidencia.

### 1.12 Presupuesto de coste como métrica

`MAX_FIXED_COST_PCT_YEAR = 0.04` — el 4% anual del capital asignado, que es
aproximadamente el 40% del margen esperado en el escenario base. El gasto real
(API del LLM + datos + luz) se expone en Prometheus, con alerta al 80% y
degradación automática al 95%. Por debajo de $50k de capital, el stack de datos
es de $0/mes, sin excepciones.

Esto implica un capital mínimo práctico de **$9,600** con el stack recomendado de
$32/mes. El break-even puro (neto = 0) es más bajo, $3,933 en el escenario base;
esta constante es **disciplina**, no el punto de equilibrio. Excepción documentada:
los pasos 5.1 y 5.2 del plan ($500 y $2,000) están exentos porque ahí el coste es
I+D operacional; la exención caduca en 5.3 y se anota en `DECISIONS.md`.

---

## 2. Reglas de ingeniería

- **Python 3.12** (Lumibot solo está clasificado hasta 3.12; versiones más nuevas
  compiladas desde fuente suelen faltar extensiones C). `uv` + `pyproject.toml`.
- **Todo el DDL vive en `migrations/` (Alembic).** Nada de `CREATE TABLE` a mano.
- **Un servicio con claves de trading: `order-router`, `replicas: 1`.** El
  `llm-analyst` se diseña deliberadamente **sin secretos de exchange**.
- **El bus transporta eventos, Postgres es la verdad.** Nada de ticks a tasa completa
  en Redis Streams.
- **No almacenes deltas L2 crudos.** Deriva a 1s: `bid/ask/mid/spread_bps/
  depth_10bps/depth_50bps/imbalance`.
- **Backoff exponencial siempre con jitter ±30%.** Sin jitter los ingesters se
  sincronizan y te ganas un ban de IP.
- **Detección de huecos por número de secuencia**, nunca por timestamp.
- **Watchdog de WS por edad del último mensaje**, no por `on_close`. Un socket TCP
  vivo que no entrega datos es el fallo más peligroso.
- **Tests de propiedad, no ejemplos**, para `core/stats/`: FDR empírico ≤ q sobre
  ≥10,000 hipótesis nulas simuladas; cobertura de la confidence sequence ≥ 1−α;
  y `Beta(50,50)` con 5 de 5 éxitos debe dar `P(θ>0.6) = 0.058` (con el prior
  uniforme daría 0.953: esa es exactamente la diferencia que este test protege).
- **Toda llamada al LLM se registra** con `prompt_sha256`, blobs content-addressed,
  tokens, latencia y `policy_version`. La confianza declarada por el modelo se
  **guarda pero no se usa para decidir**.
- **Salida del LLM: JSON estricto vía tool use**, con opción `abstain` obligatoria y
  `rationale` con `maxLength`. El output domina la factura cuando hay caché.
- **Nunca `logs/*.csv` como interfaz entre procesos.** Eso era el patrón viejo.

---

## 3. Los tres principios heredados del `ROADMAP.md` original

Siguen siendo correctos y se mantienen:

1. **Deterministic before intelligent.** Reglas primero, LLM después, siempre con las
   reglas como baseline medible.
2. **Propose, then approve.** La automatización sugiere; un humano autoriza cualquier
   cosa que cambie el riesgo.
3. **Guards are code, not configuration.** Sin apalancamiento excesivo, sin cortos no
   autorizados, sin opciones desnudas: propiedades estructurales que sobreviven a
   cualquier edición de config.

Y uno nuevo, que sale del análisis:

4. **Measure the cost, not just the return.** Cada cifra de P&L que el sistema
   reporta es **neta de fees, financiación, slippage y coste de IA**, o no se reporta.
   Convención de signo, explícita porque equivocarse aquí hace que el dashboard
   mienta en la dirección favorable: `fees >= 0` se resta; `funding_payment` lleva
   signo (positivo = cobrado) y se suma; `slippage >= 0` para ejecución adversa y
   se resta.

---

## 4. Estado y siguiente gate

Mantén `DECISIONS.md` como bitácora append-only: una entrada por decisión con fecha,
contexto, alternativas y motivo. Al empezar una sesión, resume en qué fase estamos y
cuál es el gate de salida pendiente (están en el informe §10).

**No avances a la fase siguiente hasta que el gate de la actual esté verde y
documentado.**

---

## 5. Incógnitas abiertas

Antes de escribir código que dependa de un venue, un precio o una regla fiscal,
comprueba la §13 del informe (18 puntos de verificación). Si un punto sigue abierto y
tu tarea depende de él: **para y pregunta.** No entierres la suposición en el código.
