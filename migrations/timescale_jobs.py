"""Utilidades compartidas por las migraciones que crean políticas de TimescaleDB.

Borrar una hipertabla o un agregado continuo mientras uno de sus jobs se ejecuta
en segundo plano falla con "tuple concurrently deleted". Antes de cualquier DROP
hay que desprogramar los jobs, esperar a que ninguno esté en marcha y borrarlos.
Se llama dentro de `op.get_context().autocommit_block()`: el scheduler tiene que
ver la desprogramación.
"""

from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op

IDLE_TIMEOUT_SECONDS = 60


def stop_jobs(where_sql: str, params: dict[str, object]) -> None:
    """Desprograma, espera y borra los jobs que cumplen `where_sql`.

    `where_sql` filtra `timescaledb_information.jobs`; los valores van en `params`.
    """
    bind = op.get_bind()
    ids = list(bind.execute(
        sa.text(f"SELECT job_id FROM timescaledb_information.jobs WHERE {where_sql}"),  # noqa: S608
        params,
    ).scalars())
    for job_id in ids:
        bind.execute(sa.text("SELECT alter_job(:id, scheduled => false)"), {"id": job_id})
    running = sa.text(
        "SELECT count(*) FROM timescaledb_information.job_stats"
        " WHERE job_id = ANY(:ids) AND job_status = 'Running'"
    )
    deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS
    while ids and bind.execute(running, {"ids": ids}).scalar_one():
        if time.monotonic() > deadline:
            raise RuntimeError(f"los jobs {ids} siguen en marcha tras {IDLE_TIMEOUT_SECONDS} s")
        time.sleep(0.2)
    for job_id in ids:
        bind.execute(sa.text("SELECT delete_job(:id)"), {"id": job_id})
