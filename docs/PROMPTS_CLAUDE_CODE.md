# Prompts de arranque para Claude Code

Uno por fase. Cada uno termina con su gate de salida. **No pases al siguiente hasta
que el gate esté verde y anotado en `DECISIONS.md`.**

Antes de la primera sesión, copia los ficheros del informe al repo con estos
nombres y en estos sitios:

| Fichero entregado | Destino en el repo |
|---|---|
| `INFORME_TALOS.md` | `docs/INFORME_TALOS.md` |
| `CLAUDE.md` | `CLAUDE.md` (raíz) |
| `anexo_A_model.py` | `docs/model.py` |
| `anexo_B_schema.sql` | `docs/schema_reference.sql` (el DDL real va a `migrations/`) |
| `anexo_C_compose.yml` | `docker/compose.yml` |
| `anexo_C_risk_floor.py` | `core/risk_floor.py` |
| `dashboard_mockup.html` | `docs/dashboard_mockup.html` |

Y crea `DECISIONS.md` vacío.

---

## Sesión 0 — Arranque y auditoría

```
Lee CLAUDE.md y docs/INFORME_TALOS.md completos antes de tocar nada.

Tarea de esta sesión, sin escribir código de producción todavía:

1. Audita el repo actual (config_models.py, wheel_strategy.py, crypto_rebalancer.py,
   app_dashboard.py, config.json) y confirma o corrige el inventario de la §1 del
   informe. Si encuentras algo que el informe describe mal, dímelo.

2. Crea la estructura de directorios vacía del árbol de la §9.2, con un __init__.py
   y un docstring de una línea en cada paquete diciendo qué va ahí. Nada más.

3. Monta el andamiaje de proyecto: pyproject.toml con uv y grupos de dependencias por
   servicio (engine, api, llm, dev), Alembic inicializado apuntando a migrations/,
   ruff + mypy en strict para core/ y risk/, pytest con markers (guards, stats,
   integration), pre-commit con ruff y gitleaks, y un GitHub Action que corre
   `pytest -m guards` en cada push.

4. core/risk_floor.py YA EXISTE: es el fichero que he copiado del anexo C. NO lo
   reescribas ni lo reconstruyas desde el resumen de CLAUDE.md §1.1 (ese resumen
   abrevia varias constantes que las fases 2, 3 y 5 necesitan). Lo que sí tienes que
   hacer:
   - verificar que importa limpiamente y que floor_hash() y assert_no_mutation()
     funcionan;
   - escribir tests/guards/test_risk_floor.py que compruebe que assert_no_mutation()
     pasa con el fichero intacto Y LANZA RuntimeError cuando se monkeypatchea una
     constante (este guard tuvo un bug de NameError que lo hacía inútil en silencio;
     el test existe para que no vuelva);
   - cablear la llamada a assert_no_mutation() en el arranque de cada servicio.

5. Crea DECISIONS.md con la primera entrada: fecha, las cuatro decisiones ya tomadas
   (Coinbase como venue primario, router LLM híbrido, dashboard FastAPI, Docker
   Compose + Traefik) y un enlace al informe.

No implementes ninguna estrategia, ningún broker y ningún endpoint todavía.

GATE: `uv run pytest` pasa (aunque sea 0 tests), `ruff check` y `mypy core risk`
limpios, `alembic current` responde, y el Action corre en verde.
```

---

## Sesión 1 — Fase 0.1-0.2: ledger e instrumentación

