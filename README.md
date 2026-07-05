# Multi-Asset Trading Bot

Hybrid multi-asset platform (stocks, options, spot crypto) built on Lumibot.

## Modules

| File | Purpose |
|---|---|
| `config.json` | System state and parameters (single source of truth) |
| `config_models.py` | Pydantic v2 validation + atomic persistence |
| `wheel_strategy.py` | The Wheel strategy (CSP → CC, 80% early close) via Alpaca |
| `crypto_rebalancer.py` | Spot BTC/ETH/USDC rebalancing with ±5% threshold |
| `app_dashboard.py` | Streamlit dashboard: metrics, hot parameter control, logs |

## Usage

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in API keys

python crypto_rebalancer.py          # rebalancer dry-run demo
python wheel_strategy.py             # Lumibot engine (paper by default)
streamlit run app_dashboard.py       # control panel
```

## Safety

- Puts are cash-secured only; calls are covered only; crypto is spot only (no margin or shorts).
- `live_trading_mode: false` by default → paper trading. Keep it there for ≥60 days before going live.
- Kill switch and daily loss limit live in `config.json`, editable from the dashboard.
- Credentials via `.env` only (excluded in `.gitignore`).
