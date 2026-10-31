# Informe de evolución — `Multi-Asset_Trading_Bot` → plataforma multi-estrategia con capa de inteligencia

**Para:** Sebastian Sterling (SethSterling22)
**Repo analizado:** `github.com/SethSterling22/Multi-Asset_Trading_Bot` @ `1e96a76`
**Fecha:** 13 de septiembre de 2026
**Objetivo:** replicar la funcionalidad del bot "TALOS" del vídeo como una **cuarta línea de ingreso** dentro de tu plataforma, con dashboard de P&L por estrategia, desplegable en tu homelab, y ejecutable por Claude Code.

**Convención de procedencia** — respétala, es lo que separa este informe de un pitch:

| Marca | Significado |
|---|---|
| `[V]` | Fuente localizada y fechada; el dato aparece en la fuente |
| `[C]` | Cálculo propio, determinista y reproducible (el script está en el anexo A) |
| `[K]` | Conocimiento del modelo (corte mayo 2026), **no re-verificado** |
| `[?]` | Evidencia débil, contradictoria o inexistente |

⚠️ **Limitación de esta sesión:** la lectura directa de páginas web (`WebFetch`) estuvo bloqueada por la política de egress del sandbox, y el puente al folder de tu máquina (`omarchy`) está deshabilitado por política de tu organización. He leído el repo clonándolo de GitHub, y he verificado la **existencia y fecha** de las fuentes vía búsqueda, pero no he podido extraer cifras del interior de las páginas. **La §13 es una lista de 18 cosas que hay que confirmar antes de escribir una línea de código.** No la trates como opcional.

---

# 0. Resumen ejecutivo — el veredicto antes del detalle

**Lo que el vídeo enseña y lo que realmente demuestra son cosas distintas.** El bot del vídeo hizo 35 operaciones en 7 días y ganó +13% ($130.83 sobre $1,015). En la misma ventana, Bitcoin subió +21.2% (cifra del propio vídeo, `[?]` sin verificar — está en el punto 14 de la §13). **El bot perdió contra comprar y no hacer nada por 8.2 puntos porcentuales**, y el propio autor lo admite al final. Peor: con 35 operaciones no se puede distinguir habilidad de suerte. Con una volatilidad por operación del 2% (lo normal en perpetuos apalancados), el t-estadístico es **1.03** → p ≈ 0.15. Para que ese resultado significase algo harían falta **131 a 523 operaciones en total**, es decir **96 a 488 más de las que hizo** `[C]`.

**Aun así, vale la pena construirlo — pero no como está en el vídeo.** Hay tres razones:

1. **El hallazgo regulatorio de 2026 cambia el juego para ti específicamente.** Desde julio 2025 (Coinbase) y junio 2026 (Kraken) existen **perpetuos regulados por la CFTC accesibles a minoristas estadounidenses** `[V]`. Puerto Rico es jurisdicción estadounidense, así que los venues offshore del vídeo (Binance, Bybit, Hyperliquid) están cerrados para ti — pero ya no hace falta usarlos. Puedes tener cortos y financiación **legalmente, con 1099 y §1256**, en la misma API que ya usa tu repo.
2. **La diversificación de fuentes de ingreso es la tesis correcta de tu proyecto, y este módulo la mejora de verdad.** Tu Wheel es venta de volatilidad en equities (short gamma, correlacionada con el mercado). El carry de financiación de perpetuos es una prima de riesgo estructuralmente distinta. Añadirla mejora el perfil de la cartera aunque su Sharpe individual sea modesto.
3. **El panel de "hipótesis falsables" del vídeo es la mejor idea del vídeo y está mal implementada.** La tarjeta del screenshot dice "confianza 0.85, mínimo 0.60, 5 casos, hacen falta 20, le faltan 15 casos para poder cambiar algo". Eso es **estadísticamente incoherente** por seis razones distintas (§7.2). Hecho bien, con e-values y control de FDR, es un activo real y es la parte del sistema que Claude Code puede construir con más valor añadido.

**Lo que debes esperar en números** — escenario base, mis cálculos `[C]`:

| Capital | Bruto | Costes de trading | Costes fijos | **Neto anual** | **%** | Coste fijo / capital |
|---|---|---|---|---|---|---|
| $1,000 | $128 | $30 | $384 | **−$286** | **−28.6%** | 38.4% |
| $5,000 | $640 | $152 | $384 | **+$104** | **+2.1%** | 7.7% |
| $10,000 | $1,280 | $304 | $384 | **+$592** | **+5.9%** | 3.8% |
| $25,000 | $3,200 | $759 | $576 | **+$1,865** | **+7.5%** | 2.3% |
| $100,000 | $12,800 | $3,036 | $1,320 | **+$8,444** | **+8.4%** | 1.3% |

Los costes fijos son los de la **arquitectura recomendada** de §4.4 (LLM *event-gated* con modelo Haiku-class + motor nocturno en batch, stack de datos gratuito, sin GPU dedicada): $32/mes por debajo de $25k, $48/mes a $25k, $110/mes a $100k.

**Con ese stack, el capital de break-even en el escenario base es $3,933, y $12,137 en el pesimista** `[C]`. Por debajo de ~$3,000 el módulo **pierde dinero por construcción**, sin importar lo buena que sea la señal: la infraestructura y la API del LLM se comen el retorno. Y la disciplina que recomiendo es más exigente que el break-even: **coste fijo ≤ 4% anual del capital asignado**, lo que fija el mínimo práctico en **$9,600**. Ésta es la cifra que determina si tiene sentido empezar.

**Las tres decisiones de diseño que más importan**, y que contradicen el vídeo:

- **El período de tenencia óptimo es de horas a días, no minutos.** `SR_neto_max = σ·SR_t²/(4c)`. Con fees taker y una señal realista, ningún horizonte por debajo de ~4h tiene esperanza positiva `[C]`. Un bot que abre 10 operaciones al día quema **365% del notional al año** en comisiones.
- **El LLM va en la capa de meta-aprendizaje, no en el bucle de decisión.** La literatura de 2026 que audita agentes LLM de trading concluye lo contrario de la que los promociona ("The Alpha Illusion", arXiv 2605.16895) `[V]`. Y la aritmética: 16 símbolos × 96 evaluaciones/día con un modelo Sonnet-class cuesta **$1,198/mes** — 143% de una cuenta de $10k al año. Con *event-gating* al 3% y un modelo Haiku-class baja a **$15/mes** `[C]`. Mismo sistema, factor 77×.
- **El apalancamiento mata más bots que la señal.** Una estrategia con Sharpe verdadero 0.5 sobre σ=45% tiene apalancamiento growth-óptimo de **1.11×**. A 3× su crecimiento logarítmico esperado es **−23.6%/año aunque el edge sea real** `[C]`. En alts (σ=90%) a 3× es **−229%/año**. Tu decisión de "apalancamiento pequeño" es correcta; la traduzco a **≤1.5× en BTC/ETH, 1× (spot) en alts**.

**Mi recomendación sobre Claude Code: sí, pásalo a Claude Code, pero no todavía.** El repo actual son 831 líneas de Python que son un *scaffold* honesto y bien escrito. Lo que falta no es código, es **decisiones verificadas** (§13) y un contrato de arquitectura (§11). Claude Code es excelente ejecutando un plan explícito y desastroso inventando uno sobre un dominio donde el fallo cuesta dinero real. Este informe **es** ese plan: la §11 contiene el `CLAUDE.md` y los prompts de arranque.

---

# 1. Inventario verificado de lo que ya existe

Clonado y leído en su totalidad. **13 ficheros, 831 líneas de Python, 802 líneas de documentación.** El código es de buena calidad: Pydantic v2 con validadores cruzados, escritura atómica vía `os.replace`, hot-reload de config por mtime en el motor del Wheel, y un modelo de seguridad *estructural* (no configurable) que es exactamente el instinto correcto.

| Fichero | LOC | Estado real | Destino en la evolución |
|---|---|---|---|
| `config_models.py` | 127 | ✅ Completo y correcto. `TradingConfig.load/save` atómico (`tempfile` + `os.replace`), validadores cruzados (`min>max`, allocations suman 1.0, delta de cobertura menos negativo que el vendido) | **Refactorizar**, no reescribir. Se convierte en `core/config/` con secciones nuevas y, crítico, **deja de ser el único estado** (§9.1) |
| `README.md` | 102 | ✅ Honesto: declara "Status: scaffold" en la segunda línea y enumera el modelo de seguridad estructural | **Reescribir** al final, cuando la arquitectura de §6 esté en pie |
| `wheel_strategy.py` | 328 | 🟡 Ciclo completo (CSP → asignación → CC → cierre al 80%), guards de caja y cobertura, circuit breaker, y el **hot-reload de `config.json` por mtime** (`_reload_config_if_changed`). **Caveat documentado por ti mismo: `_estimate_delta()` cae a un proxy de moneyness que no es una delta** | **Mantener** como `strategies/wheel/`. Extraer guards a la base común. Arreglar la delta es Fase 0 y sigue siendo bloqueante |
| `crypto_rebalancer.py` | 195 | ✅ Lógica correcta y bien abstraída. `SpotBroker` Protocol + `PaperSpotBroker` simulado. Ventas topadas a holdings, compras topadas a USDC → sin cortos ni margen por aritmética | **Mantener y extender.** El `Protocol` es el acierto arquitectónico del repo: el `PerpBroker` nuevo es su hermano |
| `app_dashboard.py` | 181 | 🟡 3 páginas, métricas (Sharpe, max DD), consola de parámetros con revalidación Pydantic antes de escribir. **Lee `logs/equity.csv` y `logs/trades.csv` que nada escribe** | **Jubilar** (tu decisión). Se sustituye por FastAPI + frontend (§8). La lógica de `compute_metrics` se rescata a `core/metrics.py` |
| `config.json` | 37 | ✅ Fuente de verdad del estado. Ya tiene `hedged_spread_parameters` sin estrategia que los use | **Dividir**: parámetros → Postgres + config versionada; risk floor → **constantes compiladas** (§7.4) |
| `ROADMAP.md` | 289 | ✅ Excelente. Tres cosas que ya tenías bien y que este informe confirma: "deterministic before intelligent", "propose then approve", "guards are code not configuration" | **Sustituir** por el plan de §10, que lo absorbe y le añade los gates cuantitativos que le faltan |
| `RUNNING.md` | 411 | ✅ Runbook honesto, con una §0 "reality check" que enumera sus propias carencias. Poco común y muy valioso | **Mantener** y ampliar con el runbook de homelab y el de DR |
| `requirements.txt` | 8 | 🟡 `lumibot>=3.8`, `alpaca-py`, `coinbase-advanced-py`, `pydantic`, `streamlit`, `plotly`, `pandas`, `python-dotenv` | **Migrar a `uv` + `pyproject.toml`** con grupos por servicio. Quitar streamlit |
| `.env.example` | 6 | ✅ Alpaca + CDP keys, con la nota correcta de permisos read+trade | **Sustituir** por SOPS+age (§8.5). `.env` en disco es el patrón que hay que abandonar |
| `mise.toml` | 2 | Python 3.12 pinneado (correcto: Lumibot solo está clasificado hasta 3.12) | **Mantener** |

**Lo que NO existe** y el `RUNNING.md` ya reconoce: el escritor de logs, la delta real, cualquier adaptador de broker en vivo, la estrategia de spreads, tests, backtest, y toda la capa de inteligencia.

**Diagnóstico honesto:** el repo está en el punto exacto donde un scaffold bien hecho se vuelve peligroso — parece más completo de lo que está, porque la documentación es mejor que la implementación. La única vía es Fase 0 antes de cualquier cosa nueva.

---

# 2. Qué pide el vídeo, y la aritmética que lo desmonta

## 2.1 Lo que el bot del vídeo hace

Reconstruido del transcript: 16 criptomonedas; por cada una analiza mercado, derivados, volatilidad, mercados DEX, on-chain, sentimiento, noticias y macro; pasa los datos por un motor de ordenación → indicadores → filtros y controles → **analista IA (llamada a Claude)** → motor de riesgo (sizing variable) → ejecución → gestión de posición. Opera long y short. Agresividad configurable 1-10. Dos paneles de aprendizaje ("lo que va aprendiendo" / "lo que ha aprendido"). ~3-6 operaciones/día, tenencias de 30-90 minutos. Se quedó sin cuota de Claude dos veces en 7 días y el bot se paró.

## 2.2 La ecuación que decide si un bot es viable

Con `σ` = volatilidad anualizada, `c` = coste round-trip, `SR_t` = Sharpe por operación, `h` = período de tenencia en años:

```
SR_neto(h) = SR_t/√h − c/(h·σ)
```

Optimizando en `h` `[C]`:

> **`h* = 4c²/(σ²·SR_t²)`**     **`SR_neto_max = σ·SR_t²/(4c)`**

| Escenario | σ | c | SR_t | h* óptimo | **SR neto máx** |
|---|---|---|---|---|---|
| BTC perp taker/taker (10 bps), señal floja | 45% | 10 bps | 0.05 | 2.9 d | **0.28** |
| BTC perp maker/maker (4 bps), señal floja | 45% | 4 bps | 0.05 | 11 h | **0.70** |
| BTC perp taker/taker, señal buena | 45% | 10 bps | 0.10 | 17 h | **1.12** |
| BTC perp maker/maker, señal buena | 45% | 4 bps | 0.10 | 2.8 h | **2.81** |
| Alt top-50 taker (21 bps), señal buena | 90% | 21 bps | 0.10 | 19 h | **1.07** |
| Alt top-150 taker (57 bps), señal buena | 90% | 57 bps | 0.10 | 5.9 d | **0.39** |

**Tres consecuencias que hay que grabar en el diseño:**

1. El Sharpe neto escala con **`SR_t²`**: duplicar la calidad de la señal cuadruplica el resultado; reducir el coste a la mitad solo lo duplica. **La calidad de señal domina la optimización de costes**, pero el coste es lo único que controlas con certeza.
2. **El Sharpe es invariante al apalancamiento.** Apalancar no mejora nada; solo te mueve en la curva de Kelly (§4.3).
3. El horizonte óptimo cae en **horas a días**. El bot del vídeo (30-90 min) está por debajo del mínimo de break-even en casi todos los escenarios.

**Drag anual de comisiones sobre el notional, por frecuencia** `[C]`:

| ops/día | 4 bps (M/M) | 10 bps (BTC taker) | 21 bps (alt taker) |
|---|---|---|---|
| 0.14 (1 cada 7 d) | 2.0% | 5.1% | 10.7% |
| 0.25 | 3.6% | 9.1% | 19.2% |
| 1 | 14.6% | **36.5%** | 76.7% |
| 5 | 73.0% | **182.5%** | 383.2% |
| 10 | 146.0% | **365.0%** | 766.5% |

A 3× de apalancamiento multiplica por 3 sobre el equity. **Un bot a 5 ops/día, 3×, taker en BTC quema 547% del equity al año solo en fees.**

## 2.3 El resultado del vídeo, analizado

`[C]`, todo reproducible:

- Retorno geométrico por operación: `1.13^(1/35) − 1` = **35.0 bps**
- Retorno geométrico diario: **1.761%**
- Anualización naïve: `1.13^(365/7) − 1` = **58,468%** ← esta cifra sola te dice que la muestra no es informativa

| σ por operación asumida | SR_t | t (N=35) | p (1 cola) | **ops TOTALES para t=2.0** | **adicionales** |
|---|---|---|---|---|---|
| 1.0% | 0.350 | 2.07 | 0.023 ✓ | 33 | 0 |
| 1.5% | 0.233 | 1.38 | 0.088 | 74 | 39 |
| **2.0%** (realista) | **0.175** | **1.03** | **0.153** | **131** | **96** |
| 3.0% | 0.117 | 0.69 | 0.247 | 294 | 259 |
| 4.0% | 0.087 | 0.52 | 0.303 | 523 | 488 |

