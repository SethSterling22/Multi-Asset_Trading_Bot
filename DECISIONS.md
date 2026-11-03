# DECISIONS.md — bitácora de decisiones

Append-only. Una entrada por decisión: fecha, contexto, alternativas y motivo.
Las entradas no se editan; una decisión que cambia se revoca con una entrada nueva
que enlaza a la anterior. El plan de referencia es
[`docs/INFORME_TALOS.md`](docs/INFORME_TALOS.md).

---

## 2026-10-31 · D-001 · Decisiones de arquitectura de partida

Cuatro decisiones tomadas antes de la Sesión 0. Su análisis completo está en el
informe; aquí queda el resumen y la sección donde se justifica cada una.

### D-001.1 · Venue: Coinbase como primario, Kraken como alternativa

- **Contexto:** Puerto Rico es jurisdicción de EE.UU.; los venues offshore del vídeo
  (Binance, Bybit, Hyperliquid) están cerrados. Desde jul-2025 (Coinbase) y jun-2026
  (Kraken) hay perpetuos regulados por la CFTC accesibles a minoristas. (§3.1, §3.2)
- **Alternativas:** Kraken Pro + Kraken Derivatives US (mejor testnet); IBKR (máxima
  claridad fiscal, peor API); Alpaca (elegibilidad de perps sin verificar); offshore
  vía VPN (descartado, §3.3).
- **Motivo:** spot y perps regulados detrás de una sola API y SDK
  (`coinbase-advanced-py`, ya en las dependencias), y el `SpotBroker` Protocol existente
  solo necesita su hermano `PerpBroker`. Kraken queda como alternativa porque la
  demanda CME↔CFTC es un riesgo estructural: el adaptador de perps debe ser
  intercambiable desde el primer día.
- **Condiciones abiertas:** §13 puntos 1, 4, 5, 6 y 8 (regiones soportadas, sandbox de
  derivados, histórico de funding, `client_order_id`). La Sesión 5 no empieza sin
  ellos.

### D-001.2 · LLM: router híbrido local + Anthropic

- **Contexto:** el bot del vídeo se paró dos veces por quedarse sin cuota. Una rejilla
  de 16 símbolos × 96/día con un modelo Sonnet-class cuesta ~$1,198/mes. (§4.4, §5)
- **Alternativas:** solo API; solo local (GPU dedicada: $200–650/año de luz más
  hardware).
- **Motivo:** API Haiku-class event-gated para el gate (~$15/mes) y Sonnet-class en
  batch nocturno para el motor de hipótesis; un modelo local 7–14B en CPU/iGPU como
  último escalón del fallback ladder (cloud → local → determinista) y en shadow mode
  permanente. El local nunca devuelve 429/529, y el determinista garantiza que el bot
  sigue operando sin LLM.

### D-001.3 · Dashboard: FastAPI + HTMX, se jubila Streamlit

- **Contexto:** `app_dashboard.py` lee `logs/*.csv` que nada escribe, y Streamlit no da
  control de layout ni streaming decente para el panel de hipótesis. (§8)
- **Alternativas:** mantener Streamlit; FastAPI + React.
- **Motivo:** FastAPI de solo lectura sobre el ledger con SSE, y HTMX + Alpine + uPlot
  sin build step. Los únicos POST son el kill switch y la aprobación de propuestas.
  `compute_metrics` se rescata a `core/metrics.py`; el resto de `app_dashboard.py` se
  elimina en la Sesión 7.

### D-001.4 · Despliegue: Docker Compose + Traefik en el homelab

- **Contexto:** operador único, homelab en Puerto Rico con red eléctrica poco fiable
  (LUMA). (§12)
- **Alternativas:** Kubernetes/k3s (sobredimensionado para una persona); Cloudflare
  Tunnel para el acceso (descartado: termina el TLS y vería P&L y posiciones).
- **Motivo:** Compose es operable por una persona a las 3 a.m. Traefik con DNS-01 y
  split-horizon, acceso por Tailscale sin puertos abiertos, `order-router` con
  `replicas: 1` como único servicio con claves de trading, UPS + NUT, stops
  `reduce-only` residentes en el exchange y dead-man's switch fuera de la isla.

---

## 2026-10-31 · D-002 · Andamiaje de la Sesión 0

- **Contexto:** Sesión 0 de `docs/PROMPTS_CLAUDE_CODE.md`: estructura, tooling y CI, sin
  código de producción.
- **Decisiones:**
  - Dependencias en `[dependency-groups]` de uv, uno por servicio (`engine`, `api`,
    `llm`, `dev`). `streamlit` y `plotly` viven en un grupo `legacy-dashboard`
    aparte hasta la Sesión 7, porque `app_dashboard.py` sigue en el repo y no se
    toca todavía. `requirements.txt` se conserva hasta que `RUNNING.md` deje de
    referenciarlo.
  - La URL de Alembic sale de `DATABASE_URL`, nunca de `alembic.ini`, para que no
    acabe una credencial en git.
  - `ruff` cubre todo el repo salvo `docs/` (material de referencia literal) y los
    cuatro módulos heredados de la raíz, que se refactorizan en las sesiones 1, 2 y
    7. Cuando se muevan a su sitio en el árbol, quedan cubiertos.
  - El Action de CI instala solo el grupo `dev`: los guards de hoy solo dependen de
    la stdlib. Cuando un guard importe `pydantic` u otra dependencia de servicio, se
    añade su grupo al workflow.
- **Pendiente:** cablear `assert_no_mutation()` en el arranque de cada servicio. No
  hay servicios todavía; se hace al crear el primero (`strategies/base.py`, Sesión 2).

---

## 2026-11-01 · D-003 · Se elimina `requirements.txt`

- **Contexto:** D-002 conservaba `requirements.txt` hasta que `RUNNING.md` dejase de
  usarlo. `README.md` y `RUNNING.md` ya instalan con `uv sync`.
