# Multi-Asset Trading Bot

Hybrid multi-asset trading platform (stocks, options, spot crypto) built on
Lumibot, with a Streamlit control panel and Pydantic-validated configuration.

**Status: scaffold.** Core logic and safety guards are implemented; live broker
adapters, the log writer, and a production-grade delta calculation are not.
See [RUNNING.md](RUNNING.md) Section 0 for the full gap list before planning
any live deployment.

---

## Modules

| File | Purpose |
|---|---|
| `config.json` | System state and parameters — single source of truth |
| `config_models.py` | Pydantic v2 validation + atomic persistence |
| `wheel_strategy.py` | The Wheel strategy (CSP → assignment → CC, 80% early close) |
| `crypto_rebalancer.py` | Spot BTC/ETH/USDC rebalancing with a ±5% threshold |
| `app_dashboard.py` | Streamlit dashboard: metrics, hot parameter control, logs |
| `RUNNING.md` | Full runbook: paper trading → go/no-go → live deployment |
| `ROADMAP.md` | Phased plan for upcoming work |

---

## Quick start

**Requires Python 3.12.** Not 3.13 or 3.14 — Lumibot is only tested through
3.12, and source-built newer versions often lack required C extensions.

```bash
# 1. Environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python --version                   # must report 3.12.x
pip install -r requirements.txt
mkdir -p logs

# 2. Credentials (paper keys — see RUNNING.md section 3)
cp .env.example .env
# ...then edit .env with your Alpaca paper API keys

# 3. Verify the install
python config_models.py            # validates config.json
python crypto_rebalancer.py        # dry-run demo of the rebalancer

# 4. Run
streamlit run app_dashboard.py     # control panel at localhost:8501
python wheel_strategy.py           # trading engine (paper by default)
```

`live_trading_mode` is `false` in `config.json` by default, so the engine
connects to Alpaca's paper endpoint and the rebalancer stays in dry-run.
Nothing touches real money until you deliberately change that flag.

---

## How the pieces fit together

```
  config.json  ◄──── writes (Pydantic-validated, atomic) ──── app_dashboard.py
       │                                                             ▲
       │ hot-reload on mtime change                                  │
       ▼                                                             │
  wheel_strategy.py ──────► logs/*.csv ──── reads (mtime cache) ─────┘
  crypto_rebalancer.py
```

Two independent processes coordinated through the filesystem: the dashboard
writes configuration, the engine detects the change and reloads without a
restart; the engine writes logs, the dashboard detects new data and refreshes.

---

## Safety model

Risk limits are structural, not just validated:

- **Cash-secured puts only** — full collateral (strike × 100) must be in cash
  before an order is transmitted.
- **Covered calls only** — at least 100 shares held per contract sold.
- **Spot crypto only** — sells capped at actual holdings, buys capped at
  available USDC. Leverage and shorting are arithmetically impossible.
- **Kill switch** — halts all new orders, editable from the dashboard.
- **Daily loss limit** — trading suspends for the day at −3% (configurable).
- **Circuit breaker** — closes short options on abnormal market conditions.
- **Credentials** — loaded from `.env` only, which `.gitignore` excludes.

---

## Documentation

- [RUNNING.md](RUNNING.md) — installation, account setup, the 60-day paper
  phase, the go/no-go checklist, and how to switch to live capital.
- [ROADMAP.md](ROADMAP.md) — planned evolution: foundations, additional
  strategies, the data and intelligence layer, and operations.

---

This is a technical tool, not financial advice. Backtest, then paper trade for
at least 60 days, then start with capital you can afford to lose entirely.