```
Fase 0 del informe. Sin ledger, cada día de paper trading acumula tiempo sin producir
evidencia, así que esto bloquea todo lo demás.

1. Migración Alembic con el DDL de docs/schema_reference.sql, secciones "ledger" y
   "timescale". Ojo con el orden: las FK de orders.decision_id y fills.decision_id se
   reponen con ALTER TABLE después de crear `decision`, porque `decision` se define
   más abajo. Tablas:
   orders, fills, positions (VISTA derivada de fills, no tabla), equity_curve,
   risk_events, strategy_params. Hipertablas y compresión donde indica el fichero.

2. core/ledger/: fills.py (escritura idempotente con la UNIQUE del schema),
   positions.py (reconstrucción desde fills, y SOLO desde fills), equity.py (snapshot
   por sleeve en cada iteración), attribution.py.

   En attribution.py respeta la convención de signo EXACTAMENTE como está en la vista
   pnl_by_strategy_day del schema de referencia, porque equivocarse aquí hace que el
   dashboard mienta en la dirección favorable:
     net_pnl = gross_pnl − fees + funding − slippage
   con fees >= 0 (coste, se resta), funding CON SIGNO (positivo = cobrado, se suma) y
   slippage >= 0 para ejecución adversa (compra: price − decision_price; venta:
   decision_price − price). Un slippage negativo es ejecución favorable y debe poder
   verse, no absorberse.
   Y el coste de IA se reparte a partes iguales entre las decisiones que usaron cada
   llm_call: una llamada sirve a varios símbolos y varios arms, y atribuirla íntegra a
   cada uno la contaría N veces.

3. core/metrics.py: rescata compute_metrics de app_dashboard.py y añade Sortino,
   Calmar, expectancy, R medio y win rate. Mismo código que usará el backtest, para
   que backtest y live sean directamente comparables.

4. Logging estructurado (structlog) con campos fijos: ts, level, service, strategy_id,
   symbol, action, reason. Cada rechazo de guard emite un evento con su reason.

5. Tests: reconstrucción de posición desde fills == snapshot conocido; doble inserción
   del mismo fill no duplica; atribución de P&L cuadra al centavo contra un fixture
   calculado a mano.

GATE: `pytest -m "not integration"` verde; un script que inyecte 200 fills sintéticos
produce una equity curve y un P&L por sleeve correctos y verificados a mano.
```

---

## Sesión 2 — Fase 0.3-0.6: delta real, base de estrategias, guards

```
1. core/greeks.py. Orden de precedencia estricto:
   (a) greeks del broker si vienen;
   (b) Black-Scholes con IV implícita del mercado, spot, strike, tiempo a expiración
       y tasa libre de riesgo;
   (c) si ninguna de las dos está disponible: SE SALTA EL TRADE y se registra un
       evento con reason='no_greeks'. Nunca adivinar.
   Añade una banda de sanidad: rechaza cualquier contrato cuya delta calculada esté
   fuera del rango plausible para su moneyness.
   Sustituye _estimate_delta() en wheel_strategy.py por esto y BORRA el proxy de
   moneyness. Ese proxy no es una delta y no puede seleccionar contratos.

2. strategies/base.py: extrae de wheel_strategy.py y crypto_rebalancer.py todo lo
   común — carga y hot-reload de parámetros, kill switch, límite de pérdida diaria,
   validación de tamaño de posición, cableado del ledger, declaración de feeds
   requeridos con su TTL. Refactoriza ambas estrategias existentes para heredar de
   ella. No añadas estrategias nuevas en esta sesión: duplicar guards es cómo se
   desincronizan.

3. tests/guards/ completo, según CLAUDE.md §1.8. Cobertura de ramas del 100% en todas
   las funciones de guard. Cada test debe fallar si comento la línea del guard.

4. risk/breakers.py con los 8 circuit breakers del informe §12.4 punto 6, y el kill
   switch triple redundante fail-closed (risk_events ∨ Redis ∨ /run/trading/HALT).

GATE: `pytest -m guards --cov=risk --cov=strategies/base.py --cov-branch` al 100%;
test que demuestra que sin greeks y sin IV se emiten 0 órdenes y 1 log de rechazo.
```

---

## Sesión 3 — Fase 0.7-0.8: backtest honesto y grabador de datos

