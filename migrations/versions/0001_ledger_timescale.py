"""Ledger y series temporales (docs/schema_reference.sql, secciones 0-2).

Desviaciones deliberadas respecto al schema de referencia (DECISIONS.md D-004):
- `fills.venue_fill_id` es NOT NULL: con NULL la UNIQUE no aplica y la doble
  inserción duplicaría. Los fills sin id de venue (funding, paper) llevan uno
  determinista.
- `fills.multiplier`: sin él, el coste de una opción (x100) o de un perp con
  tamaño de contrato sale mal en la vista `positions`.
- `fills.realized_pnl` no existe: el P&L realizado depende del orden de los
  fills y se deriva de ellos (core/ledger/positions.py), igual que la posición.
- CHECKs de `side`, `qty`, `price` y `commission`: un `side` mal escrito
  ('BUY') contaría como venta en la vista `positions`.
- `fills` y `risk_events` son append-only por trigger, no por convención.

Las FK de `orders.decision_id` y `fills.decision_id` se añaden en la migración
que cree `decision` (sección 3), igual que en el schema de referencia.

Revision ID: 0001
Revises:
Create Date: 2026-11-01
"""

import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIDES = "('buy','sell','buy_to_open','buy_to_close','sell_to_open','sell_to_close')"
BUY_SIDES = "('buy','buy_to_open','buy_to_close')"

# ---------------------------------------------------------------------------
# 0 · TIPOS
# ---------------------------------------------------------------------------
TYPES = """
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TYPE asset_type AS ENUM ('stock','option','crypto_spot','crypto_perp','future');
CREATE TYPE order_status AS ENUM (
  'PENDING_NEW',   -- write-ahead intent: escrito ANTES del POST al exchange
  'NEW','PARTIALLY_FILLED','FILLED','CANCELED','REJECTED','EXPIRED','UNKNOWN'
);
"""

