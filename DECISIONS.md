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