```
1. backtest/: engine.py (una estrategia, un rango de fechas, usando core/metrics.py),
   cscv.py (Probability of Backtest Overfitting por combinatorially symmetric
   cross-validation), report.py.

   REQUISITO DURO: el informe NO imprime Sharpe. Imprime Deflated Sharpe Ratio, PBO,
   y el N de configuraciones evaluadas. Si N no está registrado, el informe falla con
   un error explícito. Registra N automáticamente: cada llamada al engine con un set
   de parámetros distinto incrementa el contador en una tabla.

2. data/ingest/: ws_runner.py con el watchdog por edad del último mensaje, backoff con
   jitter ±30%, detección de huecos por número de secuencia y gap-fill REST con UPSERT
   idempotente. Y crítico, arráncalo hoy: liquidations.py (WS) y oi.py (snapshots cada
   minuto) escribiendo a Timescale. Cada día sin grabar es histórico que luego habría
   que comprar, y nadie lo vende retroactivamente.

3. data/sources/: fred.py, dvol.py (Deribit, gratis sin key), status_pages.py.

GATE: backtest de crypto_rebalance sobre ≥2 años de datos reales produce un informe
con DSR, PBO y N; el grabador lleva ≥48 h corriendo sin huecos no explicados; una
prueba de caos (matar el contenedor del ingester) se recupera con gap-fill correcto.
```

---

## Sesión 4 — Fase 1: homelab

```
Infraestructura. compose.yml del anexo C como punto de partida, pero revísalo y
justifica cualquier cambio en DECISIONS.md.

1. docker/compose.yml + compose.obs.yml + Traefik con DNS-01 (empieza con el caServer
   de STAGING de Let's Encrypt, o agotarás el rate limit) y split-horizon DNS. Las
   tres redes: proxy, data, obs. El order-router en data y NO en proxy.

2. Secretos: SOPS + age → systemd oneshot → tmpfs → Docker secrets. Script de
   descifrado, y documenta en SECURITY.md que la clave age va respaldada en papel.
   Verifica que `docker inspect` no revela ningún secreto.

3. Observabilidad: VictoriaMetrics (no Prometheus, por RAM) + Grafana + Loki + Alloy +
   Alertmanager + ntfy. Expón las métricas de la lista del informe §8.2/anexo, y crea
   una regla de alerta por cada una. ws_last_message_age_seconds es la más importante.

4. Backups: pgbackrest.conf con WAL continuo + restic a B2. Y el script
   restore_test.sh que hace el game-day completo: compose desechable, restaura,
   reconstruye la posición desde fills, la compara con un snapshot guardado, arranca
   en dry-run, y cronometra. Ojo con timescaledb_pre_restore/post_restore.

5. nut-upsd: al pasar a batería, publica un evento en el bus → estrategias a
   reduce-only → apagado limpio de Postgres.

6. Dead-man's switch: script que hace curl cada 60s a un monitor externo, más el
   agente flatten-only para el VPS de fuera (con su propio compose mínimo).

GATE: el dashboard responde por HTTPS con cert válido desde la tailnet y sin ningún
puerto abierto al exterior; el game-day de restauración completa en <4 h, cronometrado
y anotado en RUNNING.md; se mata el host y la alerta llega desde fuera en <5 min.
```

---

## Sesión 5 — Fase 2: brokers, ejecución, allocator