# ---------------------------------------------------------------------------
# 1 · LEDGER  (la verdad del sistema; append-only)
# ---------------------------------------------------------------------------
LEDGER = f"""
CREATE TABLE strategy (
  strategy_id   text PRIMARY KEY,
  display_name  text NOT NULL,
  enabled       boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);
INSERT INTO strategy (strategy_id, display_name) VALUES
  ('wheel',            'Wheel'),
  ('crypto_rebalance', 'Crypto rebalance'),
  ('funding_carry',    'Funding carry'),
  ('xsec_momentum',    'Cross-sectional momentum'),
  ('trend',            'Trend');

-- Parámetros versionados. Nunca se edita una versión: se inserta otra y se
-- cierra la anterior con active_to. La FK de proposal_id llega con `proposal`.
CREATE TABLE strategy_params (
  id            bigserial PRIMARY KEY,
  strategy_id   text NOT NULL REFERENCES strategy(strategy_id),
  version       integer NOT NULL,
  params        jsonb NOT NULL,
  approved_by   text NOT NULL,
  proposal_id   uuid,
  active_from   timestamptz NOT NULL DEFAULT now(),
  active_to     timestamptz,
  UNIQUE (strategy_id, version)
);
CREATE UNIQUE INDEX one_active_version_per_strategy
  ON strategy_params (strategy_id) WHERE active_to IS NULL;

CREATE TABLE orders (
  client_order_id  text PRIMARY KEY,        -- determinista: {{strat}}-{{sym}}-{{bar}}-{{n}}
  ts_intent        timestamptz NOT NULL DEFAULT now(),
  ts_ack           timestamptz,
  venue            text NOT NULL,
  symbol           text NOT NULL,
  asset_type       asset_type NOT NULL,
  side             text NOT NULL CHECK (side IN {SIDES}),
  qty              numeric NOT NULL CHECK (qty > 0),
  order_type       text NOT NULL,
  limit_price      numeric,
  reduce_only      boolean NOT NULL DEFAULT false,
  status           order_status NOT NULL DEFAULT 'PENDING_NEW',
  venue_order_id   text,
  reject_reason    text,
  strategy_id      text NOT NULL REFERENCES strategy(strategy_id),
  decision_id      uuid,
  decision_price   numeric,
  risk_floor_hash  bytea NOT NULL,
  attempt          integer NOT NULL DEFAULT 1
);
CREATE INDEX orders_open ON orders (venue, status)
  WHERE status IN ('PENDING_NEW','NEW','PARTIALLY_FILLED');

-- El ledger de fills. APPEND-ONLY (trigger más abajo).
CREATE TABLE fills (
  id               bigserial PRIMARY KEY,
  ts               timestamptz NOT NULL,
  venue            text NOT NULL,
  symbol           text NOT NULL,
  asset_type       asset_type NOT NULL,
  side             text NOT NULL CHECK (side IN {SIDES}),
  qty              numeric NOT NULL CHECK (qty >= 0),   -- 0: filas de funding
  price            numeric NOT NULL CHECK (price >= 0),
  multiplier       numeric NOT NULL DEFAULT 1 CHECK (multiplier > 0),
  commission       numeric NOT NULL DEFAULT 0 CHECK (commission >= 0),
  funding_payment  numeric NOT NULL DEFAULT 0,          -- con signo: + cobrado
  decision_price   numeric,
  strategy_id      text NOT NULL REFERENCES strategy(strategy_id),
  decision_id      uuid,
  client_order_id  text REFERENCES orders(client_order_id),
  venue_fill_id    text NOT NULL,
  UNIQUE (venue, venue_fill_id)             -- idempotencia del gap-fill
);
CREATE INDEX fills_strategy_ts ON fills (strategy_id, ts DESC);
CREATE INDEX fills_symbol_ts   ON fills (symbol, ts DESC);

-- La posición es una VISTA. Nunca una tabla, nunca memoria de proceso.
CREATE VIEW positions AS
SELECT venue, symbol, asset_type, strategy_id,
       SUM(CASE WHEN side IN {BUY_SIDES} THEN qty ELSE -qty END) AS net_qty,
       SUM(CASE WHEN side IN {BUY_SIDES} THEN qty*price*multiplier
                ELSE -qty*price*multiplier END)                  AS net_cost,
       SUM(commission)      AS total_commission,
       SUM(funding_payment) AS total_funding,
       MAX(ts)              AS last_fill_ts
FROM fills
GROUP BY venue, symbol, asset_type, strategy_id
HAVING ABS(SUM(CASE WHEN side IN {BUY_SIDES} THEN qty ELSE -qty END)) > 1e-12;

CREATE TABLE risk_events (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  kind         text NOT NULL,
  scope        text NOT NULL,
  reason       text NOT NULL,
  actor        text NOT NULL,
  metadata     jsonb NOT NULL DEFAULT '{{}}'
);
CREATE INDEX risk_events_recent ON risk_events (ts DESC);

CREATE TABLE reconciliation_runs (
  id             bigserial PRIMARY KEY,
  ts             timestamptz NOT NULL DEFAULT now(),
  venue          text NOT NULL,
  mismatches     integer NOT NULL,
  detail         jsonb NOT NULL,
  action_taken   text NOT NULL
);

-- Append-only impuesto por la base de datos: un UPDATE o DELETE sobre el ledger
-- es siempre un bug, y debe fallar ruidosamente.
CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% es append-only: % rechazado', TG_TABLE_NAME, TG_OP;
END;
$$;
CREATE TRIGGER fills_append_only BEFORE UPDATE OR DELETE ON fills
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER fills_no_truncate BEFORE TRUNCATE ON fills
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER risk_events_append_only BEFORE UPDATE OR DELETE ON risk_events
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER risk_events_no_truncate BEFORE TRUNCATE ON risk_events
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
"""

# ---------------------------------------------------------------------------
# 2 · TIMESCALE  (series temporales de mercado)
# ---------------------------------------------------------------------------
TIMESCALE = """
CREATE TABLE trades (
  ts       timestamptz      NOT NULL,
  venue    text             NOT NULL,
  symbol   text             NOT NULL,
  trade_id text             NOT NULL,
  price    double precision NOT NULL,
  amount   double precision NOT NULL,
  side     smallint         NOT NULL,     -- 1 buy, -1 sell
  PRIMARY KEY (venue, symbol, ts, trade_id)
);
SELECT create_hypertable('trades','ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE trades SET (
  timescaledb.compress           = true,
  timescaledb.compress_segmentby = 'venue, symbol',
  timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('trades', INTERVAL '7 days');
SELECT add_retention_policy ('trades', INTERVAL '400 days');

-- Top-of-book DERIVADO a 1s. No se almacenan deltas L2 crudos.
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

-- Tape propio. Irreemplazable: SIN retention policy.
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
"""