**Y el argumento decisivo, bayesiano:** el estimador puntual implica un Sharpe anual de ~8.4 — un valor que no existe en ninguna estrategia documentada públicamente a esa capacidad. Con un prior razonable sobre el Sharpe verdadero de un bot retail, `N(0.2, 0.4²)`, y `SE(SR_anual) = √(365/7) = 7.2` con 7 observaciones, la muestra aporta **~0.3% de la información del posterior**. Los 7 días literalmente no dicen nada `[C]`.

**Cuánto track record hace falta** — MinTRL de Bailey & López de Prado `[C]` (`α`=0.05, benchmark 0):

| Sharpe estimado | Distribución normal | Cola gorda + asimetría negativa |
|---|---|---|
| 0.5 | **13.2 años** | 20.6 años |
| 0.8 | 6.6 años | 12.0 años |
| 1.0 | **5.1 años** | 9.8 años |
| 2.0 | 3.0 años | 6.4 años |

La asimetría negativa —típica de la venta de volatilidad, que es **exactamente lo que hace tu Wheel** y lo que hace el carry de financiación— **duplica el track record requerido**. Las estrategias que más fácilmente producen un Sharpe alto en muestras cortas son precisamente las que necesitan muestras más largas para validarse.

## 2.4 Sobreajuste por multiple testing: el efecto individual más grande

Sharpe espurio esperado del mejor de `N` variantes probadas, con alpha verdadero **exactamente cero** `[C]` (aproximación de Gumbel, como en el Deflated Sharpe Ratio):

| Datos | N=10 | N=100 | **N=1,000** | N=10,000 |
|---|---|---|---|---|
| 6 meses | 2.23 | 3.58 | **4.60** | 5.46 |
| 1 año | 1.57 | 2.53 | **3.26** | 3.86 |
| 2 años | 1.11 | 1.79 | **2.30** | 2.73 |
| 5 años | 0.70 | 1.13 | 1.46 | 1.73 |

**Un grid search de 3 parámetros × 10 valores ya son 1,000 combinaciones.** Sobre 2 años de datos diarios, el mejor Sharpe esperado por puro ruido es **2.30**. Cualquier backtest de ese grid con Sharpe inferior es indistinguible del azar.

**Y aquí está el peligro específico de meter un LLM:** un LLM que genera hipótesis barato es una máquina de aumentar `N` sin aumentar la evidencia. Requisito de diseño no negociable: **el sistema registra `N` (el número real de hipótesis y configuraciones evaluadas) y reporta Deflated Sharpe Ratio + PBO, no Sharpe.** Si no se registra `N`, el DSR es incalculable y ningún backtest contiene información.

---

# 3. Decisión de venue — el hallazgo que cambia el proyecto

**Puerto Rico es jurisdicción estadounidense.** Para la CFTC, FinCEN y los términos de servicio de cualquier exchange, eres *US person*. La ventaja de vivir en PR es exclusivamente fiscal; no da acceso a venues offshore.

## 3.1 Lo que cambió en 2025-2026 `[V]`

| Fecha | Evento |
|---|---|
| **21 jul 2025** | **Coinbase lanza perpetual-style futures para minoristas de EE.UU.** — los primeros perpetuos regulados por la CFTC. 10× apalancamiento, 0.02% de fee al lanzamiento |
| oct 2025 | Kraken compra Small Exchange (DCM de la CFTC) por $100M → Kraken Derivatives US |
| **6 may 2026** | **Kraken lanza margen spot regulado por la CFTC a toda su base de clientes de EE.UU.**, incluyendo venta en corto |
| 29 may 2026 | CME lanza futuros y opciones cripto **24/7**; CFTC Letter 26-17 clasifica ciertos perpetuos como *foreign futures* |
| 3 jun 2026 | Policy Statement de la CFTC sobre listado de contratos perpetuos (Federal Register 2026-11020) |
| **jun 2026** | **CME demanda a la CFTC** por la aprobación de los perpetuos de bitcoin (argumenta que son *swaps* Dodd-Frank, no futuros) ← **riesgo de litigio vivo sobre toda la estructura onshore** |
| **15 jun 2026** | **Kraken lanza perpetuos de EE.UU. regulados por la CFTC** |
| 10 feb 2026 | IBKR añade los nano y perpetual-style futures de Coinbase Derivatives |

## 3.2 Matriz de disponibilidad para ti

| Venue | Spot a PR | Perps a minorista de EE.UU. | Veredicto |
|---|---|---|---|
| **Coinbase Advanced + Coinbase Derivatives (CFM)** | ✅ | **✅ CFTC-regulado** `[V]` | 🥇 **Primera opción** |
| **Kraken Pro + margen spot US + Kraken Derivatives US** | ✅ | **✅ CFTC-regulado** `[V]` | 🥈 Segunda, y **mejor testnet** |
| Interactive Brokers (CME micro + Coinbase nano) | ✅ | ✅ §1256, 1099 limpio | 🥉 Máxima claridad fiscal, peor API y granularidad |
| Alpaca (lo que usa tu repo) | ✅ spot | Endpoint de perps existe bajo `/us/`; **elegibilidad minorista no verificada** `[?]` | Mantener para spot y para el Wheel |
| Binance.US | ✅ spot | ❌ sin derivados | Solo spot |
| Binance.com, Bybit, OKX global, Bitget, Gate | ❌ | ❌ | **Prohibido a US persons** |
| Hyperliquid, dYdX, GMX, Jupiter, Drift | ❌ geobloqueo | ❌ | **No operar. Sí usar como fuente de datos gratis** |
| SGX vía FBOT | — | Solo instituciones (ECP) | No aplica |

**Por qué Coinbase es la elección:** único venue donde spot y perpetuos regulados viven detrás de **una API, un SDK y un esquema de auth** — `coinbase-advanced-py`, que **ya está en tu `requirements.txt`**, y tu propio docstring en `crypto_rebalancer.py` ya lo nombra como objetivo. El coste de migración es el más bajo de todas las opciones: el `SpotBroker` Protocol ya existe; solo hay que escribir su hermano `PerpBroker`.

**Lo que tienes que asumir del camino onshore:**
- Los "perpetuos" de Coinbase son técnicamente **futuros de vencimiento largo con mecanismo de financiación**. Los tickers llevan código de expiración (p.ej. `BIP-20DEC30-CDE`) `[V]`. Tu modelo de datos debe manejar símbolo + expiración + roll en 2030 + devengo de financiación. No es un perp de Binance.
- **No hay margen aislado nativo.** Coinbase usa margen de cartera FCM. La sustitución práctica es **un sub-portfolio por estrategia + caps duros de notional y pérdida en tu propia capa de riesgo** — que es justo donde tu repo ya tiene el andamiaje (`max_portfolio_risk_per_trade`, `daily_loss_limit_percentage`, `emergency_kill_switch`). Presupuesta tiempo de ingeniería aquí; es la mayor regresión funcional frente a un venue offshore.
- **La demanda CME↔CFTC es un riesgo estructural real.** Diseña el adaptador para que el venue de perpetuos sea **intercambiable** desde el primer día.

## 3.3 Sobre usar un VPN hacia un venue offshore

No lo hagas, y la razón principal no es la que crees:

1. **Contractual:** incumplimiento plano de los términos. Resultado realista: posición cerrada a la fuerza, **cuenta congelada, retiro bloqueado "pendiente de revisión de compliance", beneficios confiscados**, y sin foro donde reclamar.
2. **Regulatorio:** históricamente la CFTC ha acusado a los venues, no a los usuarios (ver CFTC v. KuCoin). Pero el marco de 2026 existe precisamente para crear una vía legal — lo que hace que la evasión deliberada se vea **peor**, no mejor.
3. **Operativo:** un homelab 24/7 es un patrón de IP estable y atribuible. La aplicación del geobloqueo en 2026 es conductual, no solo por IP.
4. **Y la decisiva para ti:** pierdes 1099 y §1256, y ganas análisis de FBAR/Form 8938. **Cuando tu tesis completa es una postura fiscal en PR que quieres a prueba de auditoría, el peor riesgo ajustado de esta lista es exactamente este.**

## 3.4 Fiscal — una alerta con fecha

⚠️ **No es asesoría legal ni fiscal. Esto es lo urgente:** la **Ley 38-2026** modificó la Ley 60 `[V]`. El programa de Inversionista Residente se extiende hasta 2055, **pero la tasa de 0% sobre ganancias de capital expira el 31 de diciembre** y se introduce un régimen del 4% para nuevos solicitantes. Las fechas exactas de vigencia y el *grandfathering* son `[?]` y contradictorios entre fuentes.

Además, tres cosas que un bot complica y que un CPA de PR debe resolver **antes** de que opere con capital real:
- **Carácter de la renta:** un bot de alta frecuencia puede generar renta de *negocio*, no ganancia de capital. El beneficio de 0%/4% de la Ley 60 es sobre *capital gains*; puede no alcanzar a esto.
- **§1256:** los futuros de CME y de Coinbase Derivatives son generalmente contratos §1256 → **mark-to-market a fin de año + reparto 60/40**. Su interacción con el sourcing de §933 para un residente bona fide de PR no es obvia, y es justo donde aterrizan las auditorías (Holland & Knight publicó dos veces sobre el escrutinio del IRS a la Ley 60, nov-2025 y jun-2026) `[V]`.
- **La apreciación previa a la mudanza no está cubierta** (§937(d), regla de los 10 años).

**Acción:** esto va a un abogado fiscal de PR **este mes**, no el año que viene. Y el diseño del bot debe emitir desde el día uno un export de operaciones apto para tu CPA (está en Fase 3, §10).

---

# 4. Modelo de rentabilidad

## 4.1 Qué estrategias sobreviven a los costes a tu tamaño

Síntesis de la investigación, con el veredicto para cuentas de $1k–$25k:

| Estrategia | Sharpe bruto realista | Sharpe neto | Retorno neto | **¿Viable a $1k–25k?** | Confianza |
|---|---|---|---|---|---|
| **Carry de financiación (delta-neutral)** | 1.5–2.5 | 1.0–2.0 | 5–15% | **Sí desde ~$10k.** Mejor respaldo académico (BIS WP 1087, "Crypto carry") | `[K]` media |
| **Momentum cross-seccional (semanal)** | 1.0–2.0 | **0.4–0.8** | 10–25% | **Sí — la mejor candidata direccional.** Con una restricción importante: la pata corta solo en **perpetuos de BTC/ETH** (donde el risk floor permite 1.5×); en alts es long/flat, porque el floor los limita a spot 1× y en spot sin margen no se puede vender en corto | `[K]` media |
| **Trend / vol breakout (días-semanas)** | 0.5–1.0 | **0.3–0.7** | 8–20% | **Sí — segunda mejor** | `[K]` media |
| Mean reversion en alts (≥4h) | 0.8–1.5 | 0.3–0.7 | 8–20% | Sí, **solo** con horizonte ≥4h | `[K/?]` baja |
| Market making | 4–10 (con rebate) | **Negativo** | — | **NO.** Tick de BTCUSDT perp = 0.10 USDT sobre ~$110k = **0.0091 bps**; spread de 1-3 ticks = 0.01–0.03 bps; fee maker VIP0 = **2 bps = 220× el tick**. Capturas centésimas de bps y pagas 2 | `[C]` sobre tick `[K]` |
| Arb cross-exchange / triangular | 3–6 (con co-location) | **≈0** | — | **NO.** Requiere latencia <5 ms e inventario en N venues | `[V]` alta |
| Fading de cascadas de liquidación | **No medible** | — | — | **NO validable**: 3–8 eventos/año, y los papers de 2026 demuestran heterogeneidad entre eventos | `[C]` alta |
| **Benchmark: buy-and-hold BTC** | **~1.0** | **~1.0** | histórico alto, DD −77%/−84% | **La barra a superar** | `[K]` media |

**El dato incómodo:** buy-and-hold BTC da Sharpe ~1.0 con **0 bps de coste, 0 riesgo operacional, 0 coste de infraestructura y 0 riesgo de sobreajuste**. El techo teórico de un bot retail taker/taker en BTC con señal floja es **0.28**. Un bot retail debe superar un benchmark que está fuera de su alcance teórico, salvo que consiga fees maker y `SR_t ≥ 0.10`.

**La lectura constructiva de eso:** no compitas contra BTC en el eje de Sharpe direccional. Compite en el eje de **descorrelación**. El carry de financiación es delta-neutral; su valor en tu cartera no es batir a BTC, es dar retorno cuando tu Wheel está en drawdown. Esa es la tesis correcta de un proyecto multi-activo y es lo que justifica el módulo.

## 4.2 Modelo de P&L, tres escenarios

Supuestos del modelo `[C]` — sleeves, brutos anuales, round-trips efectivos/año y coste round-trip:

| Sleeve | Peso | Bruto base | RT/año | Coste RT |
|---|---|---|---|---|
| Carry de financiación | 40% | 10% | 6 | 33 bps (spot+perp, ambas patas) |
| Momentum cross-seccional | 40% | 16% | 21 (semanal, 40% de rotación) | 21 bps (alt top-50) |
| Trend BTC/ETH | 20% | 12% | 24 | 10 bps |

Costes fijos = la **arquitectura recomendada de §4.4**, no una estimación genérica: LLM event-gated Haiku-class ($15/mes hasta $25k, $31/mes a $25k, $56/mes a $100k), datos $0 por debajo de $50k y $29/mes por encima, electricidad $17/mes sin GPU dedicada y $25/mes con ella.

**Escenario pesimista** (bruto ponderado 6.20%, drag de trading 3.04%, margen **3.16%**):

| Capital | Neto anual | % |
|---|---|---|
| $1,000 | −$352 | −35.2% |
| $5,000 | −$226 | −4.5% |
| $10,000 | −$68 | −0.7% |
| $25,000 | +$215 | +0.9% |
| $100,000 | +$1,844 | +1.8% |

**Escenario base** (bruto 12.80%, drag 3.04%, margen **9.76%**):

| Capital | Bruto | Trading | Fijos | **Neto** | **%** |
|---|---|---|---|---|---|
| $1,000 | $128 | $30 | $384 | **−$286** | −28.6% |
| $5,000 | $640 | $152 | $384 | **+$104** | +2.1% |
| $10,000 | $1,280 | $304 | $384 | **+$592** | +5.9% |
| $25,000 | $3,200 | $759 | $576 | **+$1,865** | +7.5% |
| $100,000 | $12,800 | $3,036 | $1,320 | **+$8,444** | +8.4% |

**Escenario optimista** (bruto 19.60%, margen **16.56%**):

| Capital | Neto anual | % |
|---|---|---|
| $1,000 | −$218 | −21.8% |
| $5,000 | +$444 | +8.9% |
| $10,000 | +$1,272 | +12.7% |
| $25,000 | +$3,565 | +14.3% |
| $100,000 | +$15,244 | +15.2% |

**Desglose por sleeve a $25,000, escenario base:**

| Sleeve | Asignado | Bruto | Costes de trading | **Neto** | **% sobre el sleeve** |
|---|---|---|---|---|---|
| Carry | $10,000 | $1,000 | $198 | $802 | 8.02% |
| Momentum X-sec | $10,000 | $1,600 | $441 | $1,159 | 11.59% |
| Trend | $5,000 | $600 | $120 | $480 | 9.60% |
| **Total** | **$25,000** | **$3,200** | **$759** | **$1,865 (tras $576 de fijos)** | **7.46%** |

## 4.3 Capital de break-even — la tabla que decide si empezar

`[C]`, escenario base:

Hay **dos umbrales distintos** y conviene no confundirlos. El **break-even** es donde el neto cruza cero. El **presupuesto de disciplina** es más exigente a propósito: acota qué fracción del margen esperado se va en costes fijos.