```
PARA Y PREGÚNTAME antes de empezar si los puntos 1, 4, 5, 6 y 8 de la §13 del informe
siguen sin verificar. Dependen de páginas que yo tengo que leer, no tú.

1. brokers/base.py: mantén el SpotBroker Protocol que ya existe (es el mejor acierto
   del repo) y añade PerpBroker con get_positions, get_funding_rate, place_order
   (con client_order_id), place_reduce_only_stop, cancel, get_order_by_client_id.
   Generaliza PaperSpotBroker a brokers/paper.py para que cubra ambos Protocols.

2. brokers/coinbase_perp.py contra el SANDBOX. Maneja explícitamente que el símbolo
   lleva código de expiración (p.ej. BIP-20DEC30-CDE) y que el funding se devenga: el
   modelo de datos debe registrar cada pago de funding como una fila en fills con
   funding_payment y qty=0.

3. execution/: idempotency.py con el write-ahead intent de CLAUDE.md §1.4;
   reconcile.py como gate de arranque (verifica reloj → consulta el exchange → diff
   contra Postgres → el exchange gana en posición → si |delta| > umbral, modo seguro y
   ack humano) y como bucle cada 5-15 min; protective.py que coloca el stop
   reduce-only en el exchange en la misma unidad de trabajo que el fill de entrada.

4. core/allocator.py: presupuesto por sleeve, enforcement de que la suma del colateral
   comprometido nunca excede la caja disponible, y DOS caps de exposición distintos
   que no hay que confundir:
   - MAX_GROSS_EXPOSURE_PCT = 1.60 sobre la suma de |notional|. Es 1.60 y no 1.00
     porque funding_carry consume 2x bruto por 1x neto (largo spot + corto perp):
     con la asignación de §4.2 el bruto es 0.8+0.4+0.2 = 1.4.
   - MAX_NET_EXPOSURE_PCT = 1.00 sobre el delta agregado. Éste es el que limita el
     riesgo direccional.
   Más el cap ajustado por correlación con la matriz rodante (informe §4.6: 16
   posiciones a rho=0.8 son 1.23 apuestas independientes y un cap del 1% por
   posición da 3.61% de riesgo de cartera; usa core.risk_floor.correlation_adjusted_risk).

5. Test de caos obligatorio: matar el order-router entre el INSERT y el POST, y entre
   el POST y el ack. En ambos casos: 0 órdenes duplicadas y reconciliación correcta al
   reiniciar.

GATE: una orden de tamaño mínimo real en el sandbox de cada venue confirma que el
client_order_id se acepta, vuelve en el ack y es consultable después; el test de caos
pasa en las dos variantes; el allocator rechaza correctamente el quinto sleeve cuando
los cuatro anteriores han comprometido la caja.
```

---

## Sesión 6 — Fase 2: los tres sleeves nuevos

```
Uno por PR, en este orden, y ninguno pasa a paper sin su backtest.

1. strategies/funding_carry/ — delta-neutral: largo spot + corto perp del mismo
   subyacente, dimensionado para capturar el funding. Es el sleeve con mejor respaldo
   académico y su coste de datos es cero. Modela explícitamente los 33 bps de coste
   round-trip de las dos patas: con funding al 10.95% APR (3.0 bps/día) el break-even
   está en ~10 días de tenencia, así que la lógica de entrada debe exigir un funding
   proyectado que justifique la tenencia mínima. Y cubre el riesgo de ADL: el 10 de
   octubre de 2025 hubo auto-deleveraging incluso en libros cubiertos.

2. strategies/xsec_momentum/ — cross-seccional, rebalanceo SEMANAL, 10-16 patas.
   RESTRICCIÓN IMPORTANTE que sale del risk floor: la pata CORTA solo puede
   expresarse en perpetuos de BTC/ETH (MAX_LEVERAGE_MAJORS = 1.5). En alts el floor
   impone spot a 1x, y en spot sin margen no se puede vender en corto, así que los
   alts son long/flat. El ranking cross-seccional sigue siendo el mismo; lo que
   cambia es cómo se expresa el lado corto. No intentes resolverlo subiendo
   MAX_LEVERAGE_ALTS: eso es el invariante 1.1 y la respuesta es no.
   Mide el turnover y hazlo cumplir: ≤21 round-trips equivalentes al año
   (MAX_ROUNDTRIPS_PER_YEAR['xsec_momentum']). Si el backtest quiere rotar más, el
   backtest está sobreajustado.

3. strategies/trend/ — BTC/ETH, horizonte días-semanas. Win rate esperado 30-40% con
   payoff 2.5-3.5:1, así que dimensiona sabiendo que con 35% de win rate la racha
   perdedora esperada es de 13.6 sobre 1000 operaciones y 15.2 sobre 2000: con r=1%
   por operación eso es −13% de drawdown sin que nada esté roto.

Para los tres: vol targeting (informe §5.2 — la evidencia está disputada, documéntalo
así en el docstring), ½-Kelly como techo de sizing (no es conservadurismo, es
corrección del sesgo de estimación de SR), y feeds declarados con TTL.

GATE por sleeve: backtest sobre ≥2 años con PBO < 0.5 y DSR > 0, N registrado, y el
turnover medido dentro del presupuesto. Un sleeve que no pase su gate no entra en
paper.
```

---

## Sesión 7 — Fase 2: API y dashboard