- **Alternativas:** mantenerlo generado con `uv export`, para quien instale con pip.
- **Motivo:** dos listas de dependencias se desincronizan. Todas sus dependencias
  están en `pyproject.toml` y fijadas en `uv.lock`; el informe (§9.2, §9.3) ya preveía
  sustituirlo.

---

## 2026-11-01 · D-004 · Ledger (Sesión 1): desviaciones del schema de referencia

- **Contexto:** migración `0001` con las secciones 0–2 de `docs/schema_reference.sql`
  y `core/ledger/`. Al implementarlas aparecieron cinco problemas del schema de
  referencia que harían mentir al ledger.
- **Decisiones:**
  1. **El slippage se restaba dos veces.** La vista `pnl_by_strategy_day` define
     `gross_pnl = Σ realized_pnl` y `net = gross − fees + funding − slippage`. Si
     `realized_pnl` se calcula con los precios de ejecución, el coste de ejecución
     ya está dentro y restarlo otra vez subestima el neto (en el fixture de los
     tests: 6.29 en lugar de 8.29). Se mantiene la fórmula y los signos de
     CLAUDE.md §3.4, y se define `gross_pnl` = P&L realizado a precios de fill +
     slippage, es decir, a precios de decisión. Así `net_pnl` = realizado − fees +
     funding, exacto, y el slippage sigue visible en el waterfall.
  2. **`fills.realized_pnl` no se almacena.** Depende del orden de los fills, y un
     gap-fill que inserte un fill antiguo dejaría mal todos los posteriores en una
     tabla que no admite UPDATE. Se deriva por replay con coste medio
     (`core/ledger/positions.py`), igual que la posición (§1.3). Con 150–400
     operaciones al año el replay es trivial.
  3. **`fills.venue_fill_id` es NOT NULL.** Con NULL, la UNIQUE de idempotencia no
     aplica y la doble inserción duplica. Funding y paper llevan un id determinista.
  4. **`fills.multiplier`.** Sin él, el coste de una opción (×100) o de un perp con
     tamaño de contrato (BIP = 0.01 BTC) sale mal en la vista `positions`.
  5. **CHECKs y append-only por trigger.** `side` restringido a los seis valores
     (un 'BUY' contaba como venta en la vista), `qty >= 0`, `price >= 0`,
     `commission >= 0`. UPDATE, DELETE y TRUNCATE sobre `fills` y `risk_events`
     lanzan excepción.
- **Otras decisiones de la sesión:**
  - `sqlalchemy`, `psycopg` y `structlog` pasan a `[project].dependencies`: los usa
    `core/`, que importan todos los servicios y CI.
  - Los tests `db` fallan sin `TEST_DATABASE_URL` en vez de saltarse: un gate que
    pasa porque no ejecutó nada no es un gate.
  - `docker/compose.dev.yml`: misma imagen que producción, sin contraseña (trust) y
    escuchando solo en 127.0.0.1.
  - La vista SQL de atribución (sección 6) no se crea todavía: necesita `decision` y
    `llm_call` (sección 3), y el P&L realizado es path-dependent, así que la única
    implementación es Python. El reparto del coste de IA ya está en
    `core/ledger/attribution.py`; la última decisión se lleva el resto de la división
    para que la suma sea exacta.
  - `core/metrics.py` recibe la anualización explícita (`periods_per_year`) y
    devuelve `None`, no 0.0, para las métricas que no se pueden calcular.

---

## 2026-11-01 · D-005 · Gate de la Sesión 1 (Fase 0.1–0.2): verde

- **Entorno:** PostgreSQL 17.11 + TimescaleDB 2.31.0 (`docker/compose.dev.yml`,
  imagen `timescale/timescaledb-ha:pg17`).
- **Resultado:**
  - `pytest -m "not integration"`: 76 passed, incluidos los 12 tests `db` (migración
    de ida y vuelta, idempotencia, append-only, CHECKs, vista `positions` == replay).
  - `ruff check .` y `mypy core risk` limpios.
  - `scripts/synthetic_fills.py` con `--database-url`: 200 fills en 5 sleeves y 50
    snapshots diarios por sleeve; las cuatro comprobaciones contables pasan, lo
    releído de Postgres es idéntico y la vista coincide con el replay. La
    transacción se deshace: la base queda sin fills sintéticos.
  - La migración crea las 6 hipertablas, la compresión y la retención del schema de
    referencia (sin retención en `liquidations`) y los dos agregados continuos con
    su política de refresco.
- **Verificación a mano:** los resultados de los fixtures (`tests/ledger/fixtures.py`,
  `test_attribution.py`) están calculados a mano en sus comentarios. En la salida del
  script se comprobaron a mano el slippage, la comisión y el signo del funding de los
  primeros fills de `trend`; el resto lo cubren las identidades contables, que no
  reutilizan el método de coste medio.
- **Pendiente de la Fase 0.1:** "posición reconstruida desde `fills` == snapshot del
  exchange" necesita un broker real o su sandbox; aquí se cumple contra snapshots
  conocidos. Se cierra con la reconciliación (Sesión 5).
- **Siguiente:** Sesión 2 (Fase 0.3–0.6): delta real, `strategies/base.py`, los 10
  grupos de `tests/guards/` y `risk/breakers.py`.

---

## 2026-11-01 · D-006 · Sesión 2: delta real, guards comunes, breakers y kill switch

