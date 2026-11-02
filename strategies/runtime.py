"""Arranque y parada del proceso de una estrategia.

El problema que resuelve: Lumibot atiende Ctrl+C (para la estrategia y `run_all`
vuelve), pero el hilo del WebSocket del broker no es daemon y no termina nunca,
así que Python se queda esperándolo al salir y el proceso no muere. Y Lumibot no
atiende SIGTERM, que es lo que envían `pkill` y `docker stop`.

`run_process`:
- Cuando `run` vuelve, vacía los logs y sale con os._exit, sin esperar a hilos
  sueltos. Es seguro: cada escritura del ledger ya está confirmada en su propia
  transacción, y lo que no llegó a escribirse no está a medias.
- SIGTERM pide la misma parada ordenada que Ctrl+C (`stop`).
- Si la parada ordenada no termina en `grace_seconds`, sale a la fuerza. Una
  segunda señal fuerza la salida en el momento.

No cierra posiciones: parar el proceso no es liquidar. Los stops protectores
viven en el exchange (CLAUDE.md §1.5).
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any, NoReturn

from core import log as core_log

DEFAULT_GRACE_SECONDS = 30.0


def _flush() -> None:
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()


def run_process(
    run: Callable[[], None],
    stop: Callable[[], None],
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    exit_process: Callable[[int], NoReturn] = os._exit,
    logger: Any | None = None,
) -> NoReturn:
    """Ejecuta `run` y termina el proceso cuando vuelve, pase lo que pase con los hilos."""
    if grace_seconds <= 0:
        raise ValueError("grace_seconds > 0")
    log = logger if logger is not None else core_log.get_logger()
    stopping = threading.Event()

    def force(reason: str) -> None:
        log.error("shutdown_forced", action="exit", reason=reason)
        _flush()
        exit_process(1)

    def on_sigterm(signum: int, frame: FrameType | None) -> None:
        if stopping.is_set():
            force("segunda señal durante la parada")
        stopping.set()
        log.warning("shutdown_requested", action="stop", reason=signal.Signals(signum).name)
        timer = threading.Timer(
            grace_seconds, force, args=(f"la parada no terminó en {grace_seconds:g} s",)
        )
        timer.daemon = True
        timer.start()
        stop()

    signal.signal(signal.SIGTERM, on_sigterm)

    code = 0
    try:
        run()
    except Exception:
        log.exception("strategy_crashed", action="exit", reason="exception")
        code = 1
    log.info("process_exit", action="exit", reason=f"code {code}")
    _flush()
    exit_process(code)
