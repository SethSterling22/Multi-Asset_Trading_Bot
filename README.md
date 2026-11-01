# Multi-Asset Trading Bot

Multi-asset, multi-strategy trading platform (stocks, options, spot crypto and
CFTC-regulated crypto perps), run by one person from a homelab. The goal is not
to maximise any single strategy's Sharpe but to keep the strategies
**decorrelated** from each other.

**Status: migrating from scaffold to platform.** The repo is following the plan
in [docs/INFORME_TALOS.md](docs/INFORME_TALOS.md). Session 0 (layout, tooling,
CI, risk floor guard), Session 1 (Phase 0.1–0.2: ledger, metrics, structured
logging) and Session 2 (Phase 0.3–0.6: real delta, shared guards, breakers,
kill switch) are done; Session 3 (honest backtest, data recorder) is next. The
only runnable code is still the legacy scaffold below, and **it is not fit for
live capital** (see [Known gaps](#known-gaps-in-the-legacy-code)).

---

## Strategy sleeves

| Sleeve | Asset | Direction | State |
|---|---|---|---|
| `wheel` | Equities + options (Alpaca) | Premium selling | Legacy scaffold; real delta pending |
| `crypto_rebalance` | Spot crypto | Long/flat | Legacy scaffold; simulated broker only |
| `funding_carry` | CFTC-regulated perps (Coinbase/Kraken) | Delta-neutral | To build (Phase 2) |
| `xsec_momentum` | BTC/ETH perps + alt spot | Long/short on majors only; alts long/flat | To build (Phase 2) |
| `trend` | BTC/ETH perps | Long/short | To build (Phase 2) |

---

## Where things live

| Path | What |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Working contract and **invariants**. Read it before changing anything |
| [DECISIONS.md](DECISIONS.md) | Append-only decision log |
| [docs/INFORME_TALOS.md](docs/INFORME_TALOS.md) | The plan: cost arithmetic, venue choice, architecture, phase gates (§10) |
| [docs/PROMPTS_CLAUDE_CODE.md](docs/PROMPTS_CLAUDE_CODE.md) | One working session per phase, each with its exit gate |
| `docs/model.py` | Reproducible calculations behind every `[C]` figure in the plan |
| `docs/schema_reference.sql` | Reference DDL; the real DDL goes in `migrations/` (Alembic) |
| `docs/dashboard_mockup.html` | Visual reference for the new dashboard |
| [core/risk_floor.py](core/risk_floor.py) | Risk limits as **Python constants**, hash-verified. Never config |
| `core/`, `risk/`, `strategies/` | In use: config, ledger, greeks, metrics, logging; pre-trade checks, breakers, kill switch, LLM action space; the strategies and their shared guards (`strategies/base.py`) |
| `brokers/`, `data/`, `execution/`, `llm/`, `api/`, `web/`, `backtest/` | Rest of the target tree (informe §9.2), still empty |
| `tests/guards/` | Guard tests that must never fail; required to merge |
| `docker/compose.yml` | Homelab stack (Compose + Traefik), reference for Phase 1 |

The original scaffold moved into the tree in Session 2:

| Was | Now |
|---|---|
| `config_models.py` | `core/config/models.py` (a shim at the root keeps `app_dashboard.py` working) |
| `wheel_strategy.py` | `strategies/wheel/logic.py` (the cycle, behind an `OptionsBroker` port) + `strategies/wheel/strategy.py` (the Lumibot adapter) |
| `crypto_rebalancer.py` | `strategies/crypto_rebalance/rebalancer.py` |
| `app_dashboard.py` (Streamlit) | Still at the root; `compute_metrics` is now `core/metrics.py`; deleted in Session 7 |

---

## Development

**Requires Python 3.12** (Lumibot is only classified through 3.12) and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync                         # all service groups: engine, api, llm, dev
uv run pre-commit install       # ruff + gitleaks on every commit
```

The ledger tests (marker `db`) run against a local Postgres + TimescaleDB:

```bash
docker compose -f docker/compose.dev.yml up -d     # localhost:5433, dev only
export TEST_DATABASE_URL=postgresql+psycopg://trading@127.0.0.1:5433/postgres
```

The checks that gate every change:

```bash
uv run pytest                   # all tests; `-m guards` is what CI runs
uv run ruff check .
uv run mypy core risk           # strict
uv run python -m scripts.synthetic_fills --database-url "$TEST_DATABASE_URL"
```

Without `TEST_DATABASE_URL` the `db` tests fail instead of being skipped; run
`uv run pytest -m "not db"` to leave them out on purpose. Alembic reads the
database URL from `DATABASE_URL` only, never from `alembic.ini`. Each service installs only its own dependency group
(`uv sync --no-default-groups --group engine`).

### Running the strategies (paper only)

```bash
cp .env.example .env            # Alpaca paper keys; see RUNNING.md §3
uv run python -m core.config.models        # validates config.json
uv run python -m strategies.crypto_rebalance   # dry-run demo with the simulated broker
uv run streamlit run app_dashboard.py      # legacy panel at localhost:8501
```

The Wheel refuses to start without its ledger and kill switch:

```bash
docker compose -f docker/compose.dev.yml up -d   # Postgres :5433 and Redis :6380
export DATABASE_URL=postgresql+psycopg://trading@127.0.0.1:5433/trading
export REDIS_URL=redis://127.0.0.1:6380/0
DATABASE_URL=$DATABASE_URL uv run alembic upgrade head
uv run python -m strategies.wheel          # paper by default
```

Importing Lumibot loads `.env` and opens a broker socket on its own, so only
import `strategies.wheel.strategy` where that is intended.

`live_trading_mode` is `false` in `config.json`, so the engine uses Alpaca's
paper endpoint and the rebalancer stays in dry-run. Keep it that way: going live
is gated by Phase 4 of the plan (60–90 days of paper), not by this flag.

---

## Principles

From the original roadmap, kept as-is:

1. **Deterministic before intelligent.** Rules first; the LLM later, always
   measured against the rules as baseline.
2. **Propose, then approve.** Automation suggests; a human authorises anything
   that changes risk.
3. **Guards are code, not configuration.** No excess leverage, no unauthorised
   shorts, no naked options: structural properties that survive any config edit.
4. **Measure the cost, not just the return.** Every P&L figure is net of fees,
   funding, slippage and AI cost, or it is not reported.

The invariants that enforce these (immutable risk floor, LLM effects only ever
reduce risk, positions derived from `fills`, write-ahead order intent,
exchange-resident stops, fail-closed kill switch) are in [CLAUDE.md](CLAUDE.md) §1.

---

## Known gaps

Found in the Session 0 audit. **Fixed in Session 2**, each with a test in
`tests/guards/`:

- The Wheel's dead circuit breaker hook is gone; the breakers in
  `risk/breakers.py` replace it.
- The 80% early close reads the entry premium from the ledger, so it survives a
  restart.
- The daily and weekly loss limits use the first `equity_curve` snapshot of the
  day/week and **fail closed** when there is none.
- Cash-secured puts count the collateral of every open and pending CSP.
- Pending orders are tracked: an unfilled sell or close is not resubmitted.
- Rebalancer buys are capped at the USDC in total, and the simulated broker
  refuses an overspend or an oversell.
- The moneyness proxy is deleted: delta comes from the broker or from
  Black-Scholes with implied volatility, or the trade is skipped and logged.

**Still open:**

- Orders are still market orders (Session 5, execution).
- The Lumibot adapter (`strategies/wheel/strategy.py`) has never run against a
  broker; only the logic behind it is tested.
- The legacy dashboard reads `logs/*.csv` that nothing writes, and its "live
  trading" toggle only takes effect when the engine restarts (Session 7).

[RUNNING.md](RUNNING.md) §0 has the same list and what blocks live trading.

---

This is a technical tool, not financial, legal or tax advice. Backtest, then
paper trade for at least 60 days, then start with capital you can afford to lose
entirely.
