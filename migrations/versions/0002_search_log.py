"""Registro de N: search_log (docs/schema_reference.sql, sección 4).

N es el número de configuraciones evaluadas, incluidas las descartadas. Sin N el
Deflated Sharpe Ratio es incalculable y ningún backtest contiene información
(CLAUDE.md §1.11).

Desviaciones respecto al schema de referencia (DECISIONS.md D-008):
- Append-only por trigger: borrar o editar una fila desinflaría N, que es justo
  el sesgo que esta tabla existe para medir. Por eso `discarded` se fija al
  insertar y no se actualiza después.
- UNIQUE (kind, strategy_id, params_hash): re-ejecutar la misma configuración no
  cuenta dos veces. El hash incluye la ventana de datos, así que probar otra
  ventana SÍ es otra configuración.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE search_log (
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  kind          text NOT NULL CHECK (kind IN ('backtest_config', 'hypothesis_proposed')),
  strategy_id   text REFERENCES strategy(strategy_id),
  params_hash   bytea NOT NULL,
  params        jsonb NOT NULL,
  outcome       jsonb,             -- métricas in-sample, si aplica
  discarded     boolean NOT NULL DEFAULT false,
  UNIQUE NULLS NOT DISTINCT (kind, strategy_id, params_hash)
);
CREATE INDEX search_log_strategy ON search_log (kind, strategy_id);

CREATE TRIGGER search_log_append_only BEFORE UPDATE OR DELETE ON search_log
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER search_log_no_truncate BEFORE TRUNCATE ON search_log
  FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
""")


def downgrade() -> None:
    op.execute("DROP TABLE search_log")