- **Contexto:** Fase 0.3–0.6. Primera sesión que toca el Wheel y el rebalanceador.
- **Decisiones:**
  1. **El Wheel detrás de un puerto.** `strategies/wheel/logic.py` contiene el ciclo
     y habla con el broker por el Protocol `OptionsBroker` (el patrón de
     `SpotBroker`); `strategy.py` es solo el adaptador de Lumibot. Así los guards se
     prueban sin broker. **El adaptador no tiene tests contra Lumibot real.**
  2. **Delta:** broker → Black-Scholes con IV del precio de mercado → se salta con
     `reason='no_greeks'`. La banda de sanidad no es un número a mano: es el rango
     de deltas que Black-Scholes admite para esa moneyness con σ entre 1% y 500%,
     más 0.02 por dividendos y ejercicio americano. Sin dividendos (q=0). Un precio
     igual al valor intrínseco no da IV (no informa de la volatilidad).
  3. **Colateral de una CSP:** `max_collateral_pct_per_trade = 0.20` (nuevo campo,
     con tope en `MAX_NOTIONAL_PCT_EQUITY`). Sustituye al `max(limit * 10, limit)`
     del código viejo, que con el 0.02 de `config.json` daba el mismo 20% con un
     ×10 escondido. **Mismo riesgo que antes**, ahora explícito. El floor permitiría
     25%; no se afloja sin aprobación.
  4. **`daily_loss_limit_percentage` acotado por `DAILY_LOSS_STOP_PCT`** en el
     modelo: la config solo puede ser más estricta que el floor.
     `max_portfolio_risk_per_trade` queda como está (0.02) y **no lo aplica** ninguna
     de las dos estrategias: el Wheel no tiene stop y lo acota el colateral; el
     floor (1%) lo aplica `risk/pretrade.py` a los sleeves con stop.
  5. **Pérdida diaria y semanal persistidas:** la referencia es el primer snapshot
     de `equity_curve` del día (America/New_York para el Wheel) o de la semana. Sin
     snapshot, **no se opera**. El Wheel escribe el snapshot antes del gate en
     cada iteración.
  6. **Kill switch:** las tres fuentes son argumentos obligatorios; `config.json`
     es una cuarta fuente que solo puede activar. Un error al comprobar cualquiera
     cuenta como activo, también una config ilegible.
  7. **Breakers como funciones puras sobre la historia persistida** (órdenes,
     fills, equity), no contadores en memoria: un reinicio no los resetea. Los
     umbrales sin número en el floor ni en el informe (rechazos seguidos, spread
     frente a ATR) se pasan explícitamente al construir cada política.
  8. **Espacio de acción del LLM** (`risk/monotone.py`): `Intent`, `SizeMultiplier`
     con factory validada y `apply_effect` para los 8 `effect_kind`. Es la segunda
     de las tres capas de CLAUDE.md §1.2; los CHECK de `hypothesis` llegan con su
     migración (Sesión 8–9).
  9. **El Wheel exige `DATABASE_URL` y `REDIS_URL` para arrancar.** Sin ledger no
     hay registro, y sin las tres fuentes no hay kill switch. `docker/compose.dev.yml`
     añade Redis en 127.0.0.1:6380.
- **Bugs encontrados por los tests nuevos** (corregidos):
  - Una whitelist o `monitored_assets` de solo espacios (`["  "]`) pasaba la
    validación: `min_length` se comprueba antes de limpiar. En el rebalanceador era
    una división por cero.
  - `implied_vol` devolvía σ = 1% para un precio 0 o igual al intrínseco.
- **El comando del gate del prompt no mide `strategies/base.py`:** `--cov` recibe
  un nombre de módulo, y con `--cov=strategies/base.py` coverage avisa "never
  imported" y lo omite. El gate se corre con `--cov=strategies.base`.
- **Lumibot 4.6.32**, el que fija `uv.lock` (el `.venv` anterior tenía 4.5.83). Al
  importarlo lee `.env` y abre un socket del broker: importar el adaptador del Wheel
  tiene efectos aunque no se arranque la estrategia.

---

## 2026-11-01 · D-007 · Gate de la Sesión 2 (Fase 0.3–0.6): verde

- `pytest -m guards --cov=risk --cov=strategies.base --cov-branch
  --cov-fail-under=100`: 222 passed, **100% de líneas y ramas** en `risk/` y
  `strategies/base.py`.
- Sin greeks y sin IV ⇒ 0 órdenes y 1 log de rechazo con `reason='no_greeks'`
  (`test_no_greeks_and_no_iv_means_zero_orders_and_one_rejection`).
- Los 10 grupos de CLAUDE.md §1.8 pasan contra la base: CSP sin caja (incluido el
  colateral agregado y pendiente), CC sin 100 acciones, pérdida diaria, kill
  switch, venta > holdings, compra > USDC (en conjunto), permutaciones inválidas de
  config, monotonía del LLM, distancia a liquidación, cap ajustado por correlación.
- **"Cada test debe fallar si comento la línea del guard":** comprobado con
  mutantes. Se quita cada `return Rejected(...)`, `raise EffectRejectedError(...)`,
  `return NoDelta(...)` y `self.reject(...)` de base, pretrade, breakers, monotone,
  greeks y la lógica del Wheel, uno a uno: **55 de 56 mutantes hacen fallar algún
  test.** El superviviente es la red final de `apply_effect` para un `effect_kind`
  sin rama, inalcanzable mientras los 8 tengan rama; si se añade un noveno, falla
  `test_vocabulary_mirrors_the_schema_enum`.
- `pytest -m "not integration"` (con Postgres): 286 passed. `ruff` y `mypy` (strict
  en `core`, `risk`, `strategies/base.py` y `strategies/wheel/logic.py`) limpios.
- **Siguiente:** Sesión 3 (Fase 0.7–0.8): backtest con DSR, PBO y N registrado, y
  el grabador de liquidaciones y OI. El grabador depende del punto 9 de la §13
  (Coinalyze), sin verificar.

---

## 2026-11-01 · D-008 · Sesión 3, parte 1: backtest con DSR, PBO y N registrado

- **Contexto:** Fase 0.7. El grabador de liquidaciones y OI (Fase 0.8, la otra mitad
  de la sesión) queda para cuando haya API key de Coinalyze (§13 punto 9).
