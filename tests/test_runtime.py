"""strategies/runtime.py: el proceso termina aunque quede un hilo no-daemon vivo.

Cada test lanza un subproceso con un hilo que no termina nunca, como el del
WebSocket de Alpaca, y comprueba que el proceso muere y con qué código.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = """
import threading, time, sys
stop_event = threading.Event()
# El hilo del WebSocket: no es daemon y no termina nunca.
threading.Thread(target=lambda: time.sleep(10**6), daemon=False).start()
"""


def spawn(body: str) -> subprocess.Popen[str]:
    code = textwrap.dedent(PRELUDE) + textwrap.dedent(body)
    # El código es un literal de este fichero, no input externo.
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, text=True,  # noqa: S603
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONPATH": str(ROOT)})
    time.sleep(0.5)   # que arranque e instale los manejadores
    return proc


def wait(proc: subprocess.Popen[str], timeout: float) -> int | None:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None


def test_control_without_run_process_the_process_hangs() -> None:
    # Reproduce el bug: `run` vuelve, pero el proceso no muere.
    proc = spawn("print('run terminado', flush=True)\n")
    assert wait(proc, timeout=3) is None


def test_exits_when_run_returns_despite_the_thread() -> None:
    proc = spawn("""
        from strategies.runtime import run_process
        run_process(lambda: None, stop_event.set)
    """)
    assert wait(proc, timeout=5) == 0


def test_sigterm_requests_an_orderly_stop() -> None:
    proc = spawn("""
        from strategies.runtime import run_process
        run_process(stop_event.wait, stop_event.set, grace_seconds=30)
    """)
    proc.send_signal(signal.SIGTERM)
    assert wait(proc, timeout=5) == 0
    assert proc.stdout is not None and "shutdown_requested" in proc.stdout.read()


def test_a_stop_that_hangs_is_forced_after_the_grace_period() -> None:
    proc = spawn("""
        from strategies.runtime import run_process
        run_process(lambda: time.sleep(10**6), lambda: None, grace_seconds=1)
    """)
    proc.send_signal(signal.SIGTERM)
    assert wait(proc, timeout=6) == 1
    assert proc.stdout is not None and "shutdown_forced" in proc.stdout.read()


def test_a_second_signal_forces_immediately() -> None:
    proc = spawn("""
        from strategies.runtime import run_process
        run_process(lambda: time.sleep(10**6), lambda: None, grace_seconds=60)
    """)
    proc.send_signal(signal.SIGTERM)
    time.sleep(0.3)
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    assert wait(proc, timeout=5) == 1
    assert time.monotonic() - started < 3


def test_a_crash_exits_with_code_1() -> None:
    proc = spawn("""
        from strategies.runtime import run_process
        def boom():
            raise RuntimeError("broker caído")
        run_process(boom, stop_event.set)
    """)
    assert wait(proc, timeout=5) == 1
    assert proc.stdout is not None and "strategy_crashed" in proc.stdout.read()


@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_grace_period_must_be_positive(seconds: float) -> None:
    from strategies.runtime import run_process
    with pytest.raises(ValueError):
        run_process(lambda: None, lambda: None, grace_seconds=seconds)
