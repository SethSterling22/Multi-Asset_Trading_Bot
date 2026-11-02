from __future__ import annotations

import pytest

from tests.guards.helpers import Events


@pytest.fixture(autouse=True)
def events() -> Events:
    """Log propio por test: otro test puede dejar structlog apuntando a un stream cerrado."""
    return Events()
