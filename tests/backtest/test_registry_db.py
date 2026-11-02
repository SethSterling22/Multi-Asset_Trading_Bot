"""search_log en Postgres: append-only y sin dobles conteos.

En su propia base desechable: search_log no admite DELETE, así que no se puede
limpiar después, y desactivar los triggers para limpiar contaminaría la conexión
compartida del pool para los tests siguientes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from backtest.registry import PostgresSearchLog
from tests.conftest import alembic, throwaway_database

pytestmark = pytest.mark.db


def test_search_log_counts_distinct_configs_and_is_append_only() -> None:
    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        engine = create_engine(url)
        try:
            log = PostgresSearchLog(engine)
            h = log.record("crypto_rebalance", {"t": 0.05}, {"sharpe_per_period": 0.03}, False)
            log.record("crypto_rebalance", {"t": 0.05}, {"sharpe_per_period": 0.03}, False)
            log.record("crypto_rebalance", {"t": 0.10}, None, True)
            trials = log.trials("crypto_rebalance")
            assert len(trials) == 2
            assert h in {t.params_hash for t in trials}
            assert sorted(t.sharpe is None for t in trials) == [False, True]
            for statement in ("DELETE FROM search_log", "UPDATE search_log SET discarded = true",
                              "TRUNCATE search_log"):
                with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
                    conn.execute(text(statement))
            assert len(log.trials("crypto_rebalance")) == 2
        finally:
            engine.dispose()
