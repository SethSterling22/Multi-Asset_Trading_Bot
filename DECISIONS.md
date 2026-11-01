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
