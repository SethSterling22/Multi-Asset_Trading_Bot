"""Fixtures compartidas.

Los tests marcados `db` necesitan Postgres + TimescaleDB. `TEST_DATABASE_URL`
apunta a un servidor donde el usuario puede crear bases de datos; cada sesión de
tests crea una base desechable, le aplica las migraciones de verdad y la borra
al terminar. Sin la variable, los tests `db` FALLAN en lugar de saltarse: un
gate que pasa porque no se ejecutó nada no es un gate.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

# Import diferido: CI instala solo el grupo `dev` (sin alembic) para los guards,
# y pytest importa este fichero al recoger CUALQUIER test.
if TYPE_CHECKING:
    from sqlalchemy import URL, Connection, Engine


def _admin_url() -> URL:
    from sqlalchemy import make_url

    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL no está definida; los tests `db` necesitan Postgres + "
            "TimescaleDB (ver RUNNING.md). Para excluirlos: pytest -m 'not db'.",
            pytrace=False,
        )
    return make_url(raw)


def alembic(url: URL, *args: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        getattr(command, args[0])(Config("alembic.ini"), *args[1:])
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@contextmanager
def throwaway_database() -> Iterator[URL]:
    from sqlalchemy import create_engine, text

    admin_url = _admin_url()
    name = f"ledger_test_{uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin_url.set(database=name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    from sqlalchemy import create_engine

    with throwaway_database() as url:
        alembic(url, "upgrade", "head")
        engine = create_engine(url)
        yield engine
        engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Connection]:
    """Una conexión dentro de una transacción que se deshace al terminar el test."""
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()