| Coste fijo mensual | Coste anual | Break-even (base) | Break-even (pesimista) | Mínimo con la regla del 4% |
|---|---|---|---|---|
| $17/mo (luz sola, LLM local, datos gratis) | $204 | **$2,089** | $6,448 | $5,100 |
| **$32/mo (arquitectura recomendada)** | **$384** | **$3,933** | **$12,137** | **$9,600** |
| $48/mo | $576 | $5,899 | $18,205 | $14,400 |
| $67/mo | $804 | $8,234 | $25,411 | $20,100 |
| $100/mo | $1,200 | $12,290 | $37,927 | $30,000 |

**Regla operativa que sale de aquí y que va al `CLAUDE.md` y al risk floor:**

> El coste fijo mensual total del módulo no debe exceder el **4% anual del capital asignado** — aproximadamente el 40% del margen del escenario base, dejando el 60% como retorno. A $10,000 eso son **$33/mes**; a $25,000, $83/mes. El presupuesto se vigila como una métrica de Prometheus con alerta al 80% y degradación automática al 95%, no como una intención.

Eso implica, sin ambigüedad: **stack de datos gratis por debajo de $50k de capital**, LLM *event-gated* con modelo Haiku-class, y el modelo caro reservado al motor nocturno en batch. Y una excepción documentada: los pasos 5.1 y 5.2 de la Fase 5 ($500 y $2,000) **están exentos de la regla**, porque ahí el coste es I+D operacional, no drag de una estrategia en producción. La exención se anota en `DECISIONS.md` y caduca al llegar al paso 5.3.

## 4.4 Coste del LLM — el factor 40×

`[C]`, modelo Sonnet-class ($2/$10 por MTok) con caché de 1h, prefijo cacheable de 10k tokens:

| Cadencia | $/día | **$/mes** | escritura de caché | lectura | fresco | output |
|---|---|---|---|---|---|---|
| Rejilla 16 sym × 96/día (1,536 llamadas), prosa 1k tok | $39.94 | **$1,198** | 38.5% | 7.7% | 15.4% | **38.5%** |
| Misma rejilla, JSON estricto 250 tok | $28.42 | **$852** | 54.1% | 10.8% | 21.6% | 13.5% |
| **Event-gated 8% (123/día), JSON 250** | **$2.25** | **$67** | **53.4%** | 10.9% | 21.9% | 13.7% |
| **Event-gated 3% (46/día), JSON 250** | **$1.03** | **$31** | **62.1%** | 8.9% | 17.8% | 11.2% |
| Event-gated 8% con modelo Haiku-class | $1.12 | **$34** | — | — | — | — |
| **Event-gated 3% con Haiku-class** | **$0.52** | **$15** | — | — | — | — |
| Motor nocturno L5: 30 llamadas/día (30k in / 3k out) con Batch −50% | $1.35 | **$41** | — | — | — | — |

**Las dos palancas, cuantificadas por separado** `[C]`:

1. **La cadencia domina, y por un margen enorme.** Manteniendo el formato constante (JSON → JSON), pasar de rejilla a event-gated al 8% ahorra **92.1%**. Y es mejor ingeniería, no solo más barato: 96 evaluaciones al día por símbolo donde nada ha cambiado son 96 oportunidades de que el modelo invente una razón para actuar. **La llamada se dispara solo cuando un detector determinista de novedad lo justifica** (cruce de umbral, noticia nueva, cambio de régimen, anomalía de ejecución).
2. **El formato de salida es la segunda palanca: 28.8%** de ahorro al pasar de prosa a JSON estricto con el razonamiento en un campo `rationale` con `maxLength`. Ojo con la intuición fácil: **el output no domina la factura**. Con caché de 1h activo, el componente mayor son las **escrituras de caché** (53–62% en la configuración recomendada) y el output es el 11–14%. Combinando ambas palancas el ahorro total es del **97.4%**.

**Arquitectura de coste recomendada: ~$56/mes** — Haiku-class event-gated en la capa de gate ($15) + Sonnet-class en batch nocturno para el motor de hipótesis ($41), frente a $1,198/mes de la implementación naïve. **Factor 21×.** Y si el motor nocturno se reduce a 10 llamadas/día durante las primeras fases, el total baja a **$29/mes**, que es la cifra usada en el modelo de §4.2 para cuentas pequeñas.

**Sobre el LLM local** (tu respuesta incluía las tres opciones, y la correcta es el router híbrido): la electricidad es irrelevante — ~$0.022/MTok frente a $1–10/MTok de API, factor 50–450× `[C]`. El coste real del local es el hardware amortizado y tu tiempo de operación. Una RTX 3090 24/7 en Puerto Rico cuesta **$200–650/año solo de luz** `[C]`, que a $10k de capital es 2–6% anual de drag. **Dónde sí gana el local, y por eso va en el diseño:** como **último escalón del fallback ladder**. Un modelo 7–14B local que nunca da 429 ni 529 es exactamente lo que evita el fallo del vídeo (quedarse sin cuota y parar). Mantenlo corriendo en *shadow mode* permanente: te da fallback **y** una medida continua de cuánto valor aporta realmente el modelo caro.

## 4.5 Sizing: la parte que mata más bots que la señal

Kelly continuo, `g(f) = f·μ − f²σ²/2`, máximo en `f* = SR/σ` `[C]`:

| Estrategia | σ | **f\* (Kelly pleno)** | ½-Kelly | g(1×) | **g(1.5×)** | g(2×) | **g(3×)** |
|---|---|---|---|---|---|---|---|
| SR 0.3, BTC | 45% | 0.67× | 0.33× | +3.4% | +1.5% | −7.8% | **−36.8%** |
| SR 0.5, BTC | 45% | **1.11×** | 0.56× | +12.4% | **+11.9%** | +4.5% | **−23.6%** |
| SR 0.8, BTC | 45% | 1.78× | 0.89× | +25.9% | +31.2% | +31.5% | +16.9% |
| SR 1.0, BTC | 45% | 2.22× | 1.11× | +34.9% | +44.7% | +49.5% | +54.0% |
| SR 0.5, alt | 90% | **0.56×** | 0.28× | +4.5% | **−22.9%** | −72.0% | **−229.5%** |

El límite de 1.5× que recomiendo es el punto donde una estrategia con Sharpe verdadero de 0.5 en BTC sigue creciendo (+11.9%), y donde una con Sharpe 0.3 ya solo gana +1.5% — es decir, es el apalancamiento que sobrevive a que el Sharpe real sea mucho peor que el estimado. En alts a 1.5× ya se destruye capital, y de ahí viene el límite de 1× (spot, sin margen) para esa clase.

**Y `f*` se estima con `ŜR`, que está sesgado al alza por el sobreajuste de §2.4. Usar ½-Kelly no es conservadurismo, es corrección del sesgo de estimación.**

**Distancia de liquidación** `[C]`, margen aislado, `1/L − m`:

| L | BTC (m=0.4%) | nº de σ diarias BTC | Alt (m=1.0%) | nº de σ diarias alt |
|---|---|---|---|---|
| 1× | 99.6% | 41.5σ | 99.0% | 19.8σ |
| **1.5×** | **66.3%** | 27.6σ | **65.7%** | 13.1σ |
| 2× | 49.6% | 20.7σ | 49.0% | **9.8σ** |
| **3×** | **32.9%** | 13.7σ | **32.3%** | **6.5σ** |
| 10× | 9.6% | 4.0σ | 9.0% | **1.8σ** |

El 10 de octubre de 2025 se liquidaron **$19B** y muchos alt perps imprimieron *wicks* de −40% a −90%, con **ADL socializando pérdidas incluso en libros cubiertos** `[V]`. **3× aislado en un alt liquida a −32.3%: no era superviviente ese día.**

→ **Límite duro recomendado: 1.5× en BTC/ETH, 1× (spot, sin margen) en alts.** Y asumir que el margen aislado puede perderse íntegro en un evento de cola.

## 4.6 El mito de la diversificación en 16 símbolos

`σ_cartera = σ·√[(1+(N−1)ρ)/N]`, `N_efectivo = N/(1+(N−1)ρ)` `[C]`:

| N | ρ=0.7 | ρ=0.8 | ρ=0.9 |
|---|---|---|---|
| **N_efectivo con N=16** | **1.39** | **1.23** | **1.10** |
| σ_cartera/σ con N=16 | 0.848 | 0.901 | 0.952 |

**Una cartera de 16 alt perps con correlación media 0.8 tiene 1.23 apuestas independientes y una volatilidad del 90.1% de una única posición.** Los 16 símbolos del vídeo no diversifican nada: reducen la volatilidad un 10%, no el 75% que sugeriría `√(1/16)`. En eventos de estrés la correlación pasa de 0.95 y `N_efectivo → 1.05`.

**Implicación de diseño:** el límite de riesgo debe fijarse sobre el **riesgo de cartera ajustado por correlación**, no por posición. Un cap del 1% por posición con 16 posiciones a ρ=0.8 da un riesgo de cartera efectivo de **3.61%**, y a ρ=0.9 (el régimen de estrés) de **3.81%** `[C]` — no 1%, y tampoco el 4% que saldría de ignorar la correlación por completo. `MAX_CORRELATION_ADJUSTED_RISK_PCT = 0.04` en el risk floor está fijado contra el caso de estrés con margen, no contra el caso normal. Esto va en el `allocator` (§9.3) y necesita la matriz de correlación rodante como input. Nótese que `MAX_OPEN_POSITIONS = 8` hace que 16 posiciones simultáneas sean imposibles: la tabla se incluye porque es el diseño del vídeo, y el cálculo a N=8 da ρ=0.8 → N_ef 1.19 y ratio 0.917.

## 4.7 Racha perdedora esperada — calibra el riesgo por operación con esto

`Racha máxima ≈ ln(N(1−q))/ln(1/q)` `[C]`. Drawdown implicado según fracción de riesgo `r`:

Rachas máximas esperadas `[C]`:

| Win rate | N=100 | N=500 | N=1,000 | N=2,000 |
|---|---|---|---|---|
| 55% | 5.0 | 7.0 | 7.9 | 8.8 |
| 45% | **6.4** | 9.1 | **10.2** | 11.4 |
| 35% (trend) | 8.3 | 12.0 | **13.6** | 15.2 |

Drawdown implicado, `(1−r)^k − 1`:

| r por operación | racha 6 | racha 10 | racha 14 |
|---|---|---|---|
| 0.5% | −3.0% | −4.9% | −6.8% |
| **1.0%** | **−5.8%** | **−9.6%** | **−13.1%** |
| 2.0% | −11.4% | −18.3% | −24.6% |
| 5.0% | −26.5% | −40.1% | **−51.2%** |

**Una estrategia de trend con 35% de win rate y 5% de riesgo por operación experimentará, con alta probabilidad, un drawdown superior al 50% dentro de sus primeras 1,000-2,000 operaciones, por pura secuencia y sin que nada esté roto.** Con `r=1%`, el mismo escenario da −13%. Tu `max_portfolio_risk_per_trade` actual de 0.02 está en el límite alto; recomiendo **0.005–0.01** para el sleeve direccional.

Y una nota honesta: **la evidencia de que un límite de pérdida diaria mejore el Sharpe es débil o inexistente** `[?]`. Mecánicamente trunca la cola izquierda pero también elimina la recuperación por reversión. La justificación defendible no es de alpha, es de **riesgo operacional**: un límite del 2-3% acota el daño de un bug de estado, de datos rancios o de un websocket muerto. Documéntalo así en el código, para que nadie lo "optimice" más tarde creyendo que es una fuente de retorno.

---

# 5. Veredicto sobre el LLM y su rol correcto

## 5.1 La evidencia

**El paper fundacional:** Lopez-Lira & Tang, "Can ChatGPT Forecast Stock Price Movements?" (arXiv 2304.07619, publicado en *Journal of Financial Economics*) `[V]`. Reporta que el sentimiento de ChatGPT sobre titulares predice retornos del día siguiente, con una cartera long-short de Sharpe ~3.3 **sin costes de transacción** `[K]`.

**Las cinco objeciones que hay que aplicarle:** muestra de ~15 meses (MinTRL para SR 3.3 ≈ 3.4 años); el alpha se concentra en microcaps ilíquidas, justo donde el impacto de mercado es 10–100× mayor; turnover diario → drag de 50–150%/año sobre el notional; **fuga de información desde el preentrenamiento** (documentada en "Profit mirage: Revisiting information leakage in LLM-based financial agents") `[V]`; y réplicas independientes encuentran que el efecto se concentra en iliquidez y decae.

**La literatura de 2026 es notablemente escéptica** `[V]`:
- **"The Alpha Illusion: Reported Alpha from LLM Trading Agents Should Not Be Treated as Deployment Evidence"** (arXiv 2605.16895). El título es la conclusión.
- **"Artificial Intelligence in Equity and Crypto Markets: Progress, Profitability Evidence, and the Limits of Automated Investing"** (arXiv 2609.04917, sep-2026) — la referencia más reciente y directamente on-topic. **Léela antes de construir.**
- "From Hypotheses to Factors: Constrained LLM Agents in Cryptocurrency Markets" (arXiv 2604.26747) — el LLM como generador de hipótesis **restringido**: el enfoque defendible.
- Jonathan Kinlay, "Your Research Agent Is an Undisclosed Factor Exposure" (ago-2026) `[?]` pero conceptualmente fuerte: si todo el mundo usa el mismo LLM para generar estrategias, las estrategias resultantes están correlacionadas → exposición factorial no declarada y *crowding*.

**El argumento de latencia, que es el más limpio contra el sentiment-trading LLM en cripto** `[C]`: en BTC, con σ anual 45%, σ a 5 min = **13.9 bps** y σ a 1 min = 6.2 bps. Si el impacto informativo de un titular se incorpora en 1–5 minutos para un activo de alta atención, un pipeline con 1–10 s de inferencia más una cadencia de decisión de minutos llega tarde a una fracción sustancial del movimiento. La ventana explotable sobreviviente está en **alts de baja atención** — donde el coste round-trip es de 21–57 bps, es decir **1.5–4× la σ a 5 min de BTC**. **Las dos condiciones (atención baja para tener edge, liquidez alta para pagar el coste) son mutuamente excluyentes.**

## 5.2 Ranking de roles por valor esperado

| # | Rol | EV | Riesgo de ruina | Testeable offline | Veredicto |
|---|---|---|---|---|---|
| 1 | **Feature extractor** (noticias/funding/social → JSON: `event_type`, `polarity`, `magnitude`, `horizon`, `entities`) | Alto | **Muy bajo** | **Sí** (dataset etiquetado, F1, IC del feature) | **Primero.** Único rol con soporte académico real |
| 2 | **Diagnosticador post-hoc + proponente de hipótesis** (offline, sin autoridad de ejecución) | Alto | **Cero** por construcción | Sí | **Segundo.** Es el motor de tus dos paneles |
| 3 | **Veto/gate unidireccional** sobre señales deterministas (solo puede **reducir** exposición) | Medio | Bajo y acotado | Parcial (shadow) | **Tercero**, con `fail_open` |
| 4 | **Narrador de régimen** → etiqueta discreta que selecciona un preset | Medio | Medio (whipsaw de parámetros) | Sí | Requiere **histéresis** + detección de drift |
| 5 | **Proponente de config diffs con aprobación humana** | Medio | Medio (riesgo **operativo**: fatiga de aprobación) | Sí | Sí, con firma y límites duros |
| 6 | **Generador directo de señal / sizer** | **Bajo o negativo** | **Alto** | No (look-ahead) | **No lo hagas.** Es lo que la literatura crítica desmonta |

## 5.3 El principio de diseño que lo resume todo

> **Espacio de acción monótono: todo efecto derivado de un LLM sobre una decisión de trading debe ser monótono hacia menos riesgo.**

El LLM **puede**: vetar una entrada, reducir tamaño (multiplicador en `[0.25, 1.00]`), estrechar un stop, bloquear un componente, ampliar el cooldown, forzar abstención.
El LLM **nunca puede**: abrir una posición que la lógica determinista no propuso, aumentar tamaño por encima de 1.0×, ensanchar un stop, levantar un límite, aumentar apalancamiento, modificar el risk floor.

