"""Diario manual y corridas de backtest guardadas (DECISIONS.md D-017).

1. El sleeve `manual`: operaciones hechas a mano en otra cuenta o broker, que el
   sistema solo registra (sin reconciliación: no hay venue que comparar).
2. El rol `manual_journal`, con el que escribe el formulario del dashboard. Puede
   INSERTAR en `orders` y `fills` solo filas de `strategy_id = 'manual'`: lo impone
   Postgres con row-level security, no el código del dashboard. No puede borrar ni
   actualizar nada (y `fills` es append-only para todos). Nace sin LOGIN: la
   contraseña se le pone en Sadida desde /etc/trading/pg_manual_password (RUNNING.md).
   El dueño de las tablas (el rol de la app) no se ve afectado por la RLS.
3. `backtest_runs`, `backtest_equity`, `backtest_fills`: cada configuración del
   barrido, con su curva y sus operaciones, para verlas en el dashboard junto a su N
   (las pruebas registradas en `search_log`) y su DSR.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "manual_journal"


def upgrade() -> None:
    op.execute("""
INSERT INTO strategy (strategy_id, display_name) VALUES ('manual', 'Manual')
ON CONFLICT (strategy_id) DO NOTHING;

CREATE TABLE backtest_runs (
  id              bigserial PRIMARY KEY,
  ts              timestamptz NOT NULL DEFAULT now(),
  strategy_id     text NOT NULL REFERENCES strategy(strategy_id),
  params_hash     text NOT NULL UNIQUE,      -- el de search_log, en hex
  params          jsonb NOT NULL,
  window_start    date NOT NULL,
  window_end      date NOT NULL,
  initial_capital numeric NOT NULL,
  final_equity    numeric NOT NULL,
  fees_usd        numeric NOT NULL,
  n_orders        integer NOT NULL,
  discarded       boolean NOT NULL,
  n_trials        integer NOT NULL,          -- N registrado cuando se guardó
  dsr             double precision,          -- NULL si está descartada
  is_candidate    boolean NOT NULL DEFAULT false,
  final_marks     jsonb NOT NULL DEFAULT '{}'  -- cierre del último día, por símbolo
);
CREATE TABLE backtest_equity (
  run_id  bigint NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
  day     date NOT NULL,
  equity  double precision NOT NULL,
  PRIMARY KEY (run_id, day)
);
CREATE TABLE backtest_fills (
  run_id      bigint NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
  seq         integer NOT NULL,
  ts          timestamptz NOT NULL,
  symbol      text NOT NULL,
  side        text NOT NULL CHECK (side IN ('buy', 'sell')),
  qty         numeric NOT NULL CHECK (qty > 0),
  price       numeric NOT NULL CHECK (price >= 0),
  commission  numeric NOT NULL CHECK (commission >= 0),
  reason      text,
  PRIMARY KEY (run_id, seq)
);
""")
    op.execute(f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
    CREATE ROLE {ROLE} NOLOGIN;
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database());
END $$;
GRANT USAGE ON SCHEMA public TO {ROLE};
GRANT SELECT ON strategy TO {ROLE};
GRANT SELECT, INSERT ON orders, fills TO {ROLE};
GRANT USAGE ON SEQUENCE fills_id_seq TO {ROLE};

ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE fills ENABLE ROW LEVEL SECURITY;
CREATE POLICY manual_insert ON orders FOR INSERT TO {ROLE} WITH CHECK (strategy_id = 'manual');
CREATE POLICY manual_select ON orders FOR SELECT TO {ROLE} USING (true);
CREATE POLICY manual_insert ON fills FOR INSERT TO {ROLE} WITH CHECK (strategy_id = 'manual');
CREATE POLICY manual_select ON fills FOR SELECT TO {ROLE} USING (true);
""")


def downgrade() -> None:
    op.execute(f"""
DROP POLICY IF EXISTS manual_select ON fills;
DROP POLICY IF EXISTS manual_insert ON fills;
DROP POLICY IF EXISTS manual_select ON orders;
DROP POLICY IF EXISTS manual_insert ON orders;
ALTER TABLE fills DISABLE ROW LEVEL SECURITY;
ALTER TABLE orders DISABLE ROW LEVEL SECURITY;
REVOKE ALL ON orders, fills, strategy FROM {ROLE};
REVOKE USAGE ON SEQUENCE fills_id_seq FROM {ROLE};
REVOKE USAGE ON SCHEMA public FROM {ROLE};
DO $$ BEGIN
  EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {ROLE}', current_database());
END $$;
DROP TABLE backtest_fills, backtest_equity, backtest_runs;
DELETE FROM strategy WHERE strategy_id = 'manual'
  AND NOT EXISTS (SELECT 1 FROM fills WHERE strategy_id = 'manual')
  AND NOT EXISTS (SELECT 1 FROM orders WHERE strategy_id = 'manual');
""")