```
1. api/ FastAPI, SOLO LECTURA sobre el ledger, con SSE (sse-starlette) para el live.
   Los únicos POST del sistema: kill switch y aprobación de propuestas, ambos con
   confirmación explícita.

2. web/ con HTMX + Alpine + uPlot. En esta sesión, SOLO LAS VISTAS 1, 2, 5 y 6 de
   la §8.2 del informe. Las vistas 3 (aprendizaje) y 4 (propuestas) dependen de
   core/stats/ y del motor de hipótesis, que se construyen en las sesiones 8 y 9;
   hacerlas ahora sería maquetar contra un backend que no existe.
   docs/dashboard_mockup.html es la referencia visual: replica ese lenguaje.

   La vista 2 (P&L por estrategia) es la razón por la que existe este dashboard.
   Incluye el waterfall bruto → fees → financiación → slippage → coste de IA → neto.
   Toda cifra de P&L es NETA, o no se muestra.

   En la vista 3, la confianza declarada por el LLM se muestra tachada, junto a la
   calibrada, con la etiqueta "no usada para decidir". Y se muestran siempre
   n_observed Y n_effective, gobernando n_effective.

3. Grafana embebido por iframe para exploración profunda de series.

4. Borra app_dashboard.py y quita streamlit y plotly de pyproject.toml.

GATE: el P&L por estrategia cuadra al centavo contra una consulta SQL directa al
ledger; el SSE sobrevive a matar y levantar el backend; ninguna cifra de retorno
mostrada es bruta.
```

---

## Sesión 8 — Fase 3: motor estadístico (antes del LLM)

```
Esta sesión es matemática pura y no toca el LLM. Es el orden correcto: el LLM propone
texto, pero código determinista decide. El precedente es POPPER (arXiv 2502.09858).

core/stats/:
1. power.py — n_required derivado: test de proporción, test t sobre retornos, y
   binomial exacto para DEFECT. Cada función devuelve (n, formula_string). PROHIBIDO
   hardcodear cualquier n.
2. effective_n.py — deflación por clúster n/(1+(m̄−1)ρ_ICC) y tamaño efectivo de Kish
   con pesos de decaimiento.
3. evalue.py — martingala de capital por apuestas (Waudby-Smith & Ramdas, JRSS-B 2024)
   con la apuesta hedged/GRAPA, y la confidence sequence dual. cs_lower es el único
   número que el sistema puede llamar "confianza".
4. ebh.py — e-BH (Wang & Ramdas 2022), que controla FDR bajo dependencia arbitraria,
   más la variante stopped e-BH válida en cualquier momento. Y el presupuesto de
   alpha investing.
5. drift.py — ADWIN (usa river.drift.ADWIN) y Page-Hinkley.
6. deflated_sharpe.py — DSR y PBO por CSCV.

TESTS DE PROPIEDAD, no ejemplos:
- Simula 10,000 hipótesis nulas y verifica que el FDR empírico de e-BH ≤ q.
- Verifica que la cobertura de la confidence sequence ≥ 1−α con peeking continuo.
- Verifica que la martingala nunca cruza 1/α más del α% de las veces bajo el nulo.
- Verifica que Beta(50,50) con 5 de 5 éxitos da P(θ>0.6) ≈ 0.02, no 0.95.

GATE: los cuatro tests de propiedad pasan con 10,000 réplicas. Si el FDR empírico
supera q, el bug está en tu implementación de e-BH, no en el test.
```

---

## Sesión 9 — Fase 3: la capa LLM