**Con esta única restricción, el peor caso de un LLM completamente alucinado es un bot que no opera** — un fallo recuperable. Sin ella, el peor caso es la cuenta a cero. Esta restricción se implementa como un `CHECK` en la base de datos y un test unitario, no como una convención.

## 5.4 Donde el LLM sí paga, medido por retorno sobre coste

Lo más rentable de todo, y merece decirse explícitamente: **generación de código, revisión de infraestructura y detección de bugs de estado.** Eliminar un bug de doble contabilización de posiciones tras un reinicio vale más que cualquier señal que el modelo pueda producir. Ese es literalmente lo que estás haciendo al usar Claude Code para construir esto — y es el uso con mejor ROI demostrable de un LLM en un bot de perpetuos.

---

# 6. Arquitectura objetivo

```
┌─ L0  MARKET DATA / EXECUTION  (determinista, sin LLM, nunca) ────────────┐
│  ohlcv · orderbook derivado · funding · OI · liquidaciones               │
│  fills · positions · risk accounting                                     │
└──────────────────────────────────────────────────────────────────────────┘
┌─ L1  FEATURE LAYER ──────────────────────────────────────────────────────┐
│  features numéricas (deterministas)                                      │
│  + llm_text_features   ← ROL 1  (async, cacheado, TTL, versionado)      │
│  + regime_label        ← ROL 4  (horario, con histéresis, drift-monitor) │
└──────────────────────────────────────────────────────────────────────────┘
┌─ L2  ESTRATEGIA DETERMINISTA  (la baseline que hay que batir) ───────────┐
│  signal(features, params) -> Intent{side, size, stop, tp}                │
│  carry · xsec_momentum · trend · wheel · crypto_rebalance                │
└──────────────────────────────────────────────────────────────────────────┘
┌─ L3  GATE LAYER  ← ROL 3  (solo monótono a menos riesgo) ────────────────┐
│  active_hypotheses.apply(Intent) -> Intent'   (efectos acotados)         │
│  llm_gate(Intent', context) -> {allow | veto | shrink | abstain}         │
│  fail_open si reduce riesgo, fail_closed si lo aumenta                   │
└──────────────────────────────────────────────────────────────────────────┘
┌─ L4  RISK FLOOR INMUTABLE  (constantes compiladas, hash verificado) ─────┐
│  max_notional · max_leverage · daily_loss_stop · max_open · min_liq_dist │
│  si se viola -> rechaza + alerta. NO configurable en runtime.            │
└──────────────────────────────────────────────────────────────────────────┘
┌─ L5  LEARNING LOOP  (offline, nocturno, Batch API) ← ROL 2 + 5 ──────────┐
│  outcome ledger -> detección de anomalías -> el LLM propone hipótesis    │
│  -> motor estadístico (e-values, e-BH, drift) -> transiciones de estado  │
│  -> efectos activados solo dentro del vocabulario acotado de L3          │
│  -> config diffs -> cola de aprobación humana                            │
└──────────────────────────────────────────────────────────────────────────┘
```

**La nota clave:** el motor estadístico de L5 **no es el LLM**. El LLM propone texto; código determinista y auditable decide si una hipótesis se confirma. Ese es exactamente el diseño de **POPPER** (Huang et al., ICML 2025, arXiv 2502.09858, código en `github.com/snap-stanford/POPPER`) `[V]`, y es lo que separa un sistema de aprendizaje de un generador de narrativas plausibles.

---

# 7. El motor de hipótesis falsables, hecho con rigor

Esta es la parte donde más valor puedes añadir sobre el vídeo, y es la que Claude Code puede construir mejor porque es pura ingeniería verificable.

## 7.1 Tres clases de hipótesis con estadística distinta

El error conceptual más grave del panel del vídeo es tratar con el mismo aparato dos cosas incompatibles. La hipótesis del screenshot —5 posiciones cerradas por `risk_override` con `hold_s ≤ 4`, `MAE_r = MFE_r = 0`, `r ∈ [−0.21, −0.16]`— **no es un patrón de mercado**. `MAE_r = MFE_r = 0` significa que **el precio no se movió**: es un bucle abrir-cerrar que solo paga comisiones. **Eso es un bug**, y la acción correcta es arreglar el código, no "aprender un patrón que entra en cada decisión".

```python
class HypothesisClass(Enum):
    DEFECT   # mecánico, casi determinista, sobre el propio sistema.
             # n_required de binomial exacto: 3-5 casos BASTAN.
             # Efecto: abrir ticket + circuit-breaker del componente.
             # NUNCA se convierte en "patrón aprendido".
    RISK     # evitación de pérdida, pérdida asimétrica.
             # Umbral de evidencia MEDIO (el coste de un falso positivo
             # es coste de oportunidad).  Efecto: veto / shrink / cooldown.
    EDGE     # afirmación de alpha. Umbral ALTO.
             # n_required de test de potencia + e-BH FDR: 200-800 casos.
             # Efecto: como máximo, priorización entre señales existentes.
```

## 7.2 Los seis defectos de la tarjeta del vídeo

1. **"confianza 0.85" no es una posterior.** Es un número que el LLM *dijo*. Los LLM están sistemáticamente sobreconfiados. Una posterior es una función determinista de (prior, n, éxitos); no se le pregunta a nadie.
2. **Dos puertas redundantes e inconsistentes.** La tarjeta tiene `confianza 0.85 ≥ mínimo 0.60` (**pasada**) y a la vez `5 de 20 casos` (**no pasada**), y concluye "le faltan 15 casos". Si la confianza fuese una posterior, sería monótona creciente en `n` y **una sola puerta bastaría**. Tener dos significa que la confianza es decorativa.
3. **`n_required = 20` es una constante arbitraria.** No se deriva de ningún tamaño de efecto ni potencia.
4. **Los 5 casos son un clúster, no 5 observaciones.** Mismo día (`day_utc: 2026-08-19`), mismo componente. `n_eff = 5/(1+4·0.9) = ` **1.09**.
5. **Confirmación sobre la muestra de descubrimiento.** La hipótesis fue inducida de esos 5 casos y el scope los *incluye*. Es circular: la evidencia está garantizada de antemano.
6. **Sin control de multiplicidad.** La tarjeta confirmada ("patrón · 38 casos · confianza 0.80 · desde entonces entra en cada decisión") no muestra q-valor, ni e-value, ni cuántas hipótesis compiten. Con 0.80 de confianza y decenas de hipótesis concurrentes, la probabilidad de que al menos una confirmada sea espuria es cercana a 1.

**La cuenta que hay que ver:** un bot que genera 30 hipótesis/semana genera ~1,500/año. Al 5% sin corrección, eso son **75 falsos positivos "confirmados" al año entrando en cada decisión**. Con el tiempo, el panel "LO QUE HA APRENDIDO" se convierte en un conjunto de restricciones basadas en ruido y el bot deja de operar (o opera peor) sin que nadie sepa por qué. **Esto no es un riesgo hipotético; es el destino por defecto del diseño del vídeo.**

## 7.3 Cuánta evidencia hace falta de verdad

**Test de proporción, `p₀`=0.50, potencia 0.80** `[C]`:

| MDE (`p₁`) | α=0.05 | α=0.005 | α=0.001 |
|---|---|---|---|
| 0.55 (+5 pp) | **783** | 1,329 | 1,704 |
| 0.60 (+10 pp) | 194 | 330 | 424 |
| 0.65 (+15 pp) | 85 | 145 | 187 |
| 0.70 (+20 pp) | 47 | 81 | 104 |
| 0.80 (+30 pp) | **20** | 34 | 44 |

**Lectura: "20 casos" solo es suficiente si crees que el efecto es de +30 puntos porcentuales de hit rate y no corriges por multiplicidad.** Ninguna ventaja real en cripto líquido es de +30 pp. Para un edge realista de +5 pp necesitas **783–1,704 observaciones**.

**Y aquí hay que ser brutalmente honesto sobre el plazo, porque contradice al vídeo y también a la intuición.** El presupuesto de turnover de §2.2 (horizonte de horas a días) implica del orden de **150–400 operaciones al año** sumando los tres sleeves nuevos, no las 500–1,600 que asumen los papers. Con un scope que captura el 5–15% de las operaciones:

| Clase de hipótesis | `n_required` | Plazo real a 300 ops/año y scope del 10% |
|---|---|---|
| `DEFECT` (binomial exacto, p₀ ≈ 2%) | 2–3 | **días** |
| `RISK` (proporción, MDE +20 pp, α=0.01) | 81 | **~2.7 años** |
| `EDGE` (t sobre retornos, d=0.10) | 785 | **~26 años** |

**La conclusión incómoda: a la frecuencia que la aritmética de costes impone, una hipótesis `EDGE` no se va a confirmar nunca.** Eso no es un fallo del diseño, es el diseño funcionando: dice la verdad en lugar de fabricar confianza. Las consecuencias prácticas son tres, y hay que aceptarlas antes de construir esto:
1. El motor de hipótesis es, en la práctica, **un detector de defectos y un filtro de riesgo**, no un descubridor de alpha. Ése es su valor real y es considerable.
2. Las hipótesis `EDGE` deben nacer con `effect_kind = 'observe_only'` y quedarse ahí. Se registran para que en tres años haya un histórico pre-registrado, no para cambiar nada este trimestre.
3. Cualquier dashboard que prometa "le faltan 15 casos" para un `EDGE` está mintiendo, incluido el del vídeo.

**Test t sobre retornos** (α=0.05, potencia 0.80, `n = 7.849/d²`) `[C]`:

| `d` (Sharpe por trade) | n | SR anual equivalente a 500 trades/año |
|---|---|---|
| 0.05 | **3,140** | 1.12 |
| 0.10 | **785** | 2.24 |
| 0.20 | 197 | 4.47 |
| 0.30 | 88 | 6.71 |

Una estrategia excelente (`d`=0.05, SR anual 1.12) **requiere 3,140 trades (~6 años) para demostrarse al 5%.**

**Binomial exacto para `DEFECT`** — aquí sí bastan pocos casos, y esta es la justificación *correcta* de "5 casos bastan". Si bajo el nulo la probabilidad es `p₀` y observas `k` de `k` éxitos, el p-valor exacto es `p₀^k` `[C]`:

| `p₀` | k=2 | k=3 | k=4 | k=5 |
|---|---|---|---|---|
| 0.01 | 1.0e-4 | **1.0e-6** | 1.0e-8 | 1.0e-10 |
| 0.02 | 4.0e-4 | **8.0e-6** | 1.6e-7 | 3.2e-9 |
| 0.05 | 2.5e-3 | 1.25e-4 | 6.3e-6 | 3.1e-7 |

Con una tasa base del 2%, **3 casos consecutivos dan p = 8×10⁻⁶** — suficiente incluso con Bonferroni sobre 500 hipótesis. **El sistema debe calcular esto, estimando `p₀` del histórico, en lugar de escribir 20 a mano.**

**Deflación por clúster** `n_eff = n/(1+(m̄−1)ρ_ICC)` `[C]`:

| n observado | m̄ | ρ | **n_efectivo** |
|---|---|---|---|
| 5 | 5 | 0.9 | **1.09** |
| 20 | 5 | 0.9 | 4.35 |
| 38 | 4 | 0.8 | **11.18** |

**Requisito de UI: el dashboard muestra `n_observed` y `n_effective`, y `n_effective` es el que gobierna.** La tarjeta "38 casos" del vídeo probablemente sean ~11 observaciones efectivas.

## 7.4 Inferencia correcta: e-values y siempre-válida

**El problema estructural de un bot: mira el contador continuamente.** Un p-valor clásico con *peeking* no controla nada — el error de Tipo I tiende a 1 con suficientes miradas. La solución es una **martingala de prueba** y la **desigualdad de Ville**.

Para `Xᵢ ∈ [0,1]` y nulo `E[Xᵢ] ≤ p₀`, la martingala de capital por apuestas (Waudby-Smith & Ramdas, *JRSS-B* 86(1):1, 2024) `[V]`:

```
E_t = Π (1 + λᵢ(Xᵢ − p₀)),    λᵢ predecible,  λᵢ ∈ [0, 1/(1−p₀))
```

**Ville:** bajo el nulo, `P(sup_t E_t ≥ 1/α) ≤ α`. ⇒ **Rechaza en cuanto `E_t ≥ 1/α`, mirando cuando quieras, tantas veces como quieras.** Umbrales: α=0.05 → 20; α=0.01 → 100; α=0.001 → 1,000.

Elección de apuesta (aproximación hedged/GRAPA):
```
λ_t = min( 0.9/(1−p₀),  sqrt( 2·ln(1/α) / (σ̂²_{t−1}·t·ln(1+t)) ) )
```

La **confidence sequence** dual `{θ : E_t(θ) < 1/α}` da un intervalo válido en todo momento. **Su cota inferior `cs_lower` es el único número honesto que se puede llamar "confianza" en el dashboard.**

**Multiplicidad — e-BH (Wang & Ramdas, *JRSS-B* 2022, arXiv 2009.02824)** `[V]`, que es el correcto aquí porque **controla FDR bajo dependencia arbitraria** (tus hipótesis están masivamente correlacionadas: mismos símbolos, mismos días, mismas barras). Dadas e-values `E₁…E_m`: ordena descendente y rechaza las `k` mayores donde `k = max{j : E₍ⱼ₎ ≥ m/(j·q)}`. Existe la versión **stopped e-BH** con FDR válido en cualquier momento (arXiv 2502.08539), que es literalmente tu caso de peeking continuo `[V]`.

**Presupuesto de α (alpha investing):** asigna a cada hipótesis nueva una porción de un presupuesto global de riqueza-α; rechazar devuelve α, no rechazar lo consume. Esto hace que **el sistema sea automáticamente más exigente cuando propone muchas hipótesis** — la propiedad que quieres, porque el LLM propone barato.

**Precedente en finanzas para el umbral:** Harvey, Liu & Zhu, "...and the Cross-Section of Expected Returns" (*RFS* 29(1)) `[V]` — tras corregir por los cientos de factores testeados, el umbral de significancia para un factor nuevo sube de **t=2.0 a t≈3.0**, y crece con el tiempo.

## 7.5 Prior escéptico — la diferencia entre aprender y autoengañarse

`θ ~ Beta(a₀,b₀)`; tras `s` éxitos y `f` fallos ponderados, `θ|data ~ Beta(a₀+s, b₀+f)`.

`[C]`, con 5 aciertos de 5:

| Prior | Fuerza | Posterior media | **`P(θ > 0.60 \| datos)`** |
|---|---|---|---|
| `Beta(1,1)` (uniforme — el default que usa casi todo el mundo) | 2 | 0.857 | **0.953** |
| `Beta(10,10)` | 20 | 0.600 | 0.511 |
| **`Beta(50,50)`** | **100** | **0.524** | **0.058** |
| `Beta(100,100)` | 200 | 0.512 | 0.006 |
| `Beta(200,200)` | 400 | 0.506 | 0.0001 |

Con el prior uniforme, 5 de 5 "confirman" al 95%: **ése es exactamente el falso descubrimiento que produce el dashboard del vídeo.** Con un prior escéptico de fuerza 100, la misma evidencia da 0.058 — y ni siquiera cruzaría un umbral del 0.05.

**Un `EDGE` debe nacer con prior centrado en el nulo y fuerte: `a₀+b₀ ≈ 100–200`** (`HYPOTHESIS_EDGE_PRIOR_STRENGTH = 100` en el risk floor). La diferencia entre un sistema que aprende y uno que se autoengaña es el prior.

## 7.6 No estacionariedad — cuatro mecanismos simultáneos

