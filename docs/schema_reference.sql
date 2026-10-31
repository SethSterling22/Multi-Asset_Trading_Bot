-- ============================================================================
-- Anexo B — DDL completo
-- Multi-Asset_Trading_Bot · PostgreSQL 17 + TimescaleDB
--
-- Este fichero es la referencia. La implementación va en migrations/ (Alembic).
-- Secciones: 0 tipos · 1 ledger · 2 timescale · 3 decisiones y LLM
--            4 hipótesis · 5 propuestas · 6 vistas de atribución
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================
-- 0 · TIPOS
-- ============================================================================

CREATE TYPE asset_type AS ENUM ('stock','option','crypto_spot','crypto_perp','future');
CREATE TYPE order_status AS ENUM (
  'PENDING_NEW',   -- write-ahead intent: escrito ANTES del POST al exchange
  'NEW','PARTIALLY_FILLED','FILLED','CANCELED','REJECTED','EXPIRED','UNKNOWN'
);

CREATE TYPE hypothesis_status AS ENUM (
  'proposed',      -- el LLM lo propuso, sin evidencia prospectiva
  'registered',    -- pre-registrado: scope y predicción congelados, contador a 0
  'accumulating',  -- recibiendo observaciones prospectivas
  'confirmed',     -- e-value cruzó umbral Y pasó e-BH Y out-of-sample OK
  'refuted',       -- la predicción falsable falló
  'expired',       -- TTL agotado sin resolución
  'stale',         -- estuvo confirmado, se detectó drift, desactivado
  'retired'        -- retirado manualmente / defecto arreglado
);

CREATE TYPE hypothesis_class AS ENUM ('defect','risk','edge');

-- Vocabulario CERRADO de efectos. Todos monótonos hacia menos riesgo.
CREATE TYPE effect_kind AS ENUM (
  'observe_only',        -- sin efecto, solo se anota
  'annotate_decision',   -- añade un tag al registro de decisión
  'size_multiplier',     -- SOLO en [0.25, 1.00]. Nunca > 1.0
  'veto_entry',          -- bloquea apertura
  'tighten_stop',        -- SOLO reduce la distancia al stop
  'extend_cooldown',     -- SOLO aumenta el cooldown
  'block_component',     -- circuit-breaker de un componente (DEFECT)
  'propose_config_diff'  -- va a la cola de aprobación humana, NO se aplica
);

-- ============================================================================
-- 1 · LEDGER  (la verdad del sistema; append-only)
-- ============================================================================

