"""El rebalanceador cripto contra un venue real (Alpaca paper), por el router.

    uv run python -m strategies.crypto_rebalance.live [--once] [--dry-run]

Sin `CRYPTO_DRY_RUN=false` en el entorno, corre en dry-run: planifica, registra y
no envía nada. Pasar a enviar órdenes (aunque sean de paper) es un acto explícito.

Cada ciclo (una hora):
  1. reconciliación contra el venue: con cualquier mismatch o el venue sin
     responder, el ciclo no opera (y un mismatch activa el kill switch);
  2. snapshot de equity del sleeve, que es la referencia de la pérdida diaria. Se
     escribe aunque el ciclo no opere (sleeve parado), para que el historial siga;
  3. plan del rebalanceador (gate común incluido) y órdenes por el router, y otro
     snapshot si ha habido fills.

El sleeve tiene presupuesto propio (`CRYPTO_BUDGET_USD`): su caja es el
presupuesto más el flujo de caja de SUS fills, no la caja de la cuenta, que en
paper comparte con la Wheel. Las tenencias salen del ledger (CLAUDE.md §1.3);
la reconciliación garantiza que coinciden con el venue antes de usarlas.

Límite conocido: los checks pre-trade ven solo este sleeve. La exposición
agregada entre sleeves llega cuando haya un segundo sleeve operando.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import Engine, create_engine

from brokers.base import Quote, SpotVenue, VenueUnavailableError, normalize_symbol
from core import log
from core.ledger.equity import snapshot, write_marks
from core.ledger.fills import AssetType, Fill, fetch_fills
from core.risk_floor import floor_hash
from execution.idempotency import OrderIntent, client_order_id
from execution.reconcile import reconcile
from execution.router import OrderRouter
from risk.breakers import KillSwitch
from risk.pretrade import ProposedState
from strategies.crypto_rebalance.rebalancer import STABLE_SYMBOL, CryptoRebalancer

STRATEGY_ID = CryptoRebalancer.strategy_id
LOOP_SECONDS = 3600
ZERO = Decimal(0)


def bar_id(now: datetime) -> int:
    """Una barra por hora: la misma decisión en la misma hora da el mismo client_order_id."""
    return int(now.timestamp()) // LOOP_SECONDS


def floor_to(qty: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("increment > 0")
    return (qty / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def sleeve_cash(fills: list[Fill], budget: Decimal) -> Decimal:
    """Presupuesto + flujo de caja de los fills del sleeve, neto de costes (§3.4)."""
    cash = budget
    for f in fills:
        cash += -f.signed_qty * f.price * f.multiplier - f.commission + f.funding_payment
    return cash


def holdings(fills: list[Fill]) -> dict[str, Decimal]:
    """Cantidad neta por símbolo normalizado (BTCUSD), derivada de los fills."""
    out: dict[str, Decimal] = {}
    for f in fills:
        key = normalize_symbol(f.symbol)
        out[key] = out.get(key, ZERO) + f.signed_qty
    return out


class RoutedSpotBroker:
    """`SpotBroker` del rebalanceador sobre el router: toda orden pasa por los guards."""

    def __init__(
        self,
        engine: Engine,
        venue: SpotVenue,
        router: OrderRouter,
        *,
        budget_usd: Decimal,
        pair: Callable[[str], str],
        dry_run: bool = False,
        logger: Any | None = None,
    ) -> None:
        if budget_usd <= 0:
            raise ValueError("CRYPTO_BUDGET_USD > 0")
        self._engine = engine
        self._venue = venue
        self._router = router
        self._budget = budget_usd
        self._pair = pair
        self._dry_run = dry_run
        self._log = logger if logger is not None else log.get_logger()
        self._floor_hash = floor_hash()
        self._bar = 0
        self._fills: list[Fill] = []
        self._quotes: dict[str, Quote] = {}

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    # -- ciclo -------------------------------------------------------------------
    def begin(self, now: datetime) -> None:
        """Estado fresco al empezar cada ciclo: fills del ledger, precios nuevos."""
        self._bar = bar_id(now)
        self._quotes = {}
        self._refresh()

    def _refresh(self) -> None:
        with self._engine.connect() as conn:
            self._fills = fetch_fills(conn, STRATEGY_ID)

    def _held(self) -> dict[str, Decimal]:
        return holdings(self._fills)

    def cash(self) -> Decimal:
        return sleeve_cash(self._fills, self._budget)

    def marks(self) -> dict[tuple[str, str], Decimal]:
        """Precio de marca de cada posición abierta, para el snapshot de equity."""
        held = self._held()
        return {(f.venue, f.symbol): self._quote(f.symbol).mid for f in self._fills
                if held.get(normalize_symbol(f.symbol), ZERO) != 0}

    def open_positions(self) -> dict[tuple[str, str], tuple[Decimal, Decimal]]:
        """(venue, symbol) -> (cantidad, precio de marca) de cada posición abierta."""
        held = self._held()
        return {key: (held[normalize_symbol(key[1])], mark) for key, mark in self.marks().items()}

    def _quote(self, symbol: str) -> Quote:
        pair = self._pair(symbol)
        if pair not in self._quotes:
            self._quotes[pair] = self._venue.quote(pair)
        return self._quotes[pair]

    # -- SpotBroker ----------------------------------------------------------------
    def get_balances(self) -> dict[str, float]:
        held = self._held()
        out = {s[: -len("USD")]: float(q) for s, q in held.items() if s.endswith("USD")}
        out[STABLE_SYMBOL] = float(self.cash())
        return out

    def get_price_usd(self, symbol: str) -> float:
        return float(self._quote(symbol).mid)

    def market_buy(self, symbol: str, notional_usd: float, reason: str = "") -> str:
        quote = self._quote(symbol)
        notional = Decimal(str(notional_usd))
        venue_cash = self._venue.cash()
        if notional > venue_cash:
            # La caja de la cuenta es compartida (Wheel en paper): no se pide al venue
            # lo que no tiene, aunque el presupuesto del sleeve dé para más.
            self._log.warning("buy_capped_by_venue_cash", strategy_id=STRATEGY_ID,
                              symbol=symbol, action="cap",
                              reason=f"{notional} > caja del venue {venue_cash}")
            notional = venue_cash
        return self._submit(symbol, "buy", notional / quote.ask, reason)

    def market_sell(self, symbol: str, quantity: float, reason: str = "") -> str:
        return self._submit(symbol, "sell", Decimal(str(quantity)), reason)

    # -- una orden ---------------------------------------------------------------
    def _submit(self, symbol: str, side: str, raw_qty: Decimal, reason: str = "") -> str:
        pair = self._pair(symbol)
        rules = self._venue.rules(pair)
        qty = floor_to(raw_qty, rules.qty_increment)
        if side == "sell":
            qty = min(qty, self._held().get(normalize_symbol(pair), ZERO))
        if qty < rules.min_order_qty or qty <= 0:
            self._log.info("order_skipped", strategy_id=STRATEGY_ID, symbol=pair,
                           action=f"order_{side}",
                           reason=f"qty {qty} < mínimo del venue {rules.min_order_qty}")
            return ""
        mid = self._quote(pair).mid
        intent = OrderIntent(
            client_order_id=client_order_id(STRATEGY_ID, pair, self._bar),
            venue=self._venue.name, symbol=pair, asset_type=AssetType.CRYPTO_SPOT,
            side=side, qty=qty, strategy_id=STRATEGY_ID, decision_price=mid,
            risk_floor_hash=self._floor_hash, reason=reason or None,
        )
        if self._dry_run:
            self._log.info("order_dry_run", strategy_id=STRATEGY_ID, symbol=pair,
                           action=f"order_{side}", reason="dry-run", qty=str(qty),
                           client_order_id=intent.client_order_id)
            return ""
        result = self._router.submit(intent, self.proposed(pair, side, qty))
        self._refresh()
        return intent.client_order_id if result.status is not None else ""

    def proposed(self, pair: str, side: str, qty: Decimal) -> ProposedState:
        """La cartera del sleeve tal como quedaría tras la orden."""
        after = dict(self._held())
        key = normalize_symbol(pair)
        after[key] = after.get(key, ZERO) + (qty if side == "buy" else -qty)
        mids = {s: self._quote(s).mid for s in after}
        notionals = {s: q * mids[s] for s, q in after.items()}
        held_value = sum((q * mids[s] for s, q in self._held().items()), ZERO)
        return ProposedState(
            symbol=pair, asset_type=AssetType.CRYPTO_SPOT,
            equity=self.cash() + held_value,
            position_qty=after[key], position_notional=abs(notionals[key]),
            leverage=Decimal(1),
            gross_notional=sum((abs(n) for n in notionals.values()), ZERO),
            net_notional=sum(notionals.values(), ZERO),
            open_positions=sum(1 for q in after.values() if q != 0),
            risk_usd=None,
        )


def run_cycle(
    engine: Engine,
    venue: SpotVenue,
    router: OrderRouter,
    broker: RoutedSpotBroker,
    bot: CryptoRebalancer,
    *,
    budget_usd: Decimal,
    now: datetime,
    record_equity: Callable[..., Any],
    logger: Any,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[str]:
    report = reconcile(engine, venue, router, strategy_id=STRATEGY_ID, logger=logger)

    def write_snapshot(ts: datetime) -> None:
        with engine.connect() as conn:
            fills = fetch_fills(conn, STRATEGY_ID)
        record_equity(snapshot(fills, strategy_id=STRATEGY_ID, allocated_usd=budget_usd,
                               marks=broker.marks(), ts=ts))
        # Y el precio de cada posición: con él, el historial dice qué símbolo se movió.
        with engine.begin() as conn:
            write_marks(conn, ts=ts, strategy_id=STRATEGY_ID, positions=broker.open_positions())

    # El snapshot va aunque el ciclo no opere: con el sleeve parado sus posiciones siguen
    # moviéndose de precio, y el historial tiene que verlo.
    broker.begin(now)
    try:
        write_snapshot(now)
    except VenueUnavailableError as exc:
        logger.error("cycle_skipped", strategy_id=STRATEGY_ID, action="skip",
                     reason=f"sin precios del venue: {exc}"[:300])
        return []
    if not report.ok:
        logger.error("cycle_skipped", strategy_id=STRATEGY_ID, action="skip",
                     reason=f"reconciliación: {len(report.mismatches)} mismatches, "
                            f"{report.unreachable} sin respuesta")
        return []
    ids = [oid for oid in bot.execute(dry_run=broker.dry_run) if oid]
    if ids:
        # Y otro tras operar, para que la equity del día refleje los fills de este ciclo.
        after = max(clock(), now + timedelta(microseconds=1))
        broker.begin(after)
        write_snapshot(after)
    return ids


def _env_decimal(env: Mapping[str, str], name: str) -> Decimal:
    try:
        value = Decimal(env[name])
    except (KeyError, ArithmeticError):
        sys.exit(f"{name} es obligatorio y numérico (ver RUNNING.md).")
    if value <= 0:
        sys.exit(f"{name} > 0")
    return value


def main(argv: list[str] | None = None) -> None:
    import redis
    from dotenv import load_dotenv

    from brokers.alpaca_crypto import AlpacaCryptoVenue, pair
    from core.config.models import CONFIG_PATH
    from risk.breakers import file_source, redis_source, risk_events_source
    from strategies.base import (
        EquityCurveLossReference,
        ParamsLoader,
        PostgresLedger,
        config_kill_source,
    )
    from strategies.runtime import run_process

    args = argparse.ArgumentParser(description="Rebalanceador cripto contra Alpaca paper")
    args.add_argument("--once", action="store_true", help="un ciclo y salir")
    args.add_argument("--dry-run", action="store_true", help="planifica y registra, sin órdenes")
    opts = args.parse_args(argv)
    # Por defecto NO envía órdenes: hay que decir CRYPTO_DRY_RUN=false de forma explícita.
    dry_run = opts.dry_run or os.environ.get("CRYPTO_DRY_RUN", "true").strip().lower() != "false"

    load_dotenv()
    log.configure(STRATEGY_ID)
    logger = log.get_logger()
    required = ("ALPACA_CRYPTO_API_KEY", "ALPACA_CRYPTO_API_SECRET", "DATABASE_URL", "REDIS_URL")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit(f"Faltan variables: {', '.join(missing)} (ver RUNNING.md).")
    budget = _env_decimal(os.environ, "CRYPTO_BUDGET_USD")

    engine = create_engine(os.environ["DATABASE_URL"])
    venue = AlpacaCryptoVenue(os.environ["ALPACA_CRYPTO_API_KEY"],
                              os.environ["ALPACA_CRYPTO_API_SECRET"])
    params = ParamsLoader(CONFIG_PATH)
    kill_switch = KillSwitch(
        risk_events=risk_events_source(engine),
        redis=redis_source(redis.Redis.from_url(os.environ["REDIS_URL"])),
        halt_file=file_source(),
        extra={"config.json": config_kill_source(params)},
    )
    router = OrderRouter(engine, venue, kill_switch)
    broker = RoutedSpotBroker(engine, venue, router, budget_usd=budget, pair=pair,
                              dry_run=dry_run)
    bot = CryptoRebalancer(broker)
    ledger = PostgresLedger(engine)
    bot.setup_base(params=params, kill_switch=kill_switch, ledger=ledger,
                   loss_reference=EquityCurveLossReference(engine, UTC),
                   clock=lambda: datetime.now(UTC))
    logger.warning("startup", strategy_id=STRATEGY_ID, action="start",
                   reason="paper, dry-run" if dry_run else "paper: envía órdenes",
                   budget_usd=str(budget), risk_floor=floor_hash()[:12])

    stop = threading.Event()

    def cycle() -> None:
        try:
            ids = run_cycle(engine, venue, router, broker, bot, budget_usd=budget,
                            now=datetime.now(UTC), record_equity=ledger.record_equity,
                            logger=logger)
            logger.info("cycle_done", strategy_id=STRATEGY_ID, action="cycle",
                        reason=f"{len(ids)} órdenes", orders=ids)
        except Exception as exc:
            # Un ciclo que falla no tumba el proceso; el siguiente reconcilia primero.
            logger.exception("cycle_failed", strategy_id=STRATEGY_ID, action="cycle",
                             reason=f"{type(exc).__name__}: {exc}"[:300])

    def run() -> None:
        cycle()
        while not opts.once and not stop.wait(LOOP_SECONDS):
            cycle()

    run_process(run, stop.set)


if __name__ == "__main__":
    main()