```
Lee la §5 y §7 del informe otra vez antes de empezar. El resumen: el LLM NO genera
señales. Y lee CLAUDE.md §1.2 sobre el espacio de acción monótono.

1. llm/router.py + config/routing.yaml — política de routing: modelo local para
   trabajo rutinario, API para razonamiento. Y llm/fallback.py: el ladder
   cloud → local → determinista. Semántica de fallo: fail_open si la acción reduce
   riesgo, fail_closed si lo aumenta.
   Gobernador de tokens: presupuesto mensual como métrica, degradación al 80% (menos
   contexto, modelo más pequeño), corte al 95%.

2. llm/features.py — ROL 1, el único con soporte académico. Noticias → JSON estricto
   {event_type, polarity, magnitude, horizon, entities, confidence}, con caché, TTL y
   versionado del prompt. Mide F1 contra un dataset de ≥200 noticias etiquetadas a
   mano, y reporta el Information Coefficient del feature. Si el IC no es
   distinguible de cero, el feature no entra.

3. llm/hypothesis/ — ROL 2. propose.py (batch nocturno, el LLM propone),
   engine.py (el motor de core/stats decide), lifecycle.py (las transiciones de estado
   del schema, los cuatro mecanismos de expiración de §7.6).
   El LLM clasifica la hipótesis en DEFECT/RISK/EDGE y el sistema DERIVA n_required
   según la clase. discovery_cutoff_ts obligatorio.

4. llm/calibration.py — regresión isotónica sobre la confianza declarada. Se guarda,
   no se usa para decidir.

5. llm/gate.py — ROL 3, en SHADOW MODE. Registra su decisión contrafactual pero no
   tiene autoridad. 30 días mínimo antes de plantear darle autoridad.

6. llm/proposals.py — ROL 5. Escribe a la cola, nunca a strategy_params. Con límite
   duro de propuestas por semana, y expiración de las no revisadas (la fatiga de
   aprobación es el riesgo real aquí).

7. llm/audit.py — cada llamada registrada con prompt_sha256, blobs
   content-addressed, tokens, latencia, policy_version, idempotency_key.

CADENCIA: event-gated, no rejilla. La llamada se dispara solo cuando un detector
determinista de novedad lo justifica. La diferencia entre rejilla y event-gating es
$1,198/mes vs $31/mes, y el event-gating además es mejor ingeniería: 96 evaluaciones
diarias donde nada ha cambiado son 96 oportunidades de que el modelo invente una razón
para actuar.

GATE: simular 429 y 529 de la API y verificar que el bot SIGUE OPERANDO en modo
determinista, nunca se para (es el fallo del vídeo). Una hipótesis DEFECT inyectada se
confirma en ≤5 casos. Una hipótesis EDGE nula NUNCA se confirma en 10,000
simulaciones. Ningún efecto de ninguna hipótesis puede aumentar el riesgo (test
exhaustivo sobre todos los effect_kind).
```

---

## Sesión 10 — Fase 4: paper trading y operación

```
1. scripts/: reconcile_now.py, flatten_all.py (el botón de pánico), export_taxes.py
   con tratamiento §1256 separado del resto — esto lo necesitará tu CPA y hay que
   construirlo antes del primer año fiscal en vivo, no durante.

2. notifications.py: fills, asignaciones, triggers de guard, límite diario, crashes,
   resumen diario. Las asignaciones son las que más importan: es cuando la estrategia
   cambia de fase.

3. RUNNING.md ampliado: runbook de homelab, runbook de DR, y el runbook de incidentes.
   El PRIMER paso del runbook de incidentes es "para el bot y gestiona la posición a
   mano desde la web del exchange", no "restaura el backup". Tu evento de DR más
   probable no es un disco muerto: eres tú desplegando un bug.

4. Arranca el reloj de los 60-90 días de paper. Informe semanal automático con los
   gates de la Fase 4 del informe §10, incluyendo el retorno vs BTC HODL en la misma
   ventana, gane o pierda.

GATE (el de verdad, el que decide si esto toca dinero): la tabla completa de la Fase 4
de la §10 del informe, todas las filas en verde. ≥60 días, ≥40 posiciones cerradas en
los sleeves nuevos, 0 mismatches sin explicar, 0 reinicios con pérdida de estado,
0 guards disparados sin documentar, DSR del BACKTEST > 0 con N registrado, PBO < 0.5,
coste fijo < 4% anual, corte de LLM simulado sin que el bot se pare, y un game-day de
restauración exitoso con RTO cronometrado.

Y una cosa que hay que decirle al usuario con claridad al cerrar esta fase: este gate
es OPERACIONAL, no estadístico. 60-90 días a esta frecuencia no pueden demostrar que
el edge existe (§2.3 y §7.3 del informe). Lo que demuestran es que el sistema no se
rompe. No presentes el resultado del paper como evidencia de rentabilidad.
```