CREATE TABLE strategy (
  strategy_id   text PRIMARY KEY,           -- 'wheel','funding_carry','xsec_momentum'…
  display_name  text NOT NULL,
  enabled       boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Parámetros versionados. Nunca se hace UPDATE: se inserta una versión nueva.
CREATE TABLE strategy_params (
  id            bigserial PRIMARY KEY,
  strategy_id   text NOT NULL REFERENCES strategy(strategy_id),
  version       integer NOT NULL,
  params        jsonb NOT NULL,
  approved_by   text NOT NULL,              -- 'human:sebastian' | 'default'
  proposal_id   uuid,                       -- de dónde vino, si vino de una propuesta
  active_from   timestamptz NOT NULL DEFAULT now(),
  active_to     timestamptz,                -- NULL = versión activa
  UNIQUE (strategy_id, version)
);
CREATE UNIQUE INDEX one_active_version_per_strategy
  ON strategy_params (strategy_id) WHERE active_to IS NULL;

CREATE TABLE orders (
  client_order_id  text PRIMARY KEY,        -- determinista: {strat}-{sym}-{bar}-{n}
  ts_intent        timestamptz NOT NULL DEFAULT now(),  -- write-ahead
  ts_ack           timestamptz,
  venue            text NOT NULL,
  symbol           text NOT NULL,
  asset_type       asset_type NOT NULL,
  side             text NOT NULL,
  qty              numeric NOT NULL CHECK (qty > 0),
  order_type       text NOT NULL,
  limit_price      numeric,
  reduce_only      boolean NOT NULL DEFAULT false,
  status           order_status NOT NULL DEFAULT 'PENDING_NEW',
  venue_order_id   text,
  reject_reason    text,
  strategy_id      text NOT NULL REFERENCES strategy(strategy_id),
  decision_id      uuid,
  decision_price   numeric,                 -- precio en el momento de decidir
  risk_floor_hash  bytea NOT NULL,          -- qué risk floor estaba vigente
  attempt          integer NOT NULL DEFAULT 1
);
CREATE INDEX orders_open ON orders (venue, status)
  WHERE status IN ('PENDING_NEW','NEW','PARTIALLY_FILLED');

-- El ledger de fills. APPEND-ONLY. Nada se actualiza, nada se borra.
-- strategy_id es la columna que habilita todo el P&L por estrategia.
CREATE TABLE fills (
  id               bigserial PRIMARY KEY,
  ts               timestamptz NOT NULL,
  venue            text NOT NULL,
  symbol           text NOT NULL,
  asset_type       asset_type NOT NULL,
  side             text NOT NULL,
  qty              numeric NOT NULL,        -- 0 permitido: filas de funding
  price            numeric NOT NULL,
  commission       numeric NOT NULL DEFAULT 0,
  funding_payment  numeric NOT NULL DEFAULT 0,  -- signo incluido
  realized_pnl     numeric,
  decision_price   numeric,                 -- para el slippage
  strategy_id      text NOT NULL REFERENCES strategy(strategy_id),
  decision_id      uuid,
  client_order_id  text REFERENCES orders(client_order_id),
  venue_fill_id    text,
  UNIQUE (venue, venue_fill_id)             -- idempotencia del gap-fill
);
CREATE INDEX fills_strategy_ts ON fills (strategy_id, ts DESC);
CREATE INDEX fills_symbol_ts   ON fills (symbol, ts DESC);

-- La posición es una VISTA. Nunca una tabla, nunca memoria de proceso.
CREATE VIEW positions AS
SELECT venue, symbol, asset_type, strategy_id,
       SUM(CASE WHEN side IN ('buy','buy_to_open','buy_to_close') THEN qty ELSE -qty END) AS net_qty,
       SUM(CASE WHEN side IN ('buy','buy_to_open','buy_to_close') THEN qty*price ELSE -qty*price END) AS net_cost,
       SUM(commission)      AS total_commission,
       SUM(funding_payment) AS total_funding,
       MAX(ts)              AS last_fill_ts
FROM fills
GROUP BY venue, symbol, asset_type, strategy_id
HAVING ABS(SUM(CASE WHEN side IN ('buy','buy_to_open','buy_to_close') THEN qty ELSE -qty END)) > 1e-12;

CREATE TABLE risk_events (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  kind         text NOT NULL,   -- 'kill_switch_on'|'kill_switch_off'|'breaker_open'|…
  scope        text NOT NULL,   -- 'global'|strategy_id|venue
  reason       text NOT NULL,
  actor        text NOT NULL,   -- 'human:sebastian'|'risk-monitor'|'reconciler'
  metadata     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX risk_events_recent ON risk_events (ts DESC);

CREATE TABLE reconciliation_runs (
  id             bigserial PRIMARY KEY,
  ts             timestamptz NOT NULL DEFAULT now(),
  venue          text NOT NULL,
  mismatches     integer NOT NULL,
  detail         jsonb NOT NULL,
  action_taken   text NOT NULL   -- 'none'|'safe_mode'|'human_ack_required'
);

-- ============================================================================
-- 2 · TIMESCALE  (series temporales de mercado)
-- ============================================================================

CREATE TABLE trades (
  ts       timestamptz      NOT NULL,
  venue    text             NOT NULL,
  symbol   text             NOT NULL,
  trade_id text             NOT NULL,
  price    double precision NOT NULL,
  amount   double precision NOT NULL,
  side     smallint         NOT NULL,     -- 1 buy, -1 sell
  PRIMARY KEY (venue, symbol, ts, trade_id)   -- idempotencia del gap-fill
);
SELECT create_hypertable('trades','ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE trades SET (
  timescaledb.compress           = true,
  timescaledb.compress_segmentby = 'venue, symbol',  -- filtras por esto; NUNCA trade_id
  timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('trades', INTERVAL '7 days');
SELECT add_retention_policy ('trades', INTERVAL '400 days');  -- tras exportar a Parquet

-- Top-of-book DERIVADO a 1s. No almacenamos deltas L2 crudos: serían TB,
-- y esto conserva ~95% del valor de features con 3 órdenes de magnitud menos.
CREATE TABLE book_1s (
  ts             timestamptz NOT NULL,
  venue          text NOT NULL,
  symbol         text NOT NULL,
  bid            double precision,
  ask            double precision,
  mid            double precision,
  spread_bps     double precision,
  depth_10bps    double precision,
  depth_50bps    double precision,
  imbalance      double precision,
  PRIMARY KEY (venue, symbol, ts)
);
SELECT create_hypertable('book_1s','ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE book_1s SET (timescaledb.compress = true,
  timescaledb.compress_segmentby = 'venue, symbol', timescaledb.compress_orderby = 'ts DESC');
SELECT add_compression_policy('book_1s', INTERVAL '3 days');
SELECT add_retention_policy ('book_1s', INTERVAL '180 days');

CREATE TABLE funding (
  ts            timestamptz NOT NULL,
  venue         text NOT NULL,
  symbol        text NOT NULL,
  funding_rate  double precision NOT NULL,
  interval_h    double precision NOT NULL,
  mark_price    double precision,
  index_price   double precision,
  PRIMARY KEY (venue, symbol, ts)
);
SELECT create_hypertable('funding','ts', chunk_time_interval => INTERVAL '1 month');

CREATE TABLE open_interest (
  ts        timestamptz NOT NULL,
  venue     text NOT NULL,
  symbol    text NOT NULL,
  oi_base   double precision,
  oi_usd    double precision,
  PRIMARY KEY (venue, symbol, ts)
);
SELECT create_hypertable('open_interest','ts', chunk_time_interval => INTERVAL '1 month');
ALTER TABLE open_interest SET (timescaledb.compress = true,
  timescaledb.compress_segmentby = 'venue, symbol');
SELECT add_compression_policy('open_interest', INTERVAL '7 days');

-- Tu propio tape. NADIE te lo vende retroactivamente: grábalo desde el día 1.
CREATE TABLE liquidations (
  ts        timestamptz NOT NULL,
  venue     text NOT NULL,
  symbol    text NOT NULL,
  side      smallint NOT NULL,
  qty       double precision NOT NULL,
  price     double precision NOT NULL,
  usd       double precision NOT NULL,
  raw_id    text,
  PRIMARY KEY (venue, symbol, ts, side, qty, price)
);
SELECT create_hypertable('liquidations','ts', chunk_time_interval => INTERVAL '7 days');
ALTER TABLE liquidations SET (timescaledb.compress = true,
  timescaledb.compress_segmentby = 'venue, symbol');
SELECT add_compression_policy('liquidations', INTERVAL '7 days');
-- SIN retention policy: este dataset es irreemplazable.

-- Equity por sleeve. La base del P&L por estrategia.
CREATE TABLE equity_curve (
  ts             timestamptz NOT NULL,
  strategy_id    text NOT NULL,
  equity_usd     double precision NOT NULL,
  allocated_usd  double precision NOT NULL,
  gross_notional double precision NOT NULL,
  unrealized_pnl double precision NOT NULL DEFAULT 0,
  PRIMARY KEY (strategy_id, ts)
);
SELECT create_hypertable('equity_curve','ts', chunk_time_interval => INTERVAL '7 days');

-- Agregados continuos: 1m base, y la jerarquía encima.
CREATE MATERIALIZED VIEW ohlcv_1m WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket, venue, symbol,
       first(price,ts) AS open, max(price) AS high, min(price) AS low,
       last(price,ts)  AS close, sum(amount) AS volume,
       sum(CASE WHEN side = 1 THEN amount ELSE 0 END) AS buy_volume,
       count(*) AS n_trades
FROM trades GROUP BY 1,2,3 WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_1m',
  start_offset      => INTERVAL '3 hours',   -- re-materializa: cubre los gap-fills
  end_offset        => INTERVAL '1 minute',  -- no toques el bucket en curso
  schedule_interval => INTERVAL '1 minute');

CREATE MATERIALIZED VIEW ohlcv_1h WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', bucket) AS bucket, venue, symbol,
       first(open,bucket) AS open, max(high) AS high, min(low) AS low,
       last(close,bucket) AS close, sum(volume) AS volume, sum(n_trades) AS n_trades
FROM ohlcv_1m GROUP BY 1,2,3 WITH NO DATA;
SELECT add_continuous_aggregate_policy('ohlcv_1h',
  start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour');

-- ============================================================================
-- 3 · DECISIONES Y LLAMADAS AL LLM  (auditoría completa)
-- ============================================================================

CREATE TABLE llm_call (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at         timestamptz NOT NULL DEFAULT now(),
  purpose            text NOT NULL,    -- 'gate'|'feature'|'hypothesis_gen'|'regime'|'proposal'
  provider           text NOT NULL,    -- 'anthropic'|'local'
  model              text NOT NULL,
  temperature        double precision,
  prompt_sha256      bytea NOT NULL,
  prompt_blob_uri    text NOT NULL,    -- cold storage, content-addressed
  response_blob_uri  text NOT NULL,
  input_tokens       integer,
  cache_read_tokens  integer,
  cache_write_tokens integer,
  output_tokens      integer,
  cost_usd           numeric,          -- para el presupuesto y la atribución
  latency_ms         integer,
  http_status        integer,
  idempotency_key    text NOT NULL UNIQUE,   -- un reintento reusa la respuesta
  policy_version     text NOT NULL
);
CREATE INDEX llm_call_cost ON llm_call (created_at DESC) INCLUDE (cost_usd);

CREATE TABLE decision (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ts                    timestamptz NOT NULL DEFAULT now(),
  strategy_id           text NOT NULL REFERENCES strategy(strategy_id),
  symbol                text NOT NULL,
  bar_id                bigint NOT NULL,
  feature_snapshot_hash bytea NOT NULL,
  baseline_intent       jsonb NOT NULL,   -- lo que decidió el determinista
  applied_hypotheses    uuid[] NOT NULL DEFAULT '{}',
  llm_call_id           uuid REFERENCES llm_call(id),
  llm_action            text,             -- allow|veto|shrink|abstain
  final_intent          jsonb NOT NULL,
  arm                   text NOT NULL,    -- 'baseline'|'llm'|'shadow'
  risk_floor_version    text NOT NULL,
  regime_key            text,
  realized_r            double precision, -- se rellena al cerrar
  UNIQUE (strategy_id, symbol, bar_id, arm)  -- idempotencia dura
);

-- ============================================================================
-- 4 · HIPÓTESIS
-- ============================================================================

-- Claves foráneas diferidas: orders/fills se crean antes que `decision`, y
-- strategy_params antes que `proposal`. Se reponen aquí, una vez existen.
ALTER TABLE orders ADD CONSTRAINT orders_decision_fk
  FOREIGN KEY (decision_id) REFERENCES decision(id);
ALTER TABLE fills  ADD CONSTRAINT fills_decision_fk
  FOREIGN KEY (decision_id) REFERENCES decision(id);

CREATE TABLE hypothesis (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  class                  hypothesis_class NOT NULL,
  status                 hypothesis_status NOT NULL DEFAULT 'proposed',

  -- Contenido generado por el LLM: texto, no ejecutable
  statement              text NOT NULL,
  falsifiable_prediction text NOT NULL,
  rationale              text,

  -- SCOPE: congelado al pasar a 'registered'. Define QUÉ observaciones cuentan.
  -- Se compila a un predicado determinista; el LLM NO evalúa la pertenencia.
  scope                  jsonb NOT NULL,
  scope_predicate_sql    text NOT NULL,
  scope_hash             bytea NOT NULL,

  -- HIPÓTESIS ESTADÍSTICA: explícita y obligatoria
  metric                 text NOT NULL,        -- 'is_win'|'r_multiple'|…
  test_kind              text NOT NULL,        -- 'binomial'|'t_returns'|'exact_binomial'
  null_value             double precision NOT NULL,   -- p0 o mu0
  alt_value              double precision NOT NULL,   -- p1 o mu1 (MDE pre-registrado)
  sigma_prior            double precision,
  alpha                  double precision NOT NULL DEFAULT 0.01,
  power                  double precision NOT NULL DEFAULT 0.80,

  -- EVIDENCIA
  n_observed             integer NOT NULL DEFAULT 0,
  n_effective            double precision NOT NULL DEFAULT 0,  -- clúster + decay
  n_successes            double precision NOT NULL DEFAULT 0,  -- ponderado
  n_required             integer NOT NULL,     -- DERIVADO, nunca literal
  n_required_formula     text NOT NULL,        -- auditoría de la derivación

  -- INFERENCIA: las dos vías, ambas almacenadas
  post_alpha             double precision NOT NULL DEFAULT 1.0,
  post_beta              double precision NOT NULL DEFAULT 1.0,
  posterior_mean         double precision,
  posterior_p_gt_min     double precision,
  cs_lower               double precision,     -- ← el único "confianza" honesto
  cs_upper               double precision,
  log_evalue             double precision NOT NULL DEFAULT 0,
  evalue_threshold       double precision NOT NULL,   -- 1/alpha (Ville)
  q_fdr                  double precision,
  passed_ebh             boolean NOT NULL DEFAULT false,

  -- GATE DE ACCIÓN: UNA sola puerta, derivada
  min_effect_to_act      double precision NOT NULL,
  can_act                boolean NOT NULL DEFAULT false,

  -- EFECTO PERMITIDO: acotado y monótono a menos riesgo
  effect_kind            effect_kind NOT NULL DEFAULT 'observe_only',
  effect_params          jsonb NOT NULL DEFAULT '{}',
  effect_bound_checked   boolean NOT NULL DEFAULT false,

  -- NO ESTACIONARIEDAD
  half_life_days         double precision NOT NULL DEFAULT 30,
  ttl_days               integer NOT NULL DEFAULT 90,
  adwin_state            jsonb,
  ph_state               jsonb,
  drift_detected_at      timestamptz,
  last_revalidated_at    timestamptz,

  -- OUT-OF-SAMPLE: obligatorio, la hipótesis nace de datos ya vistos
  discovery_cutoff_ts    timestamptz NOT NULL,
  oos_n                  integer NOT NULL DEFAULT 0,
  oos_ok                 boolean NOT NULL DEFAULT false,

  -- PROCEDENCIA
  proposed_by_model      text NOT NULL,
  proposer_llm_call_id   uuid NOT NULL REFERENCES llm_call(id),
  llm_stated_confidence  double precision,   -- se GUARDA, no se usa para decidir
  llm_conf_calibrated    double precision,   -- tras regresión isotónica

  created_at             timestamptz NOT NULL DEFAULT now(),
  registered_at          timestamptz,
  confirmed_at           timestamptz,
  refuted_at             timestamptz,
  expired_at             timestamptz,

  -- ------------------------------------------------------------------------
  -- LOS CHECKS QUE ACOTAN EL DAÑO POSIBLE.
  -- Son la PRIMERA de tres capas: la base de datos no puede validar la
  -- semántica de un effect_params arbitrario, así que además hay un tipo
  -- validado en Python y un test exhaustivo sobre todos los effect_kind.
  -- ------------------------------------------------------------------------
  -- El multiplicador de tamaño nunca amplifica.
  CONSTRAINT no_size_increase CHECK (
    effect_kind <> 'size_multiplier'
    OR ((effect_params->>'multiplier')::float BETWEEN 0.25 AND 1.00)
  ),
  -- El cooldown solo puede alargarse.
  CONSTRAINT cooldown_only_extends CHECK (
    effect_kind <> 'extend_cooldown'
    OR ((effect_params->>'minutes')::float > 0)
  ),
  -- El stop solo puede estrecharse: el factor va sobre la distancia al stop.
  CONSTRAINT stop_only_tightens CHECK (
    effect_kind <> 'tighten_stop'
    OR ((effect_params->>'distance_factor')::float BETWEEN 0.30 AND 1.00)
  ),
  -- Para actuar hacen falta CINCO condiciones, no una. El umbral ingenuo de
  -- n_effective >= 1 lo pasaban los 5 casos agrupados del vídeo (n_eff = 1.09),
  -- que es justo el falso descubrimiento que esto existe para impedir.
  CONSTRAINT act_requires_evidence CHECK (
    NOT can_act OR (
          passed_ebh
      AND oos_ok
      AND n_effective >= GREATEST(2.0, n_required * 0.8)
      AND cs_lower > min_effect_to_act
      AND log_evalue >= ln(evalue_threshold)
    )
  ),
  -- La muestra de descubrimiento nunca cuenta como evidencia.
  CONSTRAINT oos_only CHECK (
    registered_at IS NULL OR registered_at >= discovery_cutoff_ts
  )
);
CREATE INDEX hypothesis_live ON hypothesis (status, class) WHERE status IN ('accumulating','confirmed');
CREATE INDEX hypothesis_actionable ON hypothesis (can_act) WHERE can_act;

-- Append-only. Nunca UPDATE, nunca DELETE.
CREATE TABLE hypothesis_observation (
  id              bigserial PRIMARY KEY,
  hypothesis_id   uuid NOT NULL REFERENCES hypothesis(id),
  decision_id     uuid NOT NULL REFERENCES decision(id),
  observed_at     timestamptz NOT NULL,
  metric_value    double precision NOT NULL,
  success         boolean,                   -- para el test binomial
  cluster_id      text NOT NULL,             -- symbol||day||component
  regime_key      text NOT NULL,
  decay_weight    double precision NOT NULL, -- gamma^(age/half_life)
  lambda_bet      double precision,          -- apuesta usada en la martingala
  log_evalue_inc  double precision NOT NULL, -- log(1 + lambda*(X - p0))
  is_oos          boolean NOT NULL,
  UNIQUE (hypothesis_id, decision_id)        -- idempotencia
);

-- Registro de N: el contador que hace calculable el Deflated Sharpe Ratio.
CREATE TABLE search_log (
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  kind          text NOT NULL,     -- 'backtest_config'|'hypothesis_proposed'
  strategy_id   text,
  params_hash   bytea NOT NULL,
  params        jsonb NOT NULL,
  outcome       jsonb,             -- métricas in-sample, si aplica
  discarded     boolean NOT NULL DEFAULT false
);
-- SELECT count(*) FROM search_log WHERE kind='backtest_config' AND strategy_id=$1
--   → este es el N que va al DSR. Incluye las descartadas.

-- ============================================================================
-- 5 · PROPUESTAS DE CONFIG  (cola de aprobación humana)
-- ============================================================================

CREATE TABLE proposal (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  strategy_id    text NOT NULL REFERENCES strategy(strategy_id),
  llm_call_id    uuid NOT NULL REFERENCES llm_call(id),
  current_params jsonb NOT NULL,
  proposed_params jsonb NOT NULL,
  rationale      text NOT NULL,
  backtest_result jsonb,            -- el diff, backtesteado
  hard_bounds    jsonb NOT NULL,    -- los límites que restringían el rango propuesto
  status         text NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|expired
  reviewed_at    timestamptz,
  reviewed_by    text,
  reject_reason  text,              -- ← el dataset más valioso que vas a generar
  expires_at     timestamptz NOT NULL DEFAULT now() + INTERVAL '7 days'
);
CREATE INDEX proposal_pending ON proposal (created_at) WHERE status = 'pending';
-- Límite de propuestas por semana: se aplica en código, contra este índice.
-- Una propuesta no revisada EXPIRA; no se acumula. Mitiga la fatiga de aprobación.

ALTER TABLE strategy_params ADD CONSTRAINT strategy_params_proposal_fk
  FOREIGN KEY (proposal_id) REFERENCES proposal(id);

-- ============================================================================
-- 6 · VISTAS DE ATRIBUCIÓN  (lo que alimenta el dashboard)
-- ============================================================================

-- P&L neto por estrategia y día. Toda cifra es NETA o no se muestra.
--
-- CONVENCIONES DE SIGNO (explícitas, porque equivocarse aquí hace que el
-- dashboard mienta en la dirección favorable, que es la peor de las dos):
--   fees            >= 0  siempre. Es un coste; se RESTA.
--   funding_payment  con signo: positivo = cobrado, negativo = pagado. Se SUMA.
--   slippage        >= 0  siempre. Es un coste; se RESTA.
--                   Se define como el coste de ejecución adverso:
--                     compra : (price - decision_price) * qty   si price > dp
--                     venta  : (decision_price - price) * qty   si dp > price
--                   Una ejecución MEJOR que el precio de decisión da un valor
--                   negativo, que restado mejora el neto. Es correcto: es
--                   slippage favorable, y hay que poder verlo.
CREATE VIEW pnl_by_strategy_day AS
WITH f AS (
  SELECT strategy_id,
         date_trunc('day', ts) AS day,
         SUM(COALESCE(realized_pnl,0))                              AS gross_pnl,
         SUM(commission)                                             AS fees,
         SUM(funding_payment)                                        AS funding,
         SUM(CASE
               WHEN decision_price IS NULL OR qty = 0 THEN 0
               WHEN side LIKE 'buy%'  THEN (price - decision_price) * qty
               ELSE                        (decision_price - price) * qty
             END)                                                    AS slippage,
         COUNT(*) FILTER (WHERE qty <> 0)                            AS n_fills
  FROM fills GROUP BY 1,2
), ai AS (
  -- Una misma llm_call produce VARIAS filas en `decision` (arms baseline /
  -- llm / shadow, y varios símbolos por llamada). Atribuir su coste íntegro a
  -- cada fila lo contaría N veces. Se reparte a partes iguales entre las
  -- decisiones que la usaron, vía el conteo por llamada.
  SELECT strategy_id, day, SUM(share) AS ai_cost
  FROM (
    SELECT d.strategy_id,
           date_trunc('day', l.created_at) AS day,
           l.cost_usd / COUNT(*) OVER (PARTITION BY l.id) AS share
    FROM llm_call l
    JOIN decision d ON d.llm_call_id = l.id
  ) x
  GROUP BY 1,2
)
SELECT f.strategy_id, f.day,
       f.gross_pnl, f.fees, f.funding, f.slippage,
       COALESCE(ai.ai_cost,0) AS ai_cost,
       f.gross_pnl - f.fees + f.funding - f.slippage                       AS net_pnl,
       f.gross_pnl - f.fees + f.funding - f.slippage - COALESCE(ai.ai_cost,0) AS net_pnl_after_ai,
       f.n_fills
FROM f LEFT JOIN ai ON ai.strategy_id = f.strategy_id AND ai.day = f.day;

-- Presupuesto de coste fijo contra el límite de risk_floor.
CREATE VIEW cost_budget_month AS
SELECT date_trunc('month', created_at) AS month,
       SUM(cost_usd) AS llm_cost_usd,
       COUNT(*)      AS n_calls
FROM llm_call GROUP BY 1 ORDER BY 1 DESC;