- **Decisiones:**
  1. **El backtest ejecuta el código de producción.** `backtest/engine.py` corre el
     `CryptoRebalancer` real (con sus guards de caja y holdings) contra un broker
     simulado con comisión y slippage. Para eso la base acepta `StaticParams` además
     de `config.json`. No se simulan kill switch ni pérdida diaria: miden el estado
     del sistema, no la decisión.
  2. **Sin look-ahead:** se decide al cierre de t y se ejecuta a la apertura de t+1.
  3. **Comisión y slippage no tienen valor por defecto.** La ejecución del gate usa
     60 bps de comisión y 5 bps de slippage por lado: **supuesto sin verificar** sobre
     Coinbase Advanced (la §13 no cubre las comisiones spot). El USDC en caja rinde 0.
  4. **N en `search_log`** (migración `0002`): append-only por trigger, porque borrar
     filas desinflaría N, y `UNIQUE (kind, strategy_id, params_hash)`. El hash incluye
     la ventana y los supuestos de coste: re-ejecutar la misma configuración no cuenta
     dos veces; cambiar cualquier cosa, sí. El CLI exige la base de datos: un
     registro en memoria olvidaría las pruebas de la sesión anterior.
  5. **El informe no imprime Sharpe** (ni Sortino, que tiene el mismo problema).
     Imprime DSR, PBO y N, y falla si el candidato no está registrado. El candidato es
     el mejor in-sample del barrido: exactamente la elección que el DSR deflacta.
  6. **DSR en `core/stats/deflated_sharpe.py`, en Python puro** (sin numpy en
     `core/`). Su umbral reproduce la tabla del informe §2.4. PBO por CSCV en
     `backtest/cscv.py`, también en Python puro, con 16 bloques (12,870 combinaciones).
  7. **Dos benchmarks:** BTC buy-and-hold (lo pide el informe, gane o pierda) y los
     mismos pesos comprados el primer día sin rebalancear, que es el que aísla lo que
     aporta el rebalanceo.
- **Ambigüedad del informe:** el gate de la Fase 2 dice "DSR > 0", pero el DSR de
  Bailey y López de Prado es una probabilidad en [0, 1], siempre > 0. El informe
  imprime la probabilidad; > 0.5 equivale a superar el máximo esperado por azar, y
  > 0.95 es significativo. **Hay que decidir cuál de los dos es el gate** antes de la
  Fase 2.
- **Fallo de lógica del rebalanceador encontrado por el barrido** (no corregido, es
  una decisión de estrategia): partiendo de 100% USDC, si el umbral es mayor que el
  peso objetivo por activo, la desviación nunca lo alcanza y **no compra nunca**. Con
  umbral 0.20 y 30% cripto (15% por activo) el backtest sale plano y se descarta. Con
  el `config.json` actual (umbral 0.05, 20% por activo) no ocurre.
- **Resultado** (`docs/backtests/crypto_rebalance_2023-11-01_2026-10-31.md`): 3 años
  de velas diarias reales de Coinbase (BTC y ETH, 1,096 días sin huecos), 20
  configuraciones (1 descartada). **DSR 0.832, PBO 0.490, N 20.** El mejor (umbral
  0.10, 60% USDC) hizo +42.4% neto frente a +117.1% de BTC HODL y +27.6% de los
  mismos pesos sin rebalancear, con drawdown −29.7% frente a −53.1% y −39.2%.
  **No es evidencia de edge:** DSR < 0.95 y un PBO al nivel del azar.
