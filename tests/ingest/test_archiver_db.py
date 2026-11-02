"""El archivador contra Postgres: idempotencia, barras completas, huecos y caos."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from data.ingest.coinalyze import (
    Series,
    archive,
    coverage_gaps,
    sequence_holes,
)
from data.sources.coinalyze import INTERVAL_SECONDS, INTRADAY_RETAINED_POINTS

pytestmark = pytest.mark.db

T0 = 1_793_500_000 - 1_793_500_000 % 3600          # en punto, para que las cuentas sean limpias


class FakeCoinalyze:
    """Coinalyze simulado: conserva INTRADAY_RETAINED_POINTS barras y las liquidaciones
    solo aparecen en los minutos múltiplos de 7 (dispersas)."""

    def __init__(self, clock: list[int], fail_on: set[str] | None = None) -> None:
        self.clock = clock
        self.fail_on = fail_on or set()
        self.calls = 0

    def history(self, metric: str, symbols: list[str], interval: str, start: int,
                end: int, *, usd: bool = True) -> dict[str, list[dict[str, Any]]]:
        self.calls += 1
        self.last_usd = usd
        if metric in self.fail_on:
            raise ConnectionError("el proceso muere aquí")
        step = INTERVAL_SECONDS[interval]
        now = self.clock[0]
        oldest = now - INTRADAY_RETAINED_POINTS * step
        out = {}
        for s in symbols:
            bars = []
            t = max(start, oldest) - max(start, oldest) % step
            while t <= min(end, now):                       # incluye la barra en curso
                if metric != "liquidations" or (t // step) % 7 == 0:
                    if metric == "liquidations":
                        bars.append({"t": t, "l": 1.0, "s": 2.0})
                    else:
                        bars.append({"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5})
                t += step
            out[s] = bars
        return out


def series(metric: str = "open_interest") -> Series:
    return Series(f"TEST{uuid4().hex[:8]}.A", "test", metric, "1min")


def rows(engine: Engine, s: Series) -> list[datetime]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT ts FROM derivatives_metrics WHERE symbol = :s ORDER BY ts"), {"s": s.symbol})]


def at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_only_complete_bars_and_rerun_is_idempotent(db_engine: Engine) -> None:
    s = series()
    clock = [T0 + 30]                                      # a mitad de un minuto
    fake = FakeCoinalyze(clock)
    first = archive(db_engine, fake, [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    ts = rows(db_engine, s)
    assert ts[-1] == at(T0 - 60)                            # la barra de T0 está en curso
    # La ventana retenida empieza a mitad de un minuto (now − 1500·60 = …:30): esa barra
    # parcial queda fuera, y lo que entra son las barras completas que caben enteras.
    oldest = clock[0] - INTRADAY_RETAINED_POINTS * 60
    expected = (T0 - 60 - (oldest - oldest % 60 + 60)) // 60 + 1
    assert len(ts) == first[0].inserted == expected == INTRADAY_RETAINED_POINTS - 1
    again = archive(db_engine, fake, [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    assert again == [] or again[0].inserted == 0
    assert rows(db_engine, s) == ts


def test_incremental_runs_leave_no_gaps(db_engine: Engine) -> None:
    s = series()
    clock = [T0]
    fake = FakeCoinalyze(clock)
    for hours in range(0, 7):
        clock[0] = T0 + hours * 3600
        archive(db_engine, fake, [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    since, until = at(T0 - 3600), at(T0 + 6 * 3600)
    with db_engine.connect() as conn:
        assert coverage_gaps(conn, s, since, until) == []
        assert sequence_holes(conn, s, since, until) == []


def test_sparse_liquidations_are_not_holes_but_coverage_is_tracked(db_engine: Engine) -> None:
    s = series("liquidations")
    clock = [T0]
    archive(db_engine, FakeCoinalyze(clock), [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    since, until = at(T0 - 3600), at(T0)
    with db_engine.connect() as conn:
        assert coverage_gaps(conn, s, since, until) == []
        with pytest.raises(ValueError, match="dispersa"):
            sequence_holes(conn, s, since, until)
    assert 0 < len(rows(db_engine, s)) < INTRADAY_RETAINED_POINTS


def test_chaos_a_run_that_dies_midway_is_filled_by_the_next(db_engine: Engine) -> None:
    """Gate de la Sesión 3: matar el ingester y que se recupere con gap-fill correcto."""
    oi, liq = series("open_interest"), series("liquidations")
    clock = [T0]
    archive(db_engine, FakeCoinalyze(clock), [oi, liq], clock=lambda: clock[0])  # type: ignore[arg-type]
    clock[0] = T0 + 3 * 3600
    with pytest.raises(ConnectionError):                    # muere en el segundo grupo
        archive(db_engine, FakeCoinalyze(clock, fail_on={"liquidations"}), [oi, liq],
                clock=lambda: clock[0])  # type: ignore[arg-type]
    with db_engine.connect() as conn:
        assert coverage_gaps(conn, liq, at(T0), at(T0 + 3 * 3600)) == [
            (at(T0), at(T0 + 3 * 3600))]                     # lo que no se llegó a escribir
    clock[0] = T0 + 5 * 3600                                 # reinicio dos horas después
    archive(db_engine, FakeCoinalyze(clock), [oi, liq], clock=lambda: clock[0])  # type: ignore[arg-type]
    with db_engine.connect() as conn:
        for s in (oi, liq):
            assert coverage_gaps(conn, s, at(T0 - 3600), at(T0 + 5 * 3600)) == []
        assert sequence_holes(conn, oi, at(T0 - 3600), at(T0 + 5 * 3600)) == []


def test_a_restart_after_retention_reports_the_lost_window(db_engine: Engine) -> None:
    # Si el archivador está parado más de lo que Coinalyze conserva (~25 h a 1 min),
    # ese tiempo se ha perdido y el informe lo dice; no se rellena con nada.
    s = series()
    clock = [T0]
    archive(db_engine, FakeCoinalyze(clock), [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    clock[0] = T0 + 30 * 3600
    archive(db_engine, FakeCoinalyze(clock), [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    lost_from, lost_to = at(T0), at(clock[0] - INTRADAY_RETAINED_POINTS * 60)
    with db_engine.connect() as conn:
        assert coverage_gaps(conn, s, at(T0 - 60), at(clock[0] - 60)) == [(lost_from, lost_to)]


def test_a_missing_bar_inside_covered_time_is_a_hole(db_engine: Engine) -> None:
    s = series()
    clock = [T0]

    class Holey(FakeCoinalyze):
        def history(self, *a: Any, **k: Any) -> dict[str, list[dict[str, Any]]]:
            out = super().history(*a, **k)
            return {sym: [b for b in bars if b["t"] != T0 - 600] for sym, bars in out.items()}

    archive(db_engine, Holey(clock), [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    with db_engine.connect() as conn:
        assert sequence_holes(conn, s, at(T0 - 3600), at(T0)) == [at(T0 - 600)]


def test_coverage_log_is_append_only(db_engine: Engine) -> None:
    # Un trigger de fila solo salta si hay una fila: el test crea la suya y no depende
    # de lo que hayan dejado otros tests.
    from sqlalchemy.exc import DBAPIError
    s = series()
    clock = [T0]
    archive(db_engine, FakeCoinalyze(clock), [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    for statement in ("DELETE FROM ingest_runs WHERE symbol = :s",
                      "UPDATE ingest_runs SET rows_inserted = 0 WHERE symbol = :s"):
        with db_engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(statement), {"s": s.symbol})


def test_daily_native_series_ask_for_native_units(db_engine: Engine) -> None:
    s = Series(f"TEST{uuid4().hex[:8]}.A", "test", "open_interest", "daily")
    clock = [T0]
    fake = FakeCoinalyze(clock)
    archive(db_engine, fake, [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    assert fake.last_usd is False
    with db_engine.connect() as conn:
        units = {r[0] for r in conn.execute(text(
            "SELECT DISTINCT unit FROM derivatives_metrics WHERE symbol = :s"), {"s": s.symbol})}
    assert units == {"base_asset"}


def test_daily_oi_and_liquidations_are_requested_in_native_units() -> None:
    from data.ingest.coinalyze import in_usd, unit
    assert not in_usd("open_interest", "daily") and unit("open_interest", "daily") == "base_asset"
    assert not in_usd("liquidations", "daily")
    assert in_usd("open_interest", "1min") and unit("open_interest", "1min") == "usd"
    assert in_usd("funding_rate", "daily") and unit("funding_rate", "daily") == "percent"


def test_report_does_not_count_time_before_the_first_run(
    db_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    # Una serie que empezó a archivarse hace 2 h no tiene "48 h sin cubrir": antes de
    # su primera ejecución no existía. Eso es lo que marcaba falsos huecos en Sadida.
    from data.ingest.coinalyze import report
    s = series()
    now = int(datetime.now(UTC).timestamp())
    clock = [now - now % 60]
    archive(db_engine, FakeCoinalyze(clock), [s], clock=lambda: clock[0])  # type: ignore[arg-type]
    assert report(db_engine, hours=48, max_lag_hours=12, series=[s]) == 0
    out = capsys.readouterr().out
    assert "SIN CUBRIR" not in out and "0 problemas" in out