# Los agregados continuos no pueden crearse dentro de una transacción.
CONTINUOUS_AGGREGATES = [
    """
CREATE MATERIALIZED VIEW ohlcv_1m WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket, venue, symbol,
       first(price,ts) AS open, max(price) AS high, min(price) AS low,
       last(price,ts)  AS close, sum(amount) AS volume,
       sum(CASE WHEN side = 1 THEN amount ELSE 0 END) AS buy_volume,
       count(*) AS n_trades
FROM trades GROUP BY 1,2,3 WITH NO DATA
""",
    """
SELECT add_continuous_aggregate_policy('ohlcv_1m',
  start_offset      => INTERVAL '3 hours',   -- re-materializa: cubre los gap-fills
  end_offset        => INTERVAL '1 minute',  -- no toca el bucket en curso
  schedule_interval => INTERVAL '1 minute')
""",
    """
CREATE MATERIALIZED VIEW ohlcv_1h WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', bucket) AS bucket, venue, symbol,
       first(open,bucket) AS open, max(high) AS high, min(low) AS low,
       last(close,bucket) AS close, sum(volume) AS volume, sum(n_trades) AS n_trades
FROM ohlcv_1m GROUP BY 1,2,3 WITH NO DATA
""",
    """
SELECT add_continuous_aggregate_policy('ohlcv_1h',
  start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour')
""",
]


def upgrade() -> None:
    op.execute(TYPES)
    op.execute(LEDGER)
    op.execute(TIMESCALE)
    with op.get_context().autocommit_block():
        for statement in CONTINUOUS_AGGREGATES:
            op.execute(statement)


# Los jobs que crea esta migración (compresión, retención y refresco de agregados).
POLICY_PROCS = (
    "policy_compression", "policy_retention", "policy_refresh_continuous_aggregate",
)
JOBS_IDLE_TIMEOUT_SECONDS = 60


def _stop_policy_jobs() -> None:
    """Desprograma los jobs, espera a que ninguno esté en marcha y los borra.

    Sin esto, un DROP que coincide con un job ejecutándose en segundo plano falla con
    "tuple concurrently deleted" (1 de cada ~60 downgrades sobre una base recién
    creada). Va en autocommit: el scheduler tiene que ver la desprogramación.
    """
    bind = op.get_bind()
    ids = list(bind.execute(
        sa.text("SELECT job_id FROM timescaledb_information.jobs WHERE proc_name = ANY(:p)"),
        {"p": list(POLICY_PROCS)},
    ).scalars())
    for job_id in ids:
        bind.execute(sa.text("SELECT alter_job(:id, scheduled => false)"), {"id": job_id})
    deadline = time.monotonic() + JOBS_IDLE_TIMEOUT_SECONDS
    running = sa.text(
        "SELECT count(*) FROM timescaledb_information.job_stats"
        " WHERE job_id = ANY(:ids) AND job_status = 'Running'"
    )
    while bind.execute(running, {"ids": ids}).scalar_one():
        if time.monotonic() > deadline:
            raise RuntimeError(f"los jobs {ids} siguen en marcha tras "
                               f"{JOBS_IDLE_TIMEOUT_SECONDS} s; downgrade abortado")
        time.sleep(0.2)
    for job_id in ids:
        bind.execute(sa.text("SELECT delete_job(:id)"), {"id": job_id})


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _stop_policy_jobs()
        op.execute("DROP MATERIALIZED VIEW IF EXISTS ohlcv_1h")
        op.execute("DROP MATERIALIZED VIEW IF EXISTS ohlcv_1m")
    op.execute("""
DROP TABLE equity_curve, liquidations, open_interest, funding, book_1s, trades;
DROP VIEW positions;
DROP TABLE reconciliation_runs, risk_events, fills, orders, strategy_params, strategy;
DROP FUNCTION reject_mutation();
DROP TYPE order_status;
DROP TYPE asset_type;
""")
    # La extensión timescaledb se deja instalada: quitarla exige que ninguna otra
    # sesión la tenga cargada, y no contiene datos.
