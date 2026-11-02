"""Archivo de métricas de derivados de fuentes externas (Coinalyze) y su cobertura.

Las tablas `funding` y `liquidations` del schema de referencia esperan eventos
individuales (cada pago de funding, cada liquidación). Coinalyze da BARRAS
agregadas por intervalo: OHLC de open interest y de funding, suma de liquidaciones
largas y cortas, ratio long/short. Se guardan crudas en su propia tabla en vez de
forzarlas en un formato que no es el suyo (DECISIONS.md D-009).

Es el tier T2 del informe (§12.5): irreemplazable, nadie lo vende retroactivamente,
y el intradía de Coinalyze se borra a los ~1,500–2,000 puntos. SIN retention policy.

`ingest_runs` registra qué ventana cubrió cada ejecución. Hace falta porque las
liquidaciones son DISPERSAS: un minuto sin liquidaciones no aparece en la API, así
que su ausencia no es un hueco. Un hueco es tiempo que ninguna ejecución cubrió.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

from migrations.timescale_jobs import stop_jobs

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

METRICS = "('open_interest', 'funding_rate', 'liquidations', 'long_short_ratio')"
INTERVALS = (
    "('1min', '5min', '15min', '30min', '1hour', '2hour', '4hour', '6hour', '12hour', 'daily')"
)


def upgrade() -> None:
    op.execute(f"""
CREATE TABLE derivatives_metrics (
  ts          timestamptz NOT NULL,     -- inicio de la barra, UTC
  source      text NOT NULL,            -- 'coinalyze'
  symbol      text NOT NULL,            -- símbolo de la fuente, p.ej. BTCUSDT_PERP.A
  exchange    text NOT NULL,            -- 'binance', 'bybit', …
  metric      text NOT NULL CHECK (metric IN {METRICS}),
  interval    text NOT NULL CHECK (interval IN {INTERVALS}),
  unit        text NOT NULL,            -- 'usd' | 'percent' | 'ratio'
  open        double precision,         -- OHLC: open_interest, funding_rate
  high        double precision,
  low         double precision,
  close       double precision,
  long_value  double precision,         -- liquidaciones de largos / % de cuentas largas
  short_value double precision,         -- liquidaciones de cortos / % de cuentas cortas
  ratio       double precision,         -- long_short_ratio
  PRIMARY KEY (source, symbol, metric, interval, ts)
);
SELECT create_hypertable('derivatives_metrics', 'ts', chunk_time_interval => INTERVAL '7 days');
ALTER TABLE derivatives_metrics SET (
  timescaledb.compress           = true,
  timescaledb.compress_segmentby = 'source, symbol, metric, interval',
  timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('derivatives_metrics', INTERVAL '7 days');
-- SIN retention policy: este dataset es irreemplazable.

CREATE TABLE ingest_runs (
  id            bigserial PRIMARY KEY,
  run_at        timestamptz NOT NULL DEFAULT now(),
  source        text NOT NULL,
  symbol        text NOT NULL,
  metric        text NOT NULL CHECK (metric IN {METRICS}),
  interval      text NOT NULL CHECK (interval IN {INTERVALS}),
  covered_from  timestamptz NOT NULL,   -- inclusive
  covered_to    timestamptz NOT NULL,   -- exclusivo: fin de la última barra completa
  rows_received integer NOT NULL CHECK (rows_received >= 0),
  rows_inserted integer NOT NULL CHECK (rows_inserted >= 0),
  CHECK (covered_to >= covered_from)
);
CREATE INDEX ingest_runs_series ON ingest_runs (source, symbol, metric, interval, covered_to DESC);
CREATE TRIGGER ingest_runs_append_only BEFORE UPDATE OR DELETE ON ingest_runs
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
""")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        # El job de compresión no puede estar en marcha mientras se borra la hipertabla.
        stop_jobs("hypertable_name = :t", {"t": "derivatives_metrics"})
    op.execute("DROP TABLE ingest_runs, derivatives_metrics")