1. **Decaimiento exponencial de evidencia:** `wᵢ = 0.5^(Δtᵢ/τ)`, `τ = half_life_days`. Una hipótesis confirmada con datos de hace 6 meses y `half_life=30d` tiene 1.6% de la evidencia original. Se auto-degrada a `stale` sin que nadie intervenga. Tamaño de muestra efectivo de Kish: `n_eff = (Σwᵢ)²/Σwᵢ²`, luego deflactado por clúster.
2. **TTL duro:** `ttl_days = 90`. Al expirar, **re-confirmación desde cero con nueva ventana out-of-sample.** Sin excepciones. Esto acota el daño de cualquier falso positivo a 90 días.
3. **Confidence sequence sobre ventana rodante:** `can_act` se re-evalúa cada noche. Si `cs_lower < min_effect_to_act`, `can_act := false` inmediatamente.
4. **Detección de drift:** ADWIN (vía `river.drift.ADWIN`) y Page-Hinkley sobre la serie de la métrica. Drift detectado → `stale` + alerta.

## 7.7 Esquema de datos

Postgres. Tablas: `hypothesis`, `hypothesis_observation` (append-only), `llm_call`, `decision`. El DDL completo está en el **anexo B**. Los `CHECK` son **la primera de tres capas** de defensa, no la única — la base de datos no puede validar la semántica de un `effect_params` arbitrario, así que también hay un tipo validado en Python y un test exhaustivo sobre todos los `effect_kind` (§11.1):

```sql
-- Acota el multiplicador de tamaño: nunca puede amplificar.
CONSTRAINT no_size_increase CHECK (
  effect_kind <> 'size_multiplier'
  OR ((effect_params->>'multiplier')::float BETWEEN 0.25 AND 1.00)
),
-- El cooldown solo puede alargarse, nunca acortarse.
CONSTRAINT cooldown_only_extends CHECK (
  effect_kind <> 'extend_cooldown'
  OR ((effect_params->>'minutes')::float > 0)
),
-- El stop solo puede estrecharse: el factor va sobre la distancia al stop.
CONSTRAINT stop_only_tightens CHECK (
  effect_kind <> 'tighten_stop'
  OR ((effect_params->>'distance_factor')::float BETWEEN 0.30 AND 1.00)
),
-- Para actuar: e-BH, out-of-sample, evidencia efectiva suficiente, la puerta
-- de tamaño de efecto, Y el n_required derivado. Las cinco, no una.
CONSTRAINT act_requires_evidence CHECK (
  NOT can_act OR (
        passed_ebh
    AND oos_ok
    AND n_effective >= GREATEST(2.0, n_required * 0.8)
    AND cs_lower > min_effect_to_act
    AND log_evalue >= ln(evalue_threshold)
  )
),
CONSTRAINT oos_only CHECK (registered_at IS NULL OR registered_at >= discovery_cutoff_ts)
```

Nótese el `n_effective >= GREATEST(2.0, n_required * 0.8)`: con el umbral ingenuo de `>= 1` **los cinco casos agrupados del vídeo habrían pasado** (`n_eff = 1.09`), que es precisamente el falso descubrimiento que este diseño existe para impedir.

Y el campo que elimina la mayor parte de los falsos positivos de un sistema como el del vídeo, en el JSON del scope:

```json
"excluded_from_evidence": { "reason": "discovery sample", "before": "2026-08-19T23:59:59Z" }
```

**La muestra de descubrimiento no cuenta como evidencia.** Los casos que *generaron* la hipótesis no pueden ser los que la *confirman*.

## 7.8 La misma tarjeta del vídeo, hecha bien

```
┌─ H-7f3a · DEFECT · scope congelado 2026-08-19T23:59:59Z ─────────────────┐
│ AFIRMACIÓN                                                               │
│   El componente `risk_override` cierra posiciones en <60 s con           │
│   MAE_r = MFE_r = 0 (el precio no se movió), realizando                 │
│   r ≈ −(coste round-trip) ≈ −0.18 R.                                    │
│                                                                          │
│ PREDICCIÓN FALSABLE (pre-registrada, congelada)                          │
│   Métrica: P(hold_s<60 ∧ mae_r=0 ∧ mfe_r=0 | close_reason=risk_override) │
│   H0: p ≤ p0 = 0.021   (tasa base histórica, 180 d, n=4 217)            │
│   H1: p ≥ p1 = 0.80                                                      │
│   Test: binomial exacto + martingala de apuestas (anytime-valid)        │
│                                                                          │
│ EVIDENCIA                                                                │
│   descubrimiento (NO cuenta):  5 casos, 1 clúster, 2026-08-19           │
│   out-of-sample:               4 / 4 éxitos                              │
│   n_observed 4 · clústeres 4 · n_effective 3.7                          │
│   n_required 3   [ n = ceil(ln α / ln p0), α=0.001, p0=0.021 → 2 ]      │
│                                                                          │
│ INFERENCIA                                                               │
│   log10 E_t = 6.91  (E_t = 8.1e6)   umbral 1/α = 100     ✓ CRUZADO      │
│   confidence sequence 99 %: p ∈ [0.47, 1.00]  → cs_lower 0.47           │
│   posterior Beta(1+4, 1+0) → media 0.833                                │
│   e-BH: q asignado 0.010 · 47 hipótesis vivas · PASA (rank 2/47)        │
│                                                                          │
│ min_effect_to_act 0.20   ·   cs_lower 0.47 > 0.20   →  CAN_ACT ✓        │
│                                                                          │
│ EFECTO PERMITIDO                                                         │
│   block_component{component: risk_override, mode: dry_run}              │
│   + open_ticket(severity=P1)                                             │
│   NO se convierte en patrón de mercado. NO entra en cada decisión.      │
│                                                                          │
│ NO ESTACIONARIEDAD                                                       │
│   half_life 30 d · TTL 90 d · ADWIN sin drift · revalidado 2026-09-12   │
│                                                                          │
│ PROCEDENCIA                                                              │
│   propuesta por sonnet-class · llm_call 9c1e… · conf. declarada 0.85 →  │
│   calibrada 0.61 (isotonic, n=612)   [NO usada para decidir]            │
└──────────────────────────────────────────────────────────────────────────┘
```

**Compara:** la tarjeta original decía "le faltan 15 casos". La corregida dice "ya puede actuar con 4, porque bajo el nulo esto ocurre 1 vez entre 8 millones" — y para una hipótesis `EDGE` diría **"le faltan 781 casos, es decir unos 26 años a tu frecuencia de operación"** (§7.3). El sistema honesto es a la vez **más rápido con bugs y mucho más lento con alpha**. Ése es el comportamiento correcto, y es el que hay que implementar.

---

# 8. El dashboard: estilo TALOS con P&L por estrategia

Tu decisión: **nuevo dashboard web (FastAPI + frontend) y jubilar Streamlit.** Correcto — Streamlit no da control visual ni websockets decentes, y el panel de hipótesis necesita layout propio.

He construido un **mockup funcional e interactivo** que acompaña este informe: `dashboard_mockup.html` (autocontenido, ábrelo en el navegador). Reproduce el lenguaje visual del vídeo y añade lo que pediste: **ingreso por cada estrategia**, más las correcciones estadísticas de §7.

## 8.1 Stack

| Capa | Elección | Por qué |
|---|---|---|
| Backend | **FastAPI** + `asyncpg` + `sse-starlette` | Solo-lectura sobre el ledger. **SSE, no WebSocket**: unidireccional, reconexión automática del navegador, atraviesa proxies sin drama. Un bot no necesita full-duplex para pintar un gráfico |
| Frontend | **HTMX + Alpine.js + uPlot** | Cero build step, cero `node_modules`, un fichero por vista. `uPlot` pinta 100k puntos sin sudar y pesa 40 kB. Si prefieres React, úsalo — pero para este alcance HTMX gana en coste de mantenimiento |
| Series temporales | **Grafana embebido** vía iframe para los paneles de exploración profunda | No reimplementes un explorador de series. Grafana ya lo es |
| Auth | **Traefik ForwardAuth + Tailscale** | El dashboard no tiene login propio. Si estás en la tailnet, estás dentro |
| Controles de escritura | Endpoints separados con confirmación + firma | El kill switch y la aprobación de propuestas son los **únicos** POST. Todo lo demás es GET |

## 8.2 Vistas

**1 · Live (la pantalla del vídeo)** — barra superior: modo (`NORMAL` / `SOLO CIERRE` / `PARADO`), equity, P&L del día, drawdown, posiciones abiertas / máximas, consultas IA hoy, calibración, **coste/operación**, coste IA acumulado del mes contra el presupuesto. Gráfico de equity intradía con marcadores de apertura/cierre. Tabla de operaciones cerradas hoy con motivo de cierre.

**2 · P&L por estrategia** ← *lo que pediste y el vídeo no tiene*. Una fila por sleeve (`wheel`, `crypto_rebalance`, `funding_carry`, `xsec_momentum`, `trend`) con:

| Columna | Cómo se calcula |
|---|---|
| P&L bruto | Σ de `realized_pnl` de los fills atribuidos al sleeve |
| Fees pagados | Σ de `commission` |
| Financiación pagada/cobrada | Σ de `funding_payment`, con signo: **positivo = cobrado**, negativo = pagado |
| Slippage estimado | Coste de ejecución adverso, **siempre con el signo de un coste**: compra `(price − decision_price)·qty`, venta `(decision_price − price)·qty`. Un valor negativo es slippage favorable, y hay que poder verlo |
| **P&L neto** | bruto − fees **+ financiación** (con signo: positiva si la cobras) − slippage |
| Coste de IA atribuido | Coste de cada `llm_call` **repartido a partes iguales** entre las `decision` que la usaron (una llamada sirve a varios símbolos y varios *arms*; atribuirla íntegra a cada uno la contaría N veces) |
| **P&L neto tras IA** | ← **la única cifra que importa** |
| Capital asignado / utilizado | del `allocator` |
| **Retorno sobre capital asignado** | neto tras IA / capital asignado |
| Sharpe, Sortino, max DD, Calmar | del `equity_curve` por sleeve |
| Nº ops, win rate, R medio, expectancy | del ledger |
| **Deflated Sharpe + PBO + N configs probadas** | ← lo que hace honesta la cifra de Sharpe |
| Contribución al riesgo de cartera | ajustada por correlación (§4.6) |
| vs. benchmark | retorno del sleeve − retorno de BTC HODL en la misma ventana |

Gráfico de **equity apilada por sleeve** (áreas) y gráfico de **contribución marginal**, más un *waterfall* bruto → fees → financiación → slippage → IA → neto. Ese waterfall es la visualización más útil de todo el dashboard: es donde verás, de un vistazo, si los costes se están comiendo el edge.

**3 · Aprendizaje** — los dos paneles del vídeo, corregidos. Cada tarjeta muestra clase (`DEFECT`/`RISK`/`EDGE`), `n_observed` **y** `n_effective`, `n_required` **con su fórmula**, `log₁₀(E_t)` contra el umbral de Ville, `cs_lower` frente a `min_effect_to_act`, q-valor de e-BH y el ranking entre hipótesis vivas, estado de drift, TTL restante, y el efecto permitido. Filtro por clase y por estado. **La confianza declarada por el LLM se muestra tachada, junto a la calibrada, y con la etiqueta "no usada para decidir".** Es un detalle de UI, pero es el que impide que vuelvas a confiar en ella.

**4 · Propuestas** — cola de aprobación de config diffs: diff lado a lado, backtest del diff, razonamiento, límites duros que restringen el rango propuesto, y botones aprobar/rechazar. El rechazo pide un motivo en texto libre — **esos rechazos son el dataset más valioso que vas a generar** para afinar los prompts.

**5 · Salud** — un panel por venue y por feed: `ws_last_message_age_seconds`, reconexiones, rate limit restante, latencia de órdenes, mismatches de reconciliación, circuit breakers abiertos, offset del reloj, lag del bus, estado del UPS.

**6 · Riesgo** — exposición por símbolo y por sleeve, apalancamiento bruto y neto, distancia a liquidación por posición en múltiplos de σ diaria, matriz de correlación rodante, `N_efectivo` de la cartera, uso del límite de pérdida diaria.

## 8.3 Atribución de P&L: el requisito de datos que lo hace posible

Todo lo anterior depende de una cosa: **cada fill lleva `strategy_id` y `decision_id`, y cada `decision` apunta a la `llm_call` que la informó.** Sin eso, el P&L por estrategia es una estimación. Con eso, es una cuenta.

```sql
-- El ledger. Append-only. Nada se actualiza, nada se borra.
CREATE TABLE fills (
  id              BIGSERIAL PRIMARY KEY,
  ts              timestamptz NOT NULL,
  venue           text NOT NULL,
  symbol          text NOT NULL,
  asset_type      text NOT NULL,          -- stock|option|crypto_spot|crypto_perp
  side            text NOT NULL,
  qty             numeric NOT NULL,
  price           numeric NOT NULL,
  commission      numeric NOT NULL DEFAULT 0,
  funding_payment numeric NOT NULL DEFAULT 0,
  decision_price  numeric,                -- para calcular slippage
  strategy_id     text NOT NULL,          -- ← LA COLUMNA QUE HABILITA TODO
  decision_id     uuid,                   -- FK añadida por ALTER TABLE: `decision`
                                          -- se crea después (ver anexo B)
  client_order_id text NOT NULL,
  venue_order_id  text,
  UNIQUE (venue, venue_order_id, ts, qty, price)
);
```

Y la posición **nunca vive en memoria**: es una vista derivada de `fills`. Si el proceso muere, el estado no se pierde porque nunca estuvo ahí. Ésta es la corrección directa del bug del vídeo (el bot "se volvió loco" porque no recordaba haber abierto una posición de Bitcoin y se bloqueó 2 horas).

---

# 9. Qué cambiar en el repo

## 9.1 El cambio conceptual: `config.json` deja de ser el estado

Hoy `config.json` es a la vez **parámetros**, **estado del sistema** y **límites de riesgo**, y la coordinación entre los dos procesos es el filesystem: `wheel_strategy.py` compara el mtime de `config.json` en cada iteración y recarga, y `app_dashboard.py` usa el mtime de los CSV como clave de caché de Streamlit. Eso funciona para dos procesos y se rompe con doce. Se divide en tres cosas con propiedades distintas:

| Qué | Dónde vive | Quién escribe | Propiedad |
|---|---|---|---|
| **Risk floor** (`max_leverage`, `max_notional_pct`, `daily_loss_stop`, `min_liq_distance_sigmas`, `max_open_positions`) | **`core/risk_floor.py`: constantes de módulo, con `hash` verificado al arrancar** | Nadie en runtime. Solo un commit + despliegue | Inmutable. Un `CHECK` en DB y un test lo garantizan |
| **Parámetros de estrategia** (deltas, DTE, umbrales, pesos) | Postgres, tabla `strategy_params` versionada + `config/*.yaml` en git como default | El dashboard vía aprobación; el LLM solo propone | Versionado, auditable, con rollback |
| **Estado del sistema** (`active`, `kill_switch`, `mode`) | **Triple redundante: fila en `risk_events` ∨ clave Redis ∨ fichero `/run/trading/HALT`** | Dashboard, risk-monitor, o tú por SSH | **Fail-closed**: un fallo al comprobarlo cuenta como activo |

El kill switch triple redundante es deliberado: es el único control que debe funcionar cuando todo lo demás está roto, y por eso no puede depender de que Postgres responda.

## 9.2 Árbol de ficheros objetivo

