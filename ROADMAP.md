# Roadmap

Planned evolution of the platform, in dependency order. Phases are sequential
because each one relies on what the previous one produced. Within a phase,
items can be built in parallel.

**Current state:** the Wheel strategy, config validation and dashboard exist.
Nothing records performance, contract selection uses a placeholder delta, and
no live broker adapter is implemented. See `RUNNING.md` section 0.

---

## Phase 0 — Foundations

**Goal:** make the 60-day paper phase produce measurable, trustworthy evidence.

Everything downstream depends on this. Strategies you cannot measure are
strategies you cannot evaluate; an intelligence layer that adjusts parameters
based on performance needs performance data to exist first.

### 0.1 Performance logging — `logging_sink.py`

The dashboard reads `logs/equity.csv` and `logs/trades.csv`. Nothing writes them.

- Append `timestamp,equity` on every trading iteration.
- Append one row per fill: `timestamp,symbol,asset_type,side,quantity,price,
  premium,commission,strategy,order_id`.
- Rotate by month so files stay manageable.
- Write via a temp-file swap or append-only with flush, so the dashboard never
  reads a partially written row.

**Unblocks:** every metric on the dashboard, the go/no-go checklist, and any
future performance-based decision.

### 0.2 Real delta calculation — `greeks.py`

`_estimate_delta()` currently falls back to a crude moneyness proxy when the
broker returns no greeks. That fallback is not a delta and must not select
contracts.

- Primary source: broker-provided greeks.
- Fallback: Black-Scholes delta computed from implied volatility, spot,
  strike, time to expiry and risk-free rate.
- If neither is available: **skip the trade and log it.** Never guess.
- Add a sanity band — reject any contract whose computed delta is outside a
  plausible range for its moneyness.

**Unblocks:** correct contract selection, which is the core of the Wheel.

### 0.3 Backtest harness — `backtest.py`

A single entry point that runs any strategy over a date range and emits a
report (equity curve, Sharpe, max drawdown, trade list, assignment count).

- Must cover a full cycle including a drawdown period, not only bull markets.
- Same metric code as the dashboard, so backtest and live numbers are
  directly comparable.

### 0.4 Safety-guard test suite — `tests/`

The guards are the part that must never silently break. Cover at minimum:

- CSP rejected when cash < strike × 100
- Covered call rejected without 100 shares per contract
- Daily loss limit halts trading
- Kill switch blocks all order submission
- Crypto sell never exceeds holdings; buy never exceeds USDC
- Pydantic rejects every invalid config permutation

Run these before any deploy. A refactor that breaks a guard should fail loudly.

### 0.5 Structured logging

Replace ad-hoc logging with a consistent format (timestamp, level, strategy,
symbol, action, reason). Every rejected order should log *why* it was
rejected — that record is what makes the paper phase diagnostic rather than
just observational.

---

## Phase 1 — Strategy expansion

**Goal:** go from one strategy to a coordinated portfolio of four.

### 1.1 Strategy base class — `strategies/base.py` *(do this first)*

Before adding three strategies, extract what the Wheel already does into a
shared base: config loading and hot-reload, kill switch and daily-loss checks,
position-size validation, logging sink wiring.

Building the other strategies first would triple-duplicate the safety guards,
and duplicated guards drift out of sync. This is the highest-leverage
refactor in the whole roadmap.

### 1.2 Capital allocator — `allocator.py`

With four strategies competing for one account, you need explicit budgeting:

- A capital budget per strategy, expressed as a fraction of portfolio value.
- Enforcement that the sum of committed collateral never exceeds available
  cash — the Wheel reserving collateral must be visible to the spread module.
- A global exposure ceiling independent of per-trade limits.

Without this, four independently sensible strategies can collectively
over-commit the account. New config section: `capital_allocation`.

### 1.3 Bull put spreads — `strategies/spreads.py`

Parameters already exist in `config.json` under `hedged_spread_parameters`.

- Sell put at `sell_delta_put`, buy protection at `buy_delta_coverage_put`.
- Collateral = spread width × 100, not strike × 100 — far more capital
  efficient than a CSP and the reason this matters at smaller account sizes.
- Verify empirically that realised max loss matches
  `(K_short − K_long) − net_credit`.
- Requires Alpaca options **Level 3** for live trading.

### 1.4 Iron condor

Extends 1.3: a bull put spread and a bear call spread opened simultaneously.
Add `iron_condor_parameters` to config (call-side deltas, minimum IV rank to
enter). Only one side can lose, so risk is defined in both directions.

### 1.5 Crypto live adapter — `brokers/coinbase_adapter.py`

Implement the existing `SpotBroker` protocol against Coinbase Advanced
(`/api/v3/brokerage`, CDP Ed25519 keys, View + Trade permissions only).

- Four methods: `get_balances`, `get_price_usd`, `market_buy`, `market_sell`.
- Handle rate limits (5 req/s on private endpoints) and retries with backoff.
- Test against sandbox keys before production keys.
- The rebalancing logic itself needs no changes — that was the point of the
  protocol abstraction.

### 1.6 Momentum ETFs — `strategies/momentum.py`

SMA crossover plus RSI on TQQQ/SQQQ. Strict limits: 2% of portfolio per
trade, 5% hard stop-loss, daily circuit breaker. Backtest this one especially
carefully — leveraged ETFs suffer volatility decay, so a strategy that looks
profitable on trending data can bleed steadily in chop.

