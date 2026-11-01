# Multi-Asset Trading Bot

Multi-asset, multi-strategy trading platform (stocks, options, spot crypto and
CFTC-regulated crypto perps), run by one person from a homelab. The goal is not
to maximise any single strategy's Sharpe but to keep the strategies
**decorrelated** from each other.

**Status: migrating from scaffold to platform.** The repo is following the plan
in [docs/INFORME_TALOS.md](docs/INFORME_TALOS.md). Session 0 (layout, tooling,
CI, risk floor guard) and Session 1 (Phase 0.1–0.2: ledger, metrics, structured
logging) are done; Session 2 (real delta, shared guards) is next. The
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
| `core/`, `brokers/`, `data/`, `strategies/`, `execution/`, `risk/`, `llm/`, `api/`, `web/`, `backtest/` | Target package tree (informe §9.2), still empty |
| `tests/guards/` | Guard tests that must never fail; required to merge |
| `docker/compose.yml` | Homelab stack (Compose + Traefik), reference for Phase 1 |

Legacy scaffold at the repo root, to be refactored into the tree above:

| File | Becomes | Session |
|---|---|---|
| `config_models.py` + `config.json` | `core/config/` + Postgres params; risk limits move to `core/risk_floor.py` | 1–2 |
| `wheel_strategy.py` | `strategies/wheel/` | 2 |
| `crypto_rebalancer.py` | `strategies/crypto_rebalance/` | 2 |
| `app_dashboard.py` (Streamlit) | `core/metrics.py` + FastAPI/HTMX dashboard; then deleted | 1, 7 |

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

### Running the legacy scaffold (paper only)

```bash
cp .env.example .env            # Alpaca paper keys; see RUNNING.md §3
mkdir -p logs
uv run python config_models.py          # validates config.json
uv run python crypto_rebalancer.py      # dry-run demo with the simulated broker
uv run streamlit run app_dashboard.py   # legacy panel at localhost:8501
uv run python wheel_strategy.py         # Wheel engine, paper by default
```

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

## Known gaps in the legacy code

Found in the Session 0 audit. They are fixed by the refactor in Sessions 1–2,
not patched in place:

- **The Wheel's circuit breaker never fires.** `on_abnormal_market_conditions`
  is not a Lumibot lifecycle hook.
- **The 80% early close stops working after a restart.** Entry premiums live
  only in memory.
- **The daily loss limit fails open** if the engine starts or restarts
  mid-session.
- **Cash-secured puts are checked per order, not in aggregate.** Several
  whitelisted symbols can count the same cash as collateral.
- **Orders are market orders, and pending orders are not tracked**, so an
  unfilled sell or close can be resubmitted on the next iteration.
- **Rebalancer buys are each capped at USDC, not in total.** Two buys can add
  up to more than the available USDC; the simulated broker allows negative
  balances, so it would not notice.
- **The delta fallback is a moneyness proxy, not a delta.** It must be replaced
  before contract selection can be trusted (Phase 0.3).
- **The legacy dashboard reads `logs/*.csv` that nothing writes**, and its
  "live trading" toggle only takes effect when the engine restarts.

[RUNNING.md](RUNNING.md) §0 has the same list and what blocks live trading.

---

This is a technical tool, not financial, legal or tax advice. Backtest, then
paper trade for at least 60 days, then start with capital you can afford to lose
entirely.