```
Multi-Asset_Trading_Bot/
├── pyproject.toml                 # uv, grupos por servicio; adiós requirements.txt
├── CLAUDE.md                      # ← §11.1. Lo lee Claude Code en cada sesión
├── README.md · RUNNING.md · ROADMAP.md · SECURITY.md · DECISIONS.md
├── docker/
│   ├── compose.yml  compose.obs.yml  compose.dev.yml
│   ├── traefik/{traefik.yml,dynamic.yml}
│   └── Dockerfile.{engine,api,web,llm}
├── migrations/                    # Alembic. Todo el DDL, nada a mano
├── config/
│   ├── instruments.yaml           # universo, tick/lot size, fees por venue
│   ├── strategies/*.yaml          # defaults versionados
│   └── routing.yaml               # política del router LLM
├── core/
│   ├── risk_floor.py              # ⚠️ CONSTANTES. No leer de YAML ni de DB
│   ├── config/{models.py,store.py,versioning.py}   # ← evoluciona config_models.py
│   ├── ledger/{fills.py,positions.py,equity.py,attribution.py}
│   ├── metrics.py                 # ← rescatado de app_dashboard.py
│   ├── stats/
│   │   ├── evalue.py              # martingala de apuestas + Ville
│   │   ├── ebh.py                 # e-BH bajo dependencia arbitraria
│   │   ├── power.py               # n_required: proporción, t, binomial exacto
│   │   ├── effective_n.py         # deflación por clúster + Kish
│   │   ├── drift.py               # ADWIN, Page-Hinkley
│   │   └── deflated_sharpe.py     # DSR + PBO vía CSCV
│   ├── allocator.py               # presupuesto de capital ajustado por correlación
│   └── bus.py                     # Redis Streams: publish/consume idempotente
├── brokers/
│   ├── base.py                    # SpotBroker (existe) + PerpBroker + OptionsBroker
│   ├── coinbase_spot.py  coinbase_perp.py
│   ├── kraken_spot.py    kraken_perp.py
│   ├── alpaca_equity.py           # lo que ya usa el Wheel
│   └── paper.py                   # ← generaliza PaperSpotBroker
├── data/
│   ├── ingest/{ws_runner.py,gapfill.py,funding.py,oi.py,liquidations.py}
│   ├── sources/{fred.py,dvol.py,coinalyze.py,defillama.py,news.py,status_pages.py}
│   └── features/{numeric.py,regime.py,llm_text.py}
├── strategies/
│   ├── base.py                    # ← ROADMAP 1.1. HACER ESTO PRIMERO
│   ├── wheel/                     # ← wheel_strategy.py, refactorizado
│   ├── crypto_rebalance/          # ← crypto_rebalancer.py
│   ├── funding_carry/             # NUEVO — el sleeve delta-neutral
│   ├── xsec_momentum/             # NUEVO — la mejor candidata direccional
│   ├── trend/                     # NUEVO
│   └── spreads/                   # ROADMAP 1.3, parámetros ya en config
├── execution/
│   ├── router.py                  # ⚠️ ÚNICO servicio con claves de trading
│   ├── idempotency.py             # client_order_id + write-ahead intent
│   ├── reconcile.py               # gate de arranque + bucle cada 5-15 min
│   └── protective.py              # stops reduce-only residentes en el exchange
├── risk/
│   ├── pretrade.py                # ⚠️ LIBRERÍA in-process, NO un servicio HTTP
│   ├── monitor.py                 # drawdown, correlación, kill switch global
│   └── breakers.py
├── llm/
│   ├── router.py                  # local ↔ Anthropic, política de routing.yaml
│   ├── gate.py                    # ROL 3, solo monótono a menos riesgo
│   ├── features.py                # ROL 1, cacheado y versionado
│   ├── hypothesis/{propose.py,engine.py,lifecycle.py}   # ROL 2
│   ├── proposals.py               # ROL 5, cola de aprobación
│   ├── fallback.py                # ladder: cloud → local → determinista
│   ├── calibration.py             # regresión isotónica de la confianza declarada
│   └── audit.py                   # log de prompt/respuesta content-addressed
├── api/                           # FastAPI: routers, SSE, schemas
├── web/                           # HTMX + Alpine + uPlot
├── ops/
│   ├── backup/{pgbackrest.conf,restic.sh,restore_test.sh}
│   ├── observability/{prometheus.yml,alerts.yml,grafana/}
│   └── secrets/{secrets.enc.yaml,decrypt.sh}
├── backtest/{engine.py,report.py,cscv.py}
├── scripts/{reconcile_now.py,flatten_all.py,export_taxes.py}
└── tests/
    ├── guards/                    # ⚠️ los tests que NUNCA pueden fallar
    ├── stats/                     # propiedades: FDR ≤ q, cobertura de la CS
    ├── idempotency/               # reenvío, restart, doble fill
    └── integration/               # contra sandbox/testnet
```

## 9.3 Tabla de decisiones por fichero

| Acción | Ficheros | Nota |
|---|---|---|
| **Mantener casi igual** | `mise.toml`, `LICENSE`, `.gitignore` | |
| **Refactorizar** | `config_models.py` → `core/config/`, `wheel_strategy.py` → `strategies/wheel/`, `crypto_rebalancer.py` → `strategies/crypto_rebalance/` | Los guards se extraen a `strategies/base.py` **antes** de añadir cualquier estrategia nueva. Duplicar guards es cómo se desincronizan |
| **Rescatar y eliminar** | `app_dashboard.py` | `compute_metrics` → `core/metrics.py`; el resto se va |
| **Dividir** | `config.json` | §9.1 |
| **Sustituir** | `requirements.txt` → `pyproject.toml`; `.env.example` → SOPS | |
| **Reescribir** | `ROADMAP.md` | Con los gates cuantitativos de §10 |
| **Crear** | Todo lo demás del árbol | ~28 módulos nuevos |

**Lo que ya está bien y hay que preservar como principio, no solo como código:** el `SpotBroker` Protocol (permite testear sin exchange), la escritura atómica de config, la validación cruzada de Pydantic, y los tres principios del `ROADMAP.md` ("deterministic before intelligent", "propose then approve", "guards are code not configuration"). Esos tres van literalmente al `CLAUDE.md`.

---

# 10. Plan por fases, con gates cuantitativos

Cada fase tiene un **criterio de salida numérico**. Si no se cumple, no se pasa. Esto es lo que le faltaba a tu `ROADMAP.md`: los gates.

## Fase 0 — Instrumentación (2-3 semanas) · **BLOQUEA TODO LO DEMÁS**

Sin esto, cada día de paper trading acumula tiempo sin producir evidencia.

| # | Entregable | Gate de salida |
|---|---|---|
| 0.1 | `migrations/` + ledger en Timescale: `fills`, `positions` (vista), `equity_curve`, `orders`, `risk_events` | `pytest tests/ledger` verde; posición reconstruida desde `fills` == snapshot del exchange |
| 0.2 | `core/metrics.py` + escritor de equity por sleeve | El dashboard antiguo deja de estar vacío con datos reales |
| 0.3 | **Delta real** — `greeks.py`: greeks del broker → Black-Scholes con IV → **si ninguna, se salta el trade y se registra**. Nunca adivinar | Test: sin greeks y sin IV ⇒ 0 órdenes emitidas y 1 log de rechazo |
| 0.4 | `strategies/base.py` con los guards extraídos | Los 10 grupos de `tests/guards/` (CLAUDE.md §1.8) pasan contra la base, no contra el Wheel |
| 0.5 | `tests/guards/` — los 10 grupos de CLAUDE.md §1.8: CSP sin caja, CC sin 100 acciones, límite diario, kill switch, venta cripto > holdings, compra > USDC, config inválida, monotonía del LLM, distancia mínima a liquidación, cap de exposición ajustado por correlación | **Cobertura 100% de las ramas de guard.** Un refactor que rompa un guard debe fallar ruidosamente |
| 0.6 | Logging estructurado: cada rechazo registra **por qué** | Cada `return` temprano de una función de guard emite un evento |
| 0.7 | `backtest/` con **CSCV → PBO** y Deflated Sharpe; registro obligatorio de `N` configs probadas | El informe de backtest **no imprime Sharpe si `N` no está registrado** |
| 0.8 | Grabador WS de **liquidaciones y OI** → Timescale | **Arráncalo el día 1.** Cada día sin grabar es histórico que luego hay que comprar |

## Fase 1 — Homelab e infraestructura (2 semanas, en paralelo con F0)

| # | Entregable | Gate |
|---|---|---|
| 1.1 | `docker/compose.yml` completo (§8 del anexo C), Traefik + Tailscale | El dashboard responde por HTTPS con cert válido desde la tailnet, sin puertos abiertos al exterior |
| 1.2 | SOPS+age → tmpfs → Docker secrets; `gitleaks` en pre-commit | `docker inspect` no revela ningún secreto; `grep -r` en el repo no encuentra claves |
| 1.3 | Observabilidad: Prometheus/VictoriaMetrics + Grafana + Loki + Alertmanager + ntfy | Las 20 métricas de §11.3 del anexo se exponen y hay una alerta por cada una |
| 1.4 | pgBackRest (WAL continuo) + restic → B2; **game-day de restauración** | **RTO medido con cronómetro < 4 h** y documentado en `RUNNING.md` |
| 1.5 | **Dead-man's switch externo** + agente `flatten-only` en un VPS de $5-10/mo | Se mata el host y llega la alerta desde fuera en <5 min |
| 1.6 | UPS + NUT → evento en el bus → `reduce-only` → apagado limpio | Se desconecta la corriente y Postgres cierra sin corrupción |

## Fase 2 — El sleeve nuevo, en testnet (3-4 semanas)

| # | Entregable | Gate |
|---|---|---|
| 2.1 | `brokers/base.py`: `PerpBroker` Protocol + `brokers/paper.py` generalizado | El backtest y el paper corren sin tocar red |
| 2.2 | `coinbase_perp.py` (y `kraken_perp.py` si el testnet de Kraken resulta mejor) contra **sandbox/demo** | **Valida `client_order_id` con una orden de tamaño mínimo real en cada venue**: que se acepte, vuelva en el ack y sea consultable después |
| 2.3 | `execution/idempotency.py` + `reconcile.py` + `protective.py` | Test de caos: matar el router entre el `INSERT` y el `POST` ⇒ 0 órdenes duplicadas, reconciliación correcta al reiniciar |
| 2.4 | `core/allocator.py` con caps ajustados por correlación | La suma del colateral comprometido nunca excede la caja disponible; test con los 5 sleeves compitiendo |
| 2.5 | `strategies/funding_carry/` (delta-neutral, el sleeve con mejor respaldo) | Backtest ≥2 años con PBO < 0.5 y DSR > 0 |
| 2.6 | `strategies/xsec_momentum/` (semanal, 10-16 patas) | Idem, y **turnover medido ≤ 21 RT/año** |
| 2.7 | `strategies/trend/` | Idem |
| 2.8 | API FastAPI + dashboard, **vistas 1, 2, 5 y 6 solamente** (las vistas 3 y 4 dependen de la Fase 3 y se construyen allí) | P&L por estrategia cuadra al centavo contra el ledger |

## Fase 3 — Capa de inteligencia (3-4 semanas)

| # | Entregable | Gate |
|---|---|---|
| 3.1 | `llm/router.py` + `routing.yaml` + **`fallback.py` (ladder: cloud → local → determinista)** | Simular 429 y 529 ⇒ **el bot sigue operando en modo determinista**, nunca se para. Éste es el gate que corrige el fallo del vídeo |
| 3.2 | `llm/features.py` (ROL 1) con caché, TTL y versionado | F1 medido contra un dataset etiquetado a mano de ≥200 noticias; **IC del feature reportado** |
| 3.3 | `core/stats/*` completo | Tests de propiedad: FDR empírico ≤ q sobre 10,000 hipótesis nulas simuladas; cobertura de la confidence sequence ≥ 1−α |
| 3.4 | `llm/hypothesis/` (ROL 2) + **vista 3** del dashboard | Una hipótesis `DEFECT` inyectada se confirma en ≤5 casos; una `EDGE` nula **nunca** se confirma en 10,000 simulaciones |
| 3.5 | `llm/gate.py` (ROL 3) en **shadow mode** | 30 días de decisiones en shadow; se reporta el contrafactual antes de darle autoridad |
| 3.6 | `llm/calibration.py` (isotónica sobre la confianza declarada) | Curva de calibración publicada en el dashboard |
| 3.7 | `llm/proposals.py` (ROL 5) + **vista 4** | Ninguna propuesta puede aplicarse sin aprobación; test que lo intente debe fallar |
| 3.8 | **Gobernador de tokens**: presupuesto mensual como métrica, con degradación *antes* de agotarse | Alerta al 80% del presupuesto; degradación automática al 95% |

## Fase 4 — Paper trading prolongado (60-90 días) · **no negociable**

Antes de la tabla, una advertencia que hay que interiorizar: **este gate es operacional, no estadístico.** Por §2.3 y §7.3, 60-90 días a la frecuencia que impone la aritmética de costes **no pueden** producir significancia estadística sobre el edge; ninguna cantidad de paciencia razonable puede. Lo que este gate verifica es que el sistema **no se rompe, no pierde estado, no se salta un guard y no cuesta más de lo previsto**. La cuestión de si el edge existe queda abierta durante años, y el diseño lo dice en lugar de disimularlo.

| Gate de salida hacia capital real | Umbral |
|---|---|
| Días en paper sin intervención manual | **≥ 60** |
| Posiciones cerradas en los sleeves nuevos | **≥ 40** (lo que el presupuesto de turnover permite en 60-90 d; **no** es una muestra estadísticamente informativa, y no se pretende que lo sea) |
| Mismatches de reconciliación | **0 sin explicación** |
| Reinicios con pérdida de estado | **0** |
| Guards disparados que no se documentaron | **0** |
| Deflated Sharpe del **backtest** de cada sleeve | **> 0**, con `N` registrado (el DSR del paper no será concluyente y no se usa como gate) |
| PBO del backtest | **< 0.5** |
| Drawdown máximo en paper | **< 2× el peor del backtest** |
| Coste fijo mensual real | **< 4% anual del capital previsto** |
| Restauración de backup probada | **≥ 1 game-day exitoso, con RTO cronometrado** |
| Alertas P0 que llegaron al móvil | **100% de las simuladas** |
| Corte de LLM simulado (429/529) | **el bot siguió operando en modo determinista** |
| Retorno vs. BTC HODL en la ventana | **reportado honestamente**, gane o pierda |

## Fase 5 — Capital real, escalonado

| Paso | Capital | Condición para pasar al siguiente |
|---|---|---|
| 5.1 | **$500**, apalancamiento 1×, solo `funding_carry`. **Exento de la regla del 4%** (§4.3): a este tamaño el coste es I+D, no drag | 30 días, 0 incidentes operacionales |
| 5.2 | $2,000, se añade `trend`. Exención de coste todavía vigente | 30 días, DD < gate |
| 5.3 | $10,000, se añade `xsec_momentum`, apalancamiento hasta 1.5× en BTC/ETH. **La exención caduca aquí**: el coste fijo debe caber en el 4% | 60 días, neto > 0 tras todos los costes |
| 5.4 | Escalar según el modelo de §4.3 | — |

**Y el gate transversal:** en cualquier fase, `backtest → paper → live`. Sin excepciones, incluso para cambios que parecen triviales. Es lo que ya dice tu `ROADMAP.md` y hay que mantenerlo.

## Fase 6 — Operación continua

Alertas por Telegram/ntfy (fills, asignaciones, guards, límite diario, crashes, resumen diario), supervisión de procesos, export fiscal para tu CPA con tratamiento §1256 separado, y **una revisión trimestral de hipótesis confirmadas** donde se re-valida todo desde cero.

---

# 11. Cómo ejecutarlo con Claude Code

Mi recomendación: **sí, pásalo a Claude Code.** El trabajo es ~28 módulos nuevos con contratos claros, mucho DDL, mucho test de propiedad y mucha configuración de infraestructura — exactamente donde un agente de código es fuerte. Pero con tres condiciones.

## 11.0 Las tres condiciones

1. **Cierra primero la §13.** Claude Code no puede verificar si Puerto Rico está en la lista de regiones soportadas de Coinbase Derivatives. Tú sí. Cada incógnita que quede abierta se convierte en una suposición enterrada en el código.
2. **Un `CLAUDE.md` con los invariantes, y un `DECISIONS.md` de bitácora.** El `CLAUDE.md` es lo que impide que en la sesión 40 el agente "simplifique" el risk floor a un YAML.
3. **Trabaja por fases con gates, una rama por fase, y los tests de guard corriendo en CI desde el commit 1.** No le des la Fase 3 hasta que la Fase 0 esté verde.

