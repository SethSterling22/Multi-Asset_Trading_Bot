"""Demo en dry-run contra el broker simulado: uv run python -m strategies.crypto_rebalance

No hay adaptador de broker real todavía, así que esto nunca envía una orden. Por
eso el kill switch de la demo usa fuentes en memoria (siempre inactivas) más el
flag de config.json; un adaptador real tendrá que construirlo con las tres fuentes
de verdad, como hace strategies/wheel/strategy.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core import log
from core.config.models import CONFIG_PATH
from core.ledger.equity import EquitySnapshot
from core.ledger.fills import Fill
from risk.breakers import KillSwitch
from strategies.base import ParamsLoader, Rejected, config_kill_source
from strategies.crypto_rebalance.rebalancer import CryptoRebalancer, PaperSpotBroker


class _DemoLedger:
    def record_fill(self, fill: Fill) -> bool:
        return True

    def record_equity(self, snapshot: EquitySnapshot) -> bool:
        return True


class _DemoLossReference:
    """Sin historia: la equity de referencia es la de ahora (pérdida 0)."""

    def __init__(self, equity: Decimal) -> None:
        self._equity = equity

    def day_start_equity(self, strategy_id: str, now: datetime) -> Decimal:
        return self._equity

    def week_start_equity(self, strategy_id: str, now: datetime) -> Decimal:
        return self._equity


def main() -> None:
    log.configure("crypto_rebalance_demo")
    demo = PaperSpotBroker(
        balances={"BTC": 0.30, "ETH": 2.0, "USDC": 5000.0},   # BTC overweight
        prices={"BTC": 68000.0, "ETH": 3500.0},
    )
    params = ParamsLoader(CONFIG_PATH)
    bot = CryptoRebalancer(demo)
    cfg = params.current()
    if isinstance(cfg, Rejected):
        raise SystemExit(f"config.json inválido: {cfg.detail}")
    _, total = bot.current_state(cfg)
    bot.setup_base(
        params=params,
        kill_switch=KillSwitch(
            risk_events=lambda _: False, redis=lambda _: False, halt_file=lambda _: False,
            extra={"config.json": config_kill_source(params)},
        ),
        ledger=_DemoLedger(),
        loss_reference=_DemoLossReference(Decimal(str(total))),
        clock=lambda: datetime.now(UTC),
    )
    ids = bot.execute(dry_run=True)
    print(f"Total value: ${total:,.2f} | Executed orders: {ids or 'none (dry-run)'}")


main()