- **Gate de la Sesión 3, parte del backtest:** verde ("backtest de crypto_rebalance
  sobre ≥2 años de datos reales produce un informe con DSR, PBO y N"). El resto del
  gate (grabador ≥48 h sin huecos, prueba de caos del ingester) sigue pendiente.

---

## 2026-11-02 · D-009 · Coinalyze verificado (§13 punto 9) y archivador REST en lugar de WebSocket

- **Contexto:** Fase 0.8, la segunda mitad de la Sesión 3. El informe pide un grabador
  WebSocket de liquidaciones y OI. Antes de construirlo se verificó Coinalyze con una
  key real (2026-11-02).
- **Hechos verificados:**
  - Gratis con key; 40 llamadas/min, y **cada símbolo de una petición cuenta como una
    llamada**. La key va en la cabecera `api_key`.
  - 28 exchanges y ~5,500 perpetuos. **No cubre los perps de EE.UU. de Coinbase**
    (no hay ningún `BIP-…-CDE`): su "Coinbase" (`BTC-PERP` en USDC) es Coinbase
    International, con solo 182 días de datos y el último del 2026-10-02. Kraken aparece
    como Kraken Futures (`pf_xbtusd`); sin confirmar si es el libro de los perps US.
  - **Diario completo:** BTCUSDT de Binance desde 2020-01-21 (OI, funding y
    liquidaciones). **Pero con `convert_to_usd` el diario se corta en ~1,500 puntos**
    (desde 2022-09): el diario de OI y liquidaciones se pide en unidad nativa.
  - **Intradía de ~1,500–2,000 puntos**, medido: 1 min ≈ 1.1 días, 15 min ≈ 21 días,
    1 h ≈ 83 días, 12 h ≈ 1,000 días.
  - **Las liquidaciones son dispersas:** un intervalo sin liquidaciones no aparece.
  - Kraken Futures tiene minutos sin dato en la propia fuente (9–17 al día en OI).
  - **Sin verificar:** los términos de uso (la web no enlaza ninguno).
- **Decisión: archivador REST (`data/ingest/coinalyze.py`), no WebSocket.**
  - Con 25 h de retención a 1 minuto, una ejecución cada pocas horas no pierde nada, sin
    sockets que vigilar y sin depender de que un exchange offshore acepte una IP de
    EE.UU. 64 series (BTC y ETH en Binance, Bybit, OKX, Hyperliquid y Kraken Futures;
    OI y liquidaciones a 1 min, funding y long/short a 1 h, y el diario de las tres) =
    64 llamadas por ejecución, ~75 s.
  - Datos crudos en `derivatives_metrics` (migración `0003`), no en `funding` ni
    `liquidations`, que esperan eventos individuales y no barras. Tier T2: sin
    retention policy.
  - `ingest_runs` (append-only) registra la ventana que cubrió cada ejecución. Un hueco
    es tiempo que ninguna cubrió; en series densas, también una barra que falta dentro
    de lo cubierto. Las liquidaciones solo pueden tener lo primero.
  - El informe separa **sin cubrir** (perdido), **retraso** desde la última ejecución
    (> 12 h = el archivador está parado; el margen con 25 h de retención se acaba) y
    **ausentes en la fuente** (informativo).
  - El WebSocket runner de la Sesión 3 queda para los datos de mercado del propio venue
    (Fase 2), donde sí hace falta.
- **Consecuencia para `funding_carry`:** Coinalyze es contexto de mercado, no el funding
  que cobraría el sleeve. El punto 6 de la §13 (histórico de funding de los perps US de
  Coinbase) sigue abierto y sigue bloqueando ese sleeve.
- **Gate de la Sesión 3, segunda mitad:** el código y el test de caos (una ejecución que
  muere a mitad se rellena con la siguiente) están en verde. **Falta lo que solo da el
  tiempo:** ≥ 48 h de ejecuciones con `report --hours 48` en 0 problemas. Primera
  ejecución real: 88,486 filas, 0 problemas en 20 h.

---

## 2026-11-02 · D-010 · LLM: Anthropic + OpenAI, sin modelo local (revoca D-001.2)

- **Contexto:** D-001.2 ponía un modelo local 7–14B como último escalón del fallback,
  porque nunca devuelve 429/529. El usuario no va a correr un LLM local (Sadida tiene
  una RTX 3050 Mobile de 4 GB) y tiene acceso a dos proveedores: **Anthropic** (API de
  Claude Enterprise; el usuario confirma que tiene permiso para usarla en este
  proyecto) y **OpenAI** (el modelo que el usuario llama "Astra"; el identificador
  exacto se verifica contra la API al configurar la capa LLM, no se da por supuesto).
- **Alternativas:** mantener el local en CPU (31 GB de RAM lo permitirían);
  un solo proveedor.
- **Decisión:**
  1. Escalera de fallback **Anthropic → OpenAI → determinista**. La red de seguridad es
     el modo determinista, no un modelo: el gate de la Fase 3.1 (ante 429/529 el bot
     sigue operando) no cambia. Dos proveedores añaden diversidad de modelo, que mitiga
     el riesgo de estrategias correlacionadas por usar todos el mismo LLM (Kinlay).
  2. **Por rol, el modelo más barato que pase su gate medido.** Un modelo más potente
     solo entra donde gane al pequeño por un margen que pague su coste: F1 e IC en el
     rol 1 (Fase 3.2), contrafactual del shadow mode en el rol 3 (Fase 3.5). Donde más
     rinde es el motor nocturno de hipótesis (rol 2, batch) y construir el sistema.
  3. **El LLM sigue fuera del bucle de decisión** (CLAUDE.md §1.2): ningún modelo, por
     potente que sea, genera señales ni tamaños. Más capacidad de proponer hipótesis
     es más N, no más evidencia.
  4. Los identificadores de modelo y la política de routing van en
     `config/routing.yaml`, no en el código. Las claves de Anthropic y OpenAI solo las
     recibe el `llm-analyst`, que no tiene claves de exchange.
  5. **El coste de IA cuenta en el P&L aunque lo pague otra cuenta** (CLAUDE.md §3.4).
     Sin el local, el presupuesto del 4% (§1.12) se aplica a la factura de API.

---

## 2026-11-02 · D-011 · Sadida como host de ejecución (adelanto parcial de la Fase 1)

- **Contexto:** el archivador de Coinalyze necesita ejecutarse al menos cada 12 h, el
  Wheel en paper tiene que estar vivo en horario de mercado y la Fase 4 pide 60–90 días
  sin intervención. Un ordenador de escritorio no da eso.
- **Sadida:** Proxmox VE 9.2 sobre un portátil Acer Nitro AN517-55: i5-12500H (12
  núcleos / 16 hilos), 31 GB de RAM, 110 GB de raíz y 910 GB en `/mnt/data`, 38 días de
  uptime. Supera el mínimo del informe (§12.5: 4 cores, 16 GB, 500 GB).
- **Decisiones:**
  1. **Una VM dedicada**, no el host de Proxmox ni un contenedor LXC (Docker en LXC
     tiene problemas de nesting, overlayfs y AppArmor). Postgres y Redis en Docker
     dentro de la VM (`docker/compose.runtime.yml`); el archivador y el Wheel como
     unidades systemd (`ops/systemd/`).
  2. **La batería del portátil hace de UPS.** Un vigilante en el host
     (`ops/power/battery-watch.sh`) activa el kill switch de la VM tras 2 minutos en
     batería (sin trading nuevo) y la apaga limpio por debajo de un umbral. Sustituye a
     NUT en la Fase 1.6. **No levanta el kill switch solo** al volver la corriente: lo
     levanta una persona (propose, then approve).
  3. **Riesgo de la batería:** un litio al 100% día y noche y con calor se degrada y
     puede hincharse. Hay que limitar la carga (BIOS o
     `charge_control_end_threshold`) o revisarla físicamente.
  4. **Se empieza limpio en Sadida, sin migrar la base.** El ledger está vacío, el
     archivo de Coinalyze tiene horas y se vuelve a descargar, y re-ejecutar el backtest
     vuelve a registrar las mismas 20 configuraciones con los mismos hashes (N se
     conserva).
  5. Solo van al `.env` de Sadida las claves de **paper** de Alpaca y la de Coinalyze.
     Las de Coinbase CDP no las usa nada todavía.
- **Lo que sigue pendiente de la Fase 1** y bloquea el dinero real, no el paper: SOPS +
  age para los secretos, backups fuera del host (pgBackRest + restic) con un game-day de
  restauración, Traefik + Tailscale sin puertos abiertos y el dead-man's switch externo.
- **Sin verificar todavía:** el tipo de disco de `/mnt/data` (SSD o mecánico) y qué más
  corre en Sadida.

---

## 2026-11-02 · D-012 · Sadida: contenedores en el Docker del host, no una VM (revisa D-011.1)

- **Contexto:** D-011 proponía una VM dedicada. El usuario prefiere contenedores: Sadida
  ya tiene Docker en el host de Proxmox, y no quiere virtualizar otro kernel.
- **Alternativas:** VM (D-011); contenedor LXC con Docker dentro (problemas de nesting,
  overlayfs y AppArmor).
- **Decisión:** todo en `docker/compose.runtime.yml` sobre el Docker del host: Postgres,
  Redis, `migrate` (una vez), `archiver` (bucle cada 6 h) y `wheel` (paper). Imagen
  `docker/Dockerfile.engine`; `.dockerignore` deja fuera `.env` y cualquier secreto.
  - Sustituye a las unidades systemd de D-011: las políticas de reinicio de compose
    cubren lo mismo (`on-failure:5` para el Wheel; el archivador ejecuta al arrancar,
    que equivale al `Persistent=true` del timer).
  - Postgres y Redis solo en la red interna del compose; Postgres además en
    `127.0.0.1:5432` del host para administrar con psql.
  - El kill switch por fichero es `/run/trading/HALT` **del host**, montado en el Wheel
    en solo lectura.
  - El vigilante de batería actúa directamente: toca ese fichero y, con poca batería,
    hace `docker compose stop` (SIGTERM al Wheel, 120 s para Postgres). Ya no hace falta
    el agente de QEMU.
- **Lo que se acepta al no usar VM:**
  - Proxmox recomienda no correr servicios en el host; un daemon de Docker con root en
    el hipervisor amplía el alcance de un contenedor comprometido a todo Sadida.
    Aceptable para paper y datos públicos; **se revisa antes de la Fase 5**, cuando haya
    claves de trading reales.
  - Docker cambia las reglas de iptables (política FORWARD a DROP), lo que puede cortar
    la red de las VM y LXC sobre `vmbr0`. Ya funciona así en Sadida; queda anotado por si
    aparece al añadir otra VM.
- **Rendimiento:** la diferencia real con una VM es memoria reservada (no se apartan
  ~12 GB), no CPU: la carga es mínima (el archivador trabaja ~75 s cada 6 h).

---

## 2026-11-02 · D-013 · Dashboard FastAPI + HTMX adelantado (Fase 2.8 / Sesión 7), solo lectura

- **Contexto:** el usuario quiere ver gráficamente lo que pasa en Sadida y prefiere ir
  directamente al dashboard propio, basado en `docs/dashboard_mockup.html`, en vez de un
  Grafana intermedio. Es un **adelanto de fase** sobre CLAUDE.md §4 (el gate de la Fase 0
  aún no está cerrado: falta el archivador 48 h y el snapshot del exchange de la 0.1).
- **Por qué se acepta:** el dashboard es de **solo lectura** (no hay ningún POST, y la
  conexión a Postgres lleva `default_transaction_read_only=on` impuesto por el
  servidor). No cambia el riesgo de nada: el orden de fases protege contra construir
  sobre algo sin verificar, y aquí lo que se construye solo mira.
- **Decisiones:**
  1. Vistas 1 (Live), 2 (P&L por estrategia, con el waterfall), 5 (Salud) y 6 (Riesgo)
     de la §8.2, más **Mercado** (el archivo de Coinalyze, el único dato vivo hoy). Las
     vistas 3 y 4 dependen de la Fase 3 y no se construyen.
  2. **Gráficos en SVG generado en el servidor**, con el CSS del mockup copiado tal cual.
     Ni uPlot ni build step: el navegador solo carga HTMX (CDN, versión fijada) para
     refrescar cabecera y vista cada 30 s.
  3. **Polling de HTMX en lugar de SSE** (la §8.1 dice SSE). Para refrescar cada 30 s
     es más simple y sobrevive a reiniciar el backend sin lógica de reconexión; el gate
     de la Sesión 7 sobre SSE se reformula como "la vista se recupera tras reiniciar el
     contenedor", que con polling es automático.
  4. **Todo cálculo reutiliza el código probado:** el P&L sale de
     `core/ledger/attribution.py`, la cobertura del archivador y el kill switch de
     `risk/breakers.py`. Sin Sharpe (D-008).
  5. Contenedor propio (`docker/Dockerfile.api`) **sin las claves de brokers ni de
     Coinalyze**. Escucha en `127.0.0.1` del host por defecto; se publica en la tailnet
     con `tailscale serve` o poniendo la IP de Tailscale en `DASHBOARD_BIND`.
  6. Sin login propio: el acceso lo da la tailnet (§8.1, "Traefik ForwardAuth +
     Tailscale"; aquí, de momento, solo Tailscale).
- **Gate de la Sesión 7 que sí aplica:** el P&L por estrategia cuadra al centavo contra el
  ledger (`tests/api/test_dashboard_db.py::test_pnl_matches_the_ledger_to_the_cent`) y
  ninguna cifra de retorno mostrada es bruta.
- Con esto, `config.json` del host se monta en el Wheel y en el dashboard: editarlo en
  Sadida surte efecto sin reconstruir la imagen.

---

## 2026-11-02 · D-014 · Sleeve cripto en Alpaca paper por el router; §13 legal diferido

- **Contexto:** el usuario quiere avanzar con el cripto sin esperar a la parte de
  credenciales y permisos legales, porque todo es paper y datos de prueba. Ya tiene la
  API de Alpaca; la de Coinbase falta. Sadida tiene battery backup y es nodo central
  del clúster, así que el vigilante de batería deja de ser necesario.
- **Decisiones:**
  1. **Primer venue cripto: Alpaca paper**, detrás del port `SpotVenue`
     (`brokers/base.py`). Coinbase Advanced entra después con el mismo port. El
     adaptador **se niega a construirse con `paper=False`**: dinero real es la Fase 5.
  2. **Capa de ejecución (`execution/`)**, que es la Sesión 5 adelantada para este
     sleeve: kill switch → pre-trade in-process → tasa de órdenes, horizonte mínimo y
     turnover → INSERT `PENDING_NEW` + COMMIT → POST. Con timeout o 5xx se consulta por
     `client_order_id` y **nunca se reenvía**; si tampoco se puede consultar, la orden
     queda `UNKNOWN` y la reconciliación decide.
  3. **Reintentos de alpaca-py desactivados** (reintenta 429/504 también en el POST de
     una orden, que es reenviar a ciegas) y timeout de 10 s en cada petición. Se hace
     sobre atributos privados de la versión fijada; un test falla si cambian.
  4. **Reconciliación antes de cada ciclo**: órdenes abiertas y posiciones del ledger
     contra el venue. Un mismatch activa el kill switch de `crypto_rebalance`
     (`risk_events`, actor `reconciler`); nada se arregla solo. Venue sin responder:
     el ciclo no opera, pero no activa el kill switch.
  5. **`MIN_HOLD_SECONDS` y `MAX_ROUNDTRIPS_PER_YEAR` se aplican ya en el router**
     (estaban en el floor y no los usaba nadie). Turnover = fills/2 en 365 días; un
     sleeve sin presupuesto en el floor no opera.
  6. **Presupuesto propio del sleeve** (`CRYPTO_BUDGET_USD`): su caja es el presupuesto
     más el flujo de caja de sus fills, y las compras se topan además con la caja real
     de la cuenta, compartida con la Wheel en paper. Recomendado: una segunda cuenta
     paper. Las tenencias salen del ledger, no del venue (§1.3).
  7. **Comisión de los fills cripto = 0, sin verificar.** Alpaca paper no deja claro si
     cobra el fee cripto en paper. Si lo cobra en especie, la cantidad del venue no
     cuadrará con la del ledger y la reconciliación lo dirá en el primer fill. Se
     corrige entonces con el dato observado, no con una suposición.
  8. **Dry-run por defecto.** Sin `CRYPTO_DRY_RUN=false` el proceso planifica y
     registra, no envía. El servicio de compose va en el perfil `crypto`: no arranca
     con un `up -d` normal.
  9. **§13 del informe** (elegibilidad de Puerto Rico en cada venue, fiscalidad,
     permisos) **se difiere a la Fase 5**, antes del primer dólar real. No afecta a
     paper. Pasos 3–4 de RUNNING §4b (batería) pasan a opcionales.
- **Límites conocidos:** los checks pre-trade ven solo este sleeve (la exposición
  agregada entre sleeves llega con el segundo sleeve operando). El fill se registra
  agregado por orden (`venue_fill_id` = id de la orden), no por ejecución parcial.

---

## 2026-11-03 · D-015 · Alpaca cobra la comisión cripto en especie; motivo y precios persistidos

- **Contexto:** las dos primeras órdenes de paper (2026-11-02 06:38 UTC) se llenaron y la
  reconciliación siguiente paró `crypto_rebalance`: el ledger tenía 0.025617702 BTC y
  0.876044135 ETH, y Alpaca 0.025553656 y 0.873854024. Era el punto que D-014 dejó sin
  verificar (comisión 0 en el ledger).
- **Observado (paper, 4 ejecuciones de 2 órdenes):** en una compra, Alpaca se queda el
  **0.25 % de cada ejecución en el activo comprado, redondeado hacia arriba a 1e-9**, y la
  orden sigue informando la cantidad bruta. Con esa regla la cantidad neta calculada
  coincide con la posición al último dígito; aplicada al agregado, BTC se queda 1e-9 corto.
- **Decisiones:**
  1. El adaptador calcula la comisión de las ejecuciones de la orden
     (`/account/activities/FILL`). Si aún no suman la cantidad llena, `VenueUnavailableError`
     y la reconciliación vuelve a mirar: la comisión nunca se estima.
  2. El fill registra la cantidad **neta** recibida, y como comisión el valor de lo cobrado
     al precio del fill. El flujo de caja sigue siendo lo pagado de verdad.
  3. `TAKER_FEE_RATE = 0.0025` es una constante del adaptador (tier 1; una market es taker).
     Si el tier cambia, las cantidades dejan de cuadrar y la reconciliación para el sleeve:
     falla cerrado, no en silencio.
  4. **En una venta la comisión va en USD (`qty · precio · 0.25 %`): NO observado todavía.**
     La caja del sleeve no se reconcilia con la de la cuenta, así que un error ahí no lo
     pararía. Hay que verificarlo contra la caja de la cuenta en la primera venta.
  5. Los fills de antes no se reescriben (`fills` es append-only). `execution.fee_correction`
     añade por cada compra anterior un fill `side=sell`, `qty = comisión`, al precio del
     fill y con ese valor como comisión: posición −comisión, realizado 0, caja 0.
     `venue_fill_id` = `<orden>:fee`, así que repetirlo es un no-op. Levanta el kill switch
     (actor = quien lo ejecuta) solo si después el ledger cuadra. En Historial, los días
     anteriores a la corrección muestran la comisión como "fuera del ledger" (+/−), porque
     sus snapshots son de antes de registrarla.
  6. Migración 0004: **`orders.reason`**, el motivo que dio la estrategia para cada orden
     (hasta ahora solo estaba en los logs), y **`position_marks`**, cantidad y precio de
     marca de cada posición abierta en cada snapshot. Con ellos el dashboard dice por qué
     se movió el no realizado (qué símbolo y cuánto) y empareja cada entrada con su salida
     (Operaciones, FIFO).
- **Lo que costó el primer día:** 4 000.82 $ pagados; 10.00 $ de comisión (0.25 %) y
  1.27 $ de slippage frente al mid en la decisión. El resto es que BTC y ETH bajaron un
  ~1–1.5 % tras la entrada: es mercado, no un defecto.

---

## 2026-11-03 · D-016 · El Wheel con cuenta propia: presupuesto y ledger, no la cuenta del broker

- **Contexto:** el Wheel y el sleeve cripto comparten una cuenta paper de Alpaca (D-014).
  El Wheel tomaba como suyas la equity (`get_portfolio_value()`) y la caja (`get_cash()`)
  de toda la cuenta. Tres efectos: la cripto contaba como equity del Wheel (su −0.08 % del
  2026-11-02 era la cripto, sin un solo fill del Wheel); la cabecera del dashboard contaba
  la cripto dos veces; y la caja del presupuesto cripto podía acabar de colateral de una put.
- **Decisiones:**
  1. `WHEEL_BUDGET_USD` (obligatorio). Equity del Wheel = presupuesto + realizado −
     comisiones + no realizado a precio de marca de SUS posiciones. Caja = min(presupuesto +
     flujo de caja de sus fills, caja real de la cuenta): el presupuesto no promete dinero
     que la cuenta no tiene. Es el mismo modelo que el sleeve cripto (`sleeve_cash`, ahora en
     `core/ledger/equity.py`), en `strategies/wheel/accounting.py`, probado sin Lumibot.
  2. Marca de una opción = el mid, el mismo precio con el que decide. Una posición abierta sin
     precio no se estima: esa iteración no escribe snapshot ni opera (rechazo `no_mark`).
  3. **La referencia de la pérdida diaria/semanal mide P&L, no capital asignado:** P&L del
     primer snapshot (equity − asignado) sobre lo asignado ahora. Con la asignación constante
     es idéntica a antes. Sin esto, pasar de ~99 900 $ (cuenta entera) a 90 000 $ habría
     parecido una caída del 10 % y habría disparado el stop semanal. El drawdown del dashboard
     usa la misma curva, y el historial separa el cambio en "Δ asignado".
  4. **Lo que NO cambia todavía:** las órdenes del Wheel siguen por Lumibot, a mercado, sin
     write-ahead ni reconciliación. Pasarlas al router necesita órdenes limit de opciones y
     reconciliar asignaciones y vencimientos, que mueven posiciones sin orden propia. Eso
     depende de cómo los reporta Alpaca en paper (actividades OPASN/OPEXP), y hay que
     verificarlo en horario de mercado antes de escribirlo (CLAUDE.md §5). Es el siguiente
     paso.
- **Efecto visible el día del cambio:** en Historial, el Wheel pasa de ~99 900 $ a 90 000 $
  con todo el cambio en "Δ asignado" y 0 en "fuera del ledger". La cabecera pasa a sumar
  90 000 $ + la equity del sleeve cripto.

---

## 2026-11-03 · D-017 · Diario tipo TradeZella, operaciones manuales y corridas de backtest guardadas

- **Contexto:** el usuario quiere un dashboard como TradeZella para seguir ganancias y
  pérdidas, para el backtest, y para registrar operaciones hechas a mano. Eligió: el
  formulario **dentro del dashboard**; las operaciones manuales **en otra cuenta o broker**;
  y ver las **corridas de backtest guardadas**.
- **Decisiones:**
  1. **El dashboard deja de ser 100 % de solo lectura, por un único camino.** El formulario
     escribe con un rol propio, `manual_journal` (migración 0005), al que Postgres solo deja
     INSERTAR filas de `strategy_id = 'manual'` en `orders` y `fills` (row-level security).
     No puede actualizar ni borrar, ni tocar `risk_events` ni otro sleeve. Lo impone la base
     de datos, no el código del dashboard. El resto del dashboard sigue con su conexión de
     solo lectura. Sin `MANUAL_DATABASE_URL` el formulario está desactivado. Los POST
     exigen Origin (o Referer) del mismo host. Hay que revisar antes de confirmar, y después
     se redirige (Post/Redirect/Get).
  2. **Lo manual no se reconcilia**: está en otra cuenta, sin venue con el que comparar. No
     cuenta en el límite de posiciones del floor que enseña la cabecera, que es del bot.
     Corregir un error es registrar la operación contraria: el ledger sigue append-only.
  3. **Sin "puntuación" compuesta** (el "Zella score"): sus pesos serían números mágicos
     (CLAUDE.md §1.9). Se enseñan sus componentes (profit factor, % de aciertos, ganancia y
     pérdida media, drawdown, factor de recuperación), cada uno con su n.
  4. **Backtest:** el CLI guarda cada configuración del barrido (`backtest_runs`,
     `backtest_equity`, `backtest_fills`, con el motivo de cada operación) con el N
     registrado y su DSR. El Diario nunca muestra su Sharpe, y siempre muestra N y DSR:
     una corrida bonita elegida entre N es justo lo que el DSR deflacta (§1.11).
  5. **P&L del día:** por defecto, el cambio de equity sin el de capital asignado (incluye las
     posiciones abiertas). Opcionalmente, solo lo cerrado ese día. En un sleeve sin
     snapshots (el manual) es lo realizado, y sin capital conocido no hay drawdown en %.
