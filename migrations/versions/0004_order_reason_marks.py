"""El motivo de cada orden y el precio de cada posición abierta en cada snapshot.

- `orders.reason`: por qué se decidió la orden (p.ej. "weight 19.10% < target 20.00%").
  Hasta ahora solo estaba en los logs; para el diario de operaciones y para comparar
  con un backtest tiene que estar junto a la orden.
- `position_marks`: cantidad y precio de marca de cada posición abierta, escritos con
  cada snapshot de equity. Con ellos el historial puede decir POR QUÉ cambió el no
  realizado (qué símbolo se movió y cuánto) y el diario, cuánto vale hoy una entrada.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
ALTER TABLE orders ADD COLUMN reason text;

CREATE TABLE position_marks (
  ts           timestamptz NOT NULL,
  strategy_id  text NOT NULL REFERENCES strategy(strategy_id),
  venue        text NOT NULL,
  symbol       text NOT NULL,
  qty          numeric NOT NULL,
  mark         numeric NOT NULL CHECK (mark >= 0),
  PRIMARY KEY (strategy_id, venue, symbol, ts)
);
CREATE INDEX position_marks_ts ON position_marks (ts DESC);
""")


def downgrade() -> None:
    op.execute("DROP TABLE position_marks")
    op.execute("ALTER TABLE orders DROP COLUMN reason")