## 11.1 `CLAUDE.md` propuesto

El fichero acompaña este informe (`CLAUDE.md`). Sus reglas duras, resumidas:

- **El risk floor son constantes de Python en `core/risk_floor.py`.** Nunca YAML, nunca DB, nunca mutable en runtime. Si una tarea parece requerir cambiarlo, la tarea está mal planteada: pregunta.
- **Todo efecto del LLM es monótono hacia menos riesgo.** Multiplicador de tamaño en `[0.25, 1.00]`. Cualquier PR que permita a un componente LLM aumentar exposición se rechaza.
- **La posición se deriva de `fills`, jamás se guarda en memoria como fuente de verdad.**
- **Toda orden lleva `client_order_id` con write-ahead intent:** `INSERT ... status='PENDING_NEW'` + COMMIT **antes** del HTTP. En timeout nunca se reenvía a ciegas: se consulta por `client_order_id`.
- **Los stops protectores viven en el exchange, `reduce-only`, siempre.** Un bot que necesita estar vivo para gestionar riesgo es una apuesta a tu uptime.
- **Los checks pre-trade son una librería in-process, no un servicio HTTP.** Un check de riesgo que puede hacer timeout fallará abierto justo cuando importa.
- **`tests/guards/` no puede fallar nunca.** Si un cambio los rompe, el cambio está mal, no el test.
- **Nada de números mágicos estadísticos.** `n_required` se **deriva** y se guarda la fórmula. Prohibido escribir `20`.
- **La muestra de descubrimiento nunca cuenta como evidencia.**
- **Registra `N`.** Cada configuración evaluada, incluidas las abandonadas. Sin `N` no se imprime Sharpe.
- **Presupuesto: el coste fijo mensual no excede el 0.5% anual del capital asignado.** Es una métrica con alerta.
- **Fail-closed en el kill switch.** Un fallo al comprobarlo cuenta como activo.

## 11.2 Prompts de arranque

Acompañan este informe en `PROMPTS_CLAUDE_CODE.md`, uno por fase, cada uno con su gate de salida explícito y la instrucción de parar y preguntar si algo de la §13 sigue abierto.

## 11.3 Cómo trabajar con el agente

- **Una rama por fase**, PRs pequeños, y el gate de la fase como criterio de merge.
- **Empieza cada sesión** con "lee `CLAUDE.md` y `DECISIONS.md`, dime en qué fase estamos y cuál es el siguiente gate". El agente no recuerda la sesión anterior; el repo sí.
- **Pídele que escriba el test antes de la implementación** en todo lo que toque dinero. Para `core/stats/` pídele explícitamente **tests de propiedad** (FDR empírico, cobertura de la CS), no ejemplos.
- **No le des acceso a claves de producción.** Sandbox y testnet durante todo el desarrollo. El `llm-analyst` container se diseña deliberadamente sin secretos de exchange.
- **Cuando proponga una optimización de un guard, di no.** Ése es el fallo que hay que anticipar: un agente optimizador ve un `return` temprano redundante y lo elimina.

---

# 12. Homelab: la parte de infraestructura

Tu decisión: **Docker Compose + Traefik.** Correcto para un operador solo.

## 12.1 Almacenamiento: TimescaleDB caliente + Parquet/DuckDB frío

**El razonamiento es de coste operativo, no de rendimiento.** A ~30-50 GB/año los cinco motores candidatos aguantan. Pero ya necesitas Postgres transaccional (órdenes, fills, posiciones con ACID y claves foráneas), y TimescaleDB te da el time-series **en el mismo motor**: un backup, un runbook, un exporter, y SQL depurable a las 3 de la mañana.

| Alternativa | Por qué no |
|---|---|
| QuestDB | Ingesta más rápida, pero **seguirías necesitando Postgres** → dos sistemas |
| ClickHouse | Mejor compresión, sin UPDATE/DELETE transaccional. Overkill |
| DuckDB+Parquet | 🥈 **Sí, como tier de archivo y backtesting.** Un escritor, sin concurrencia |
| InfluxDB 3 Core | ❌ **Límite de ventana de consulta de 72h en el OSS** `[?]` |

⚠️ Todos los benchmarks de ingesta publicados son autopublicados por vendedores compitiendo entre sí `[?]`.

**Decisión de diseño clave: no almacenes deltas L2 crudos** (serían TB). Deriva cada segundo `bid/ask/mid/spread_bps/depth_10bps/depth_50bps/imbalance`: tres órdenes de magnitud menos, ~95% del valor de features. Si más tarde necesitas microestructura real, compras el periodo concreto a Tardis.dev.

**Footprint estimado a 1 año, 16 símbolos** `[C]` con supuestos explícitos:

| Dataset | Crudo | Comprimido |
|---|---|---|
| Velas 1m (16 sym × 3 venues) | 2.5 GB | 150-250 MB |
| Trades (sup. 300k/día/sym-venue `[?]`) | ~300 GB | **15-30 GB** |
| Funding (8h) + OI (snapshots 1m) | 2 GB | ~200 MB |
| Liquidaciones (tu propio tape) | 10 GB | ~1 GB |
| Top-of-book agregado a 1s | 120 GB | 8-15 GB |
| Ledger (equity, orders, fills) | <100 MB | <100 MB |
| **Total** | | **~25-50 GB/año** |

El DDL con hipertablas, compresión, políticas de retención y agregados continuos está en el **anexo B**.

## 12.2 Bus de mensajes: Redis Streams

Ya quieres Redis para caché, locks y buckets de rate limit; los consumer groups, `XAUTOCLAIM` y `MAXLEN ~` cubren el caso. **Un daemon menos.** NATS JetStream sería la segunda opción si prevés multi-host. RabbitMQ es sobredimensionado. Postgres `LISTEN/NOTIFY` solo como *doorbell* ("la config cambió"), porque la notificación se pierde si no hay listener.

**Regla: no pongas ticks a tasa completa en el bus.** El ingester escribe crudo a Timescale y publica solo eventos decimados (barras de 1s cerradas, señales, órdenes, fills). **El bus transporta eventos; Postgres es la verdad.**

## 12.3 Lista de contenedores

En el **anexo C** está el `compose.yml` comentado. Los servicios y el razonamiento de por qué están separados:

`traefik` · `postgres` (timescaledb-ha) · `redis` · `md-coinbase` · `md-kraken` · `md-derivs` · `md-altdata` · `feature-builder` · `strategy-*` (uno por familia) · **`order-router` (`replicas: 1`, el ÚNICO con claves de trading)** · `risk-monitor` · `reconciler` · **`llm-analyst` (CERO claves de exchange, con `cpus`/`mem_limit`/`pids_limit`)** · `api` · `web` · `scheduler` (ofelia) · `victoriametrics` o `prometheus` · `grafana` · `loki` · `alloy` · `alertmanager` · `ntfy` · exporters · **`nut-upsd`** · `pgbackrest` · `restic-backup` · `restore-test`.

**Redes:** `proxy` (solo Traefik y lo expuesto), `data` (DB, Redis, ingesters, estrategias), `obs`. **El `order-router` está en `data` y no en `proxy`.**

**Fuera de la isla** ($5-10/mes, no en este host): dead-man's switch + agente `flatten-only` con una clave reduce-only. Dado dónde vives, esto no es paranoia.

## 12.4 Los 10 patrones de fiabilidad

1. **`client_order_id` idempotente** — `INSERT orders(status='PENDING_NEW')` + COMMIT **antes** de la llamada HTTP. En timeout o 5xx, **nunca reenviar a ciegas**: consultar por el id. ⚠️ El soporte en CCXT no es uniforme entre exchanges `[?]` → **valida con una orden de tamaño mínimo en cada venue antes de operar con tamaño**.
2. **Reconciliación de arranque como gate** — verifica reloj → consulta órdenes/posiciones/balance/trades → diff contra Postgres → **el exchange siempre gana** en posición; si el delta supera el umbral, **modo seguro y ack humano** antes de operar. Repetir cada 5-15 min.
3. **Stops `reduce-only` residentes en el exchange, siempre** — el ítem de mayor valor de toda esta sección. Sobreviven a apagones de LUMA, a NVMe muertos y a tus propios bugs.
4. **Watchdog de WS + gap-fill** — no confíes en `on_close`: si `now − last_message_ts > 3×` el intervalo esperado, fuerza reconexión. **Un socket TCP vivo que no entrega datos es el fallo más peligroso.** Backoff exponencial **con jitter ±30%** (sin jitter los ingesters se sincronizan y te ganas un ban). Detecta huecos **por número de secuencia**, no por timestamp.
5. **Reloj: chrony, no timesyncd** — `makestep 1.0 3`, `rtcsync`, dos pools. Sincroniza **solo en el host**. Exporta el offset y alerta a **>50 ms**. Los rechazos por timestamp son tu canario.
6. **Circuit breakers** — error-rate por venue (>20%/60 s), datos rancios, pérdida diaria, N pérdidas consecutivas, **tasa de órdenes** (protege contra bucles de bug), rechazos, spread/ATR extremo, mismatch de reconciliación. **Kill switch fail-closed y triple redundante.**
7. **Degradación elegante** — cada estrategia **declara** sus feeds y su TTL. Feed CRITICAL caído → sin trading nuevo, **mantener y alertar, no liquidar a mercado en un venue roto**. IMPORTANT → último valor con decaimiento y tamaño reducido. OPTIONAL → feature `NULL`. **Pruébalo matando contenedores.**
8. **Recuperación de posición tras crash** — `fills` append-only + vista derivada. *Exactly-once* contra un exchange es imposible; busca at-least-once + claves de idempotencia = "effectively-once".
9. **Ventanas de mantenimiento** — poléa los JSON de las status pages cada 60 s; trata las ráfagas de 5xx como estado "venue degradado", no como errores a reintentar. **No canceles tus stops porque la API parpadee.**
10. **Dead-man's switch externo** — las alertas sobre tu host no pueden depender de tu host. `curl` cada 60 s a un monitor externo; si el ping cesa, **ellos** te avisan.

## 12.5 Secretos, red, backups, hardware

**Secretos — la capa que más importa es la 0:** claves **trade-only** con **retiros DESHABILITADOS** en el exchange, **IP allowlist**, una clave por servicio (ingesters read-only; solo `order-router` tiene la de trading; `llm-analyst` ninguna), 2FA y whitelist de direcciones. Encima de eso, **SOPS + age**: `secrets.enc.yaml` commiteable con diffs por clave, descifrado por un systemd oneshot a **tmpfs** (nunca a disco), montado como Docker secret. Clave age respaldada **offline, en papel**. `gitleaks` en pre-commit — el mejor ROI de esta sección. **No** uses Vault ni Infisical self-hosted: se convierten en dependencia de arranque de tu bot, y el unseal tras cada reboot es el foot-gun que un día impedirá arrancar a las 4 a.m.

**Red:** Traefik solo en `:443` con **DNS-01** (Cloudflare, token con scope `Zone:DNS:Edit` de una sola zona), wildcard `*.lab.tudominio`, **split-horizon DNS** (zona pública solo con los TXT de `_acme-challenge`, cero registros A; tu resolver interno mapea a la IP privada) → certs públicamente válidos para nombres que solo resuelven dentro. ⚠️ `resolvers: ["1.1.1.1:53"]` explícito es obligatorio, o la auto-comprobación falla. **Empieza con el `caServer` de staging.** Acceso remoto: **Tailscale** — cero puertos abiertos, ACLs por tag, y el plano de datos es P2P y sigue funcionando si su coordinación cae. **No Cloudflare Tunnel:** CF termina el TLS y puede ver tu P&L y tus posiciones. **Y ten una vía break-glass que no sea el proxy (Tailscale SSH): el día que necesites entrar, lo roto será Traefik.**

**Backups:**

| Tier | Qué | RPO | RTO |
|---|---|---|---|
| T0 | `secrets.enc.yaml`, **clave age offline**, compose+traefik+`acme.json`, código, dump del ledger | **0** (WAL archiving: nunca pierdas un fill) | ≤15 min |
| T1 | DB de research, modelos, feature store | 24 h | 72 h |
| **T2 irreemplazable** | **tu tape de liquidaciones, snapshots de OI, archivo de noticias** — nadie te los vende retroactivamente | 24 h | 72 h |
| T3 no respaldar | velas y trades re-descargables | ∞ | — |
| Pérdida total del host | — | — | **2-4 h con runbook escrito** |

**pgBackRest** (full semanal + incremental diario + **WAL continuo** → PITR al segundo) + **restic** → Backblaze B2 (a 50-200 GB pagas céntimos `[?]`). ⚠️ **Foot-gun de TimescaleDB:** los chunks comprimidos van bien con backup físico, pero con `pg_dump`/`pg_restore` **debes** usar `timescaledb_pre_restore()`/`post_restore()` o la restauración queda corrupta. **Game-day mensual automatizado** que restaura a un compose desechable, reconstruye la posición desde `fills`, la compara con un snapshot guardado, arranca en dry-run, y **cronometra: ese número ES tu RTO**.

Y la observación que hay que tener presente: **tu evento de DR más probable no es un disco muerto, eres tú desplegando un bug.** Por eso el primer paso del runbook es *"para el bot y gestiona la posición a mano desde la web del exchange"*, no *"restaura el backup"*.

**Hardware y energía:**

| Perfil | Especificación | Idle | Típico |
|---|---|---|---|
| Mínimo viable (sin LLM local) | 4 cores / 16 GB / 500 GB NVMe (mini-PC o OptiPlex Micro usado) | 8-20 W | 15-25 W |
| **Cómodo (LLM 7-14B)** | 8-16 cores / **64 GB** / 2 TB NVMe (**caché DRAM, TBW ≥600** — escribirás sin parar) + GPU 12-16 GB | 35-50 W | **60-90 W** |
| LLM 32B | + RTX 3090 24 GB usada | +15-25 W | carga 280-350 W |

**Coste eléctrico** — `kWh/mes = W × 0.7296` `[C]`. ⚠️ **No pude verificar la tarifa actual de PR** (`lumapr.com` y `eia.gov` bloqueados; hay revisión tarifaria activa). Memoria: **$0.22-0.28/kWh** `[?]`, entre las más altas de EE.UU. tras Hawái.

| Consumo | kWh/mes | @$0.25 | **Anual @$0.25** |
|---|---|---|---|
| 15 W | 10.9 | $2.74 | **$33** |
| **90 W (cómodo)** | **65.7** | **$16.42** | **$197** |
| 300 W (3090 sostenida) | 218.9 | $54.72 | **$657** |

**Conclusión sobre el LLM local:** una 3090 24/7 cuesta $200-650/año de luz **más** ~$800 de capital para hacer lo que una API alojada hace por céntimos al día. En una cuenta de $10k eso es 2-6% anual de drag **solo en electricidad**. → **Corre un 7-14B en CPU/iGPU** para el trabajo siempre activo (etiquetado, dedup, clasificación, fallback) y llama a la API para el razonamiento puntual. GPU dedicada solo si privacidad u offline son requisitos duros.

**UPS obligatorio, no opcional:** 1500 VA / ~900 W (~$150-250) → a 60-90 W te da 20-60 min. **NUT** en contenedor: al pasar a batería publica un evento → estrategias a `reduce-only` → apagado limpio de Postgres. **Y por eso los stops del lado del exchange no son negociables aquí: el UPS te compra 40 minutos, un apagón de LUMA puede durar días.**

## 12.6 Stack de datos

**Por debajo de $50k de capital: $0/mes. Sin excepciones.** La aritmética `[C]`:

| $/mes | $1k | $10k | $25k | $100k |
|---|---|---|---|---|
| $29 | 34.8% | 3.5% | 1.4% | 0.3% |
| $100 | 120% | 12.0% | 4.8% | 1.2% |
| $200 | 240% | 24.0% | 9.6% | 2.4% |