### 1.7 Strategy scheduler

Coordinate which strategies run when: equities and options only during market
hours, crypto 24/7, rebalancing on a fixed cadence rather than every tick.

---

## Phase 2 — Data and market intelligence

**Goal:** move from fixed parameters to parameters informed by market regime.

### 2.1 Data layer — `data/`

One client per source, each with local caching and a documented refresh cadence:

- `fred.py` — interest rates, inflation, credit spreads, yield curve. Free API
  key from the St. Louis Fed. Defines the macro regime.
- `volatility.py` — VIX, VIX term structure, per-symbol IV rank/percentile.
  This is the most directly actionable feed: IV rank determines whether
  selling premium is attractive right now and at what delta.
- `calendar.py` — earnings dates, dividend dates, FOMC meetings. Selling
  options through an earnings event is a materially different bet; the bot
  should know when it is about to do that.
- `edgar.py` — SEC filings for whitelist validation (balance sheet health,
  cash flow). You will be assigned these shares eventually; this checks they
  are worth owning.
- `news.py` — headlines and sentiment for whitelist symbols.

Cache aggressively. These feeds have rate limits and most update daily or
slower.

### 2.2 Deterministic regime classifier — `regime.py` *(before any LLM)*

A rule-based classifier that maps the data layer to a market regime:
calm / elevated / stressed, derived from VIX level and term structure, credit
spreads and trend.

Each regime maps to concrete parameter adjustments — for example, lower deltas
and a shift from naked CSPs toward defined-risk spreads as stress rises.

**Build this before the LLM layer.** It is testable, backtestable, cheap and
deterministic, and it becomes the baseline the LLM must beat. If an LLM
proposal disagrees with the rule-based classifier, that disagreement is a
signal worth examining rather than silently accepting.

### 2.3 LLM router — `llm/router.py`

Classify each request by complexity and sensitivity, then dispatch:

- **Local (Hermes via Ollama/LM Studio, 64k context):** parsing API responses,
  formatting JSON, daily summaries, alert text.
- **Cloud (Claude):** weekly macro synthesis, regime interpretation, whitelist
  review, calibration decisions.

Policy file (`routing.yaml`) with a default that keeps routine work local, so
bulk structured-data tasks cannot silently accrue API cost.

### 2.4 Agent team — `llm/agents.py`

Researcher gathers evidence from the data layer; bull and bear argue opposing
cases; trader arbitrates and produces a **proposed** config diff.

Runs on a schedule — weekly at market close is a sensible default. Each run
produces a written rationale stored alongside the proposal.

### 2.5 Human approval gate — *non-negotiable*

Agents never write `config.json` directly. They write a proposal to
`proposals/YYYY-MM-DD.json` with its reasoning. You review it in the dashboard
and approve or reject; only approval applies the change, through the same
Pydantic validation and atomic write the dashboard already uses.

Reasons this is not optional: an LLM writing directly to a live trading
system's configuration is an unbounded failure mode, proposals are far easier
to audit than executed changes, and the rejected proposals themselves become
valuable training material for tuning the prompts.

Add a hard constraint layer regardless: agents can only propose values inside
ranges you define. No proposal can widen a risk limit beyond a ceiling set in
code.

### 2.6 Attribution

Record which regime and which proposal was active for every trade. Without
this you cannot tell whether the intelligence layer is adding value or
expensively adding noise.

---

## Phase 3 — Operations

### 3.1 Alerting — `notifications.py`

Telegram or email for: order fills, assignments, guard triggers, daily loss
limit, crashes, and a daily summary. Assignment alerts matter most — that is
when the strategy changes phase and may need your attention.

### 3.2 Process supervision

systemd service (or supervisor/pm2) with auto-restart, health checks and log
rotation. A trading bot that dies silently overnight and leaves positions
unmanaged is worse than one that never started.

### 3.3 Deployment

Small VPS for continuous operation. Docker for reproducibility. Isolated
container for any LLM-generated code execution. Secrets via environment, never
baked into the image.

### 3.4 Tax and reporting export

Options assignments complicate cost basis considerably. Export trades in a
format your tax professional can work with. Build this before your first live
tax year, not during it.

---

## Cross-cutting principles

- **Every new strategy: backtest → paper → live.** No exceptions, including
  for changes that look trivial.
- **Deterministic before intelligent.** Rules first, LLM second, always with
  the rules as the measurable baseline.
- **Propose, then approve.** Automation suggests; a human authorises anything
  that changes risk.
- **Guards are code, not configuration.** No leverage, no shorting, no naked
  options — structural properties that survive any config edit.
- **Measure before optimising.** Phase 0 exists so that every later decision
  can be evaluated against recorded evidence rather than intuition.

---

## Suggested sequencing

Phase 0 gates everything: until logging and delta are real, paper-trading days
accumulate without producing usable evidence. Start the 60-day clock once
Phase 0 lands.

Phase 1 can run while the paper clock is ticking — new strategies enter their
own backtest-then-paper cycle as they are finished, without resetting the
Wheel's clock.

Phase 2 is most valuable once several weeks of recorded performance exist,
because the regime classifier can then be validated against what actually
happened rather than tuned in the abstract.

Phase 3 items should land before live capital, not after — particularly
alerting and process supervision.