**Stack gratuito:** CCXT + `ccxt.pro` (ejecución y WS) · Alpaca (barras + **API de noticias gratis con cuenta**, fuente Benzinga, histórico ~2015 `[?]`) · **funding history nativo del exchange — no pagues por esto, es gratis y completo** · **Coinalyze** (OI, long/short, liquidaciones; API gratis con key) `[?]` verifica su profundidad · **tu propio grabador WS de liquidaciones** · **Deribit DVOL gratis sin API key** + cadena de opciones → construyes tu superficie IV y el skew 25d · Alchemy/QuickNode free RPC · DefiLlama + Dexscreener + GeckoTerminal · RSS + CryptoPanic · **tu LLM local puntúa el sentimiento** (esto reemplaza un servicio de pago) · **FRED** (`DGS2`, `DGS10`, `T10Y2Y`, `DTWEXBGS` — ojo, "DXY" es de ICE y no está en FRED, `CPIAUCSL`, `VIXCLS`, `WALCL`, `RRPONTSYD`, `BAMLH0A0HYM2`) · **FRED `fred/releases/dates` como calendario económico oficial** (truco poco conocido: reemplaza un calendario de pago) · status pages JSON de los exchanges → alimentan tu circuit breaker.

**Único añadido que defiendo sin reservas por encima de $10k:** **CoinGlass Startup (~$29/mo `[?]`)** si las liquidaciones y el OI histórico son entrada real de tu estrategia. Todo lo demás espera.

**Valor predictivo documentado vs decoración:**

| Nivel | Fuentes |
|---|---|
| ✅ **Mejor respaldo académico** | **Funding rate / basis / carry de perpetuos** (BIS WP 1087 "Crypto carry"; Mathematics 14(2):346) `[V]` existencia, cifras no verificadas `[?]`. **Y su coste de datos es $0. Empieza aquí** |
| ✅ Sólido por microestructura | OI + cascadas de liquidación; spread `DVOL − RV30` |
| 🟡 **Horizonte multi-mes, y probablemente roto** | **On-chain.** Kuczara (SSRN 6280358): *"Post-ETF Structural Break in Bitcoin On-Chain Signal Predictiveness: 47 metrics"* → quiebre estructural tras enero 2024 (el flujo marginal se movió dentro de custodios de ETF) `[V]` título. **Lee este paper antes de gastar un dólar en on-chain** |
| 🟡 Condicional | Noticias/eventos: valor en *event-driven*, no en sentimiento agregado |
| 🔴 **Decoración a tu escala** | Sentimiento social, métricas on-chain de altcoins, TVL como señal direccional, Fear&Greed |
| 🔴 Contexto, no señal | Macro (FRED): filtro de régimen, no timing |
| ❌ Descartar | **X/Twitter API** (~$200/mo, migró a créditos `[?]`) — sobre $10k son 24% anual de drag. NewsAPI free tier (delay 24h). Kaiko/Amberdata/Databento (enterprise) |

---

# 13. Checklist de verificación — ciérralo antes de escribir código

Ordenado por consecuencia si resulta falso.

| # | Qué verificar | Dónde | Si falla |
|---|---|---|---|
| **1** | **Que Puerto Rico esté en la lista de regiones soportadas** de Coinbase Derivatives/CFM, Kraken Derivatives US, Alpaca y Binance.US | Páginas de "supported regions" de cada uno | **Todo el §3 cambia.** Es la incógnita de mayor consecuencia |
| **2** | **Ley 38-2026: fecha exacta de vigencia, cutover 0%→4%, grandfathering** | Abogado fiscal de PR, **este mes** | Una fuente dice que el 0% expira el 31 de diciembre. Si aplica a ti, la ventana es de semanas |
| **3** | Carácter fiscal de la renta del bot (negocio vs capital gain) y tratamiento §1256 bajo §933 | Mismo abogado/CPA | Puede cambiar la estructura entera del proyecto |
| 4 | Especificaciones de los perpetuos de Kraken US: tamaño de contrato, fees, apalancamiento, intervalo de funding, y si el **demo environment** cubre los perps de EE.UU. | `support.kraken.com` | Kraken era la mejor opción de testnet; si no cubre perps US, se usa el sandbox de Coinbase |
| 5 | Cap actual de apalancamiento de los perps de Coinbase y si el **Advanced Trade Sandbox cubre derivados de EE.UU.** | `docs.cdp.coinbase.com` | Sin sandbox de derivados, la Fase 2 necesita otro plan de validación |
| 6 | **Si existe endpoint gratuito de histórico de funding** para los perps US de Coinbase | Idem | El sleeve de carry depende de esto; si no, se usa Hyperliquid como fuente de datos (leer no es operar) |
| 7 | Si los **perps de Alpaca están disponibles a minorista de EE.UU.** | `docs.alpaca.markets/us/` | Sería la migración más barata de todas |
| 8 | Estado del **soporte de `clientOrderId` en CCXT** por exchange, y validación con una orden mínima real | Prueba empírica, no docs | Es el prerrequisito de todo el patrón de idempotencia |
| 9 | Profundidad histórica real del free tier de **Coinalyze** | Prueba empírica contra tus 16 símbolos | Es el pilar del stack de datos gratuito |
| 10 | Alcance del free tier de **noticias de Alpaca** | Prueba empírica | Es el pilar de la capa de texto gratuita |
| 11 | Precios actuales de la **API de Anthropic** y multiplicadores de prompt caching | `platform.claude.com/docs/en/about-claude/pricing` | Todas las cifras de §4.4 son `[V-título]`, no leídas |
| 12 | **Tarifa $/kWh actual de PR** | `lumapr.com/current-rates...` | Cambia el cálculo de §12.5 y la decisión sobre GPU local |
| 13 | Precio actual de **CoinGlass Startup** (el único servicio de pago que recomiendo por encima de $10k) y de CoinGecko y DefiLlama Pro — encontré contradicciones entre fuentes (CoinGecko $29 vs $129; DefiLlama Pro $49 vs $300) | Páginas de pricing de cada uno | Solo afecta al stack de pago, que de todos modos no necesitas al principio |
| 14 | Retornos de **BTC 2025 y 2026 YTD** | `slickcharts.com/currency/BTC/returns` | Es tu benchmark. Sin esto no puedes juzgar nada |
| 15 | **Resultado de CME v. CFTC** y estado del CLARITY Act | Trackers legales | Un fallo adverso podría forzar reestructurar los perps US de Coinbase y Kraken |
| 16 | Lee **arXiv 2609.04917** (límites de la inversión automatizada, sep-2026) y **2605.16895** (The Alpha Illusion) | arXiv | Son los dos papers que más deberían moderar las expectativas del §5 |
| 17 | Lee **Kuczara SSRN 6280358** (quiebre estructural post-ETF en señales on-chain) | SSRN | Determina si merece la pena construir la capa on-chain |
| 18 | Rutas exactas de los **JSON de status page** de Coinbase y Kraken | Prueba con `curl` | Alimentan el circuit breaker del patrón #9 |

---

# 14. Riesgos, límites y qué NO hacer

**Lo que este informe no puede prometer.** No hay ninguna distribución auditada y pública de rendimiento de bots retail — 3Commas, Cryptohopper y Pionex publican marketing, no datos `[?]`. Los mejores sustitutos cuantitativos son regulatorios (**ESMA: 74-89% de las cuentas retail de CFD pierden dinero** `[K]`) y on-chain (la distribución de PnL de traders de Hyperliquid es **directamente computable** vía su API; si quieres una cifra honesta, calcúlala tú). Todos los rangos de "Sharpe bruto realista" de §4.1 son estimaciones de consenso práctico, no medidas publicadas. El PBO típico de bots retail no ha sido medido por nadie. La eficacia del volatility targeting está disputada (Moreira-Muir vs. Cederburg et al.) `[V/?]`.

**Las cinco cosas que NO hacer**, cada una de las cuales aparece en el vídeo o es el fallo por defecto:

1. **No operes en menos de 4 horas de horizonte con órdenes market.** Es aritméticamente inviable (§2.2).
2. **No dejes que el LLM genere señales directamente.** La literatura crítica de 2026 es clara y la aritmética de coste es implacable (§5).
3. **No apalanques por encima de 1.5×**, punto. Y por debajo de eso, dimensiona con ½-Kelly sobre el Sharpe del *backtest deflactado*, no sobre el estimado en vivo, porque con el número de operaciones que vas a acumular el Sharpe en vivo no será medible durante años (§4.5, §7.3).
4. **No confirmes hipótesis sobre la muestra que las generó**, ni con `n_required` hardcodeado, ni sin control de FDR (§7).
5. **No financies datos ni infraestructura con un edge que aún no has demostrado.** El break-even de §4.3 es una restricción, no una sugerencia.

**Y el riesgo que más probablemente te afecte:** no es un cisne negro del mercado, es **fatiga de aprobación**. El diseño tiene una cola de propuestas que requiere tu revisión. A las seis semanas, la tentación de auto-aprobar será fuerte, y ese es el momento exacto en que el sistema deja de tener un humano en el bucle sin que nadie haya decidido quitarlo. Mitigación de diseño: **límite duro de propuestas por semana**, y que una propuesta no revisada **expire** en lugar de acumularse.

---

# Anexos

- **Anexo A** — `model.py`: el script con todos los cálculos `[C]` de este informe. Reproducible con `python3 model.py`.
- **Anexo B** — `schema.sql`: DDL completo (ledger, hipótesis, hipertablas de Timescale con compresión y agregados continuos).
- **Anexo C** — `compose.yml` comentado y `risk_floor.py`.
- **`CLAUDE.md`** — contrato de invariantes para Claude Code.
- **`PROMPTS_CLAUDE_CODE.md`** — prompt de arranque por fase.
- **`dashboard_mockup.html`** — mockup interactivo del dashboard, con P&L por estrategia y el panel de hipótesis corregido.

# Fuentes principales

**Regulación y venues:** [Coinbase — US perpetual-style futures](https://www.coinbase.com/blog/coming-july-21-us-perpetual-style-futures) · [Coinbase — mecanismo de funding](https://help.coinbase.com/en/derivatives/perpetual-style-futures/funding-rate) · [Coinbase FCM](https://www.coinbase.com/fcm) · [docs CDP — futures](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/futures) · [Kraken — perps US CFTC](https://blog.kraken.com/product/kraken-derivatives/announcing-cftc-regulated-us-perps) · [Kraken — margen spot CFTC](https://blog.kraken.com/product/margin/cftc-regulated-spot) · [Kraken — demo environment](https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives) · [Kraken — CSVs OHLCVT históricos gratis](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data) · [CFTC PR 9241-26](https://www.cftc.gov/PressRoom/PressReleases/9241-26) · [Federal Register 2026-11020](https://www.federalregister.gov/documents/2026/06/03/2026-11020/policy-statement-concerning-the-listing-of-perpetual-contracts) · [CFTC — actual delivery PR 8139-20](https://www.cftc.gov/PressRoom/PressReleases/8139-20) · [Katten — perpetual futures come onshore](https://katten.com/perpetual-futures-come-onshore-the-cftcs-new-regulatory-framework) · [Lowenstein — CME sues CFTC](https://www.lowenstein.com/news-insights/publications/client-alerts/fctm-breaking-news-cme-sues-cftc-over-approval-of-bitcoin-perpetual-futures-contract-fctm) · [IBKR + Coinbase Derivatives](https://www.interactivebrokers.com/en/trading/coinbase-derivatives.php) · [Hyperliquid ToS](https://app.hyperliquid.xyz/terms) · [Alpaca — crypto fees](https://docs.alpaca.markets/us/docs/crypto-fees)

**Fiscal PR:** [Procopio — Act 38-2026](https://www.procopio.com/resource/puerto-rico-extends-act-60-resident-investor-program) · [MZLS — el 0% expira el 31 de diciembre](https://www.mzls.com/insights/act-60-puerto-rico-resident-investor-program-extended-to-2055-what-prospective-applicants-need-to-know) · [Holland & Knight — escrutinio del IRS (jun-2026)](https://www.hklaw.com/en/insights/publications/2026/06/act-60-tax-compliance-irs-scrutiny) · [Bloomberg Tax — ganancias pre-mudanza](https://news.bloombergtax.com/tax-management-memo/puerto-ricos-act-60-when-are-pre-move-bitcoin-gains-taxed)

**Estadística y sobreajuste:** [Bailey & López de Prado — Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) · [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) · [Pseudo-Mathematics and Financial Charlatanism](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659) · [pypbo](https://github.com/esvhd/pypbo) · [Harvey, Liu & Zhu — …and the Cross-Section of Expected Returns](https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.PDF) · [Waudby-Smith & Ramdas — Estimating means by betting (JRSS-B)](https://academic.oup.com/jrsssb/article/86/1/1/7043257) · [Wang & Ramdas — e-BH (arXiv 2009.02824)](https://arxiv.org/abs/2009.02824) · [stopped e-BH (arXiv 2502.08539)](https://arxiv.org/html/2502.08539v3) · [POPPER — Automated Hypothesis Validation (ICML 2025)](https://arxiv.org/abs/2502.09858) · [código POPPER](https://github.com/snap-stanford/POPPER)

**Estrategias y costes:** [Liu, Tsyvinski & Wu — Common Risk Factors in Cryptocurrency (JF 2022)](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13119) · [A Trend Factor for the Cross Section of Cryptocurrency Returns (JFQA)](https://www.cambridge.org/core/services/aop-cambridge-core/content/view/4C1509ACBA33D5DCAF0AC24379148178/S0022109024000747a.pdf/trend_factor_for_the_cross_section_of_cryptocurrency_returns.pdf) · [Fundamentals of Perpetual Futures (arXiv 2212.06888)](https://arxiv.org/abs/2212.06888) · [Wish or reality? Triangular arbitrage exploitability](https://www.sciencedirect.com/science/article/pii/S154461232401537X) · [The Market Maker's Dilemma (arXiv 2502.18625)](https://arxiv.org/html/2502.18625v2) · [Where does the criticality live? — cascadas heterogéneas (arXiv 2607.27070)](https://arxiv.org/abs/2607.27070) · [CoinDesk — el evento de liquidación de $19B](https://www.coindesk.com/research/market-spotlight-the-19-billion-liquidation-that-shook-crypto) · [Bouchaud — square-root law of market impact](https://bouchaud.substack.com/p/the-square-root-law-of-market-impact) · [Moreira & Muir — Volatility Managed Portfolios](https://www.nber.org/papers/w22208)

**LLM en trading:** [Lopez-Lira & Tang — Can ChatGPT Forecast Stock Price Movements? (arXiv 2304.07619)](https://arxiv.org/abs/2304.07619) · [The Alpha Illusion (arXiv 2605.16895)](https://arxiv.org/abs/2605.16895) · [AI in Equity and Crypto Markets: Limits of Automated Investing (arXiv 2609.04917)](https://arxiv.org/abs/2609.04917) · [From Hypotheses to Factors: Constrained LLM Agents in Crypto (arXiv 2604.26747)](https://arxiv.org/abs/2604.26747) · [Kinlay — Your Research Agent Is an Undisclosed Factor Exposure](https://jonathankinlay.com/2026/08/your-research-agent-is-an-undisclosed-factor-exposure-and-so-is-everyone-elses/) · [Anthropic — prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) · [Anthropic — batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)

**Benchmark:** [Slickcharts — retornos anuales de BTC](https://www.slickcharts.com/currency/BTC/returns) · [CFTC — Retail Traders in Futures Markets](https://www.cftc.gov/sites/default/files/2024-11/Retail_Traders_Futures_V2_new_ada.pdf)

---

*Documento técnico. No es asesoría financiera, legal ni fiscal. Los tres puntos fiscales de §3.4 requieren un profesional licenciado en Puerto Rico, y el primero tiene una fecha límite posiblemente cercana.*
