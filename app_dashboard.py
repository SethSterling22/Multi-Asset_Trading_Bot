"""Streamlit control panel for the algorithmic trading system.

Pages (st.navigation):
  1. Metrics     - balance, daily P&L, Sharpe, Max Drawdown, equity curve.
  2. Parameters  - hot-editing of config.json validated with Pydantic.
  3. Logs        - latest trades (st.dataframe) and active config (st.json).

Refresh: the trades log mtime is compared on every rerun; if the engine
wrote new data, the cache is invalidated and charts reload from disk
without blocking loops.

Run:  streamlit run app_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from pydantic import ValidationError

from config_models import CONFIG_PATH, TradingConfig

BASE_DIR = Path(__file__).parent
TRADES_LOG = BASE_DIR / "logs" / "trades.csv"      # written by the Lumibot engine
EQUITY_LOG = BASE_DIR / "logs" / "equity.csv"      # columns: timestamp,equity

st.set_page_config(page_title="Multi-Asset Trading Bot", layout="wide")


# ---------------------------------------------------------------------- #
# Data loading with mtime-based invalidation                              #
# ---------------------------------------------------------------------- #
def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(show_spinner=False)
def load_csv(path_str: str, mtime: float) -> pd.DataFrame:
    """`mtime` is part of the cache key: when the engine writes the file,
    the key changes and Streamlit reloads from disk."""
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def load_config() -> TradingConfig:
    return TradingConfig.load(CONFIG_PATH)


# ---------------------------------------------------------------------- #
# Financial metrics                                                       #
# ---------------------------------------------------------------------- #
def compute_metrics(equity: pd.DataFrame) -> dict:
    if equity.empty or "equity" not in equity:
        return {"balance": 0.0, "pnl_day": 0.0, "pnl_day_pct": 0.0,
                "sharpe": 0.0, "max_drawdown": 0.0}
    series = equity["equity"].astype(float)
    returns = series.pct_change().dropna()
    balance = float(series.iloc[-1])
    pnl_day = float(series.iloc[-1] - series.iloc[-2]) if len(series) > 1 else 0.0
    pnl_day_pct = pnl_day / float(series.iloc[-2]) * 100 if len(series) > 1 else 0.0
    sharpe = float(returns.mean() / returns.std() * (252 ** 0.5)) if returns.std() else 0.0
    running_max = series.cummax()
    max_dd = float(((series - running_max) / running_max).min() * 100)
    return {"balance": balance, "pnl_day": pnl_day, "pnl_day_pct": pnl_day_pct,
            "sharpe": sharpe, "max_drawdown": max_dd}


# ---------------------------------------------------------------------- #
# Page 1: Metrics                                                         #
# ---------------------------------------------------------------------- #
def page_metrics() -> None:
    st.title("Financial metrics")
    equity = load_csv(str(EQUITY_LOG), _mtime(EQUITY_LOG))
    m = compute_metrics(equity)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net liquidation balance", f"${m['balance']:,.2f}")
    c2.metric("Daily P&L", f"${m['pnl_day']:,.2f}", f"{m['pnl_day_pct']:+.2f}%")
    c3.metric("Sharpe ratio", f"{m['sharpe']:.2f}")
    c4.metric("Max drawdown", f"{m['max_drawdown']:.2f}%")

    if not equity.empty:
        fig = px.line(equity, x="timestamp", y="equity",
                      title="Cumulative equity curve")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No equity data yet in logs/equity.csv.")


# ---------------------------------------------------------------------- #
# Page 2: Parameter console (hot control)                                 #
# ---------------------------------------------------------------------- #
def page_parameters() -> None:
    st.title("Strategy parameter console")
    cfg = load_config()
    wheel, guards = cfg.wheel_parameters, cfg.risk_guards

    st.subheader("System status")
    col_a, col_b, col_c = st.columns(3)
    active = col_a.toggle("System active", value=cfg.system_status.active)
    kill = col_b.toggle("Emergency kill switch",
                        value=cfg.system_status.emergency_kill_switch)
    live = col_c.toggle("Live trading mode (⚠ real money)",
                        value=cfg.system_status.live_trading_mode)

    st.subheader("The Wheel")
    delta_csp = st.slider("Target CSP delta (short put)", -0.50, -0.05,
                          float(wheel.delta_limit_csp), 0.01)
    delta_cc = st.slider("Target Covered Call delta", 0.05, 0.50,
                         float(wheel.delta_limit_cc), 0.01)
    early_close = st.slider("Early close (% of premium captured)", 0.50, 0.95,
                            float(wheel.early_close_percentage_gain), 0.05)
    dte = st.slider("Expiration window (days, DTE)", 7, 90,
                    (wheel.target_expiration_days_min,
                     wheel.target_expiration_days_max))
    whitelist = st.text_input("Asset whitelist (comma-separated)",
                              ", ".join(wheel.whitelist_assets))

    st.subheader("Risk guards")
    max_risk = st.number_input("Max risk per trade (portfolio fraction)",
                               0.005, 0.10, float(guards.max_portfolio_risk_per_trade),
                               0.005, format="%.3f")
    daily_loss = st.number_input("Daily loss limit (fraction)",
                                 0.005, 0.20, float(guards.daily_loss_limit_percentage),
                                 0.005, format="%.3f")

    if st.button("Save configuration", type="primary"):
        try:
            updated = cfg.model_copy(deep=True)
            updated.system_status.active = active
            updated.system_status.emergency_kill_switch = kill
            updated.system_status.live_trading_mode = live
            updated.wheel_parameters.delta_limit_csp = delta_csp
            updated.wheel_parameters.delta_limit_cc = delta_cc
            updated.wheel_parameters.early_close_percentage_gain = early_close
            updated.wheel_parameters.target_expiration_days_min = dte[0]
            updated.wheel_parameters.target_expiration_days_max = dte[1]
            updated.wheel_parameters.whitelist_assets = [
                s.strip().upper() for s in whitelist.split(",") if s.strip()
            ]
            updated.risk_guards.max_portfolio_risk_per_trade = max_risk
            updated.risk_guards.daily_loss_limit_percentage = daily_loss
            # Full revalidation before touching disk.
            validated = TradingConfig.model_validate(updated.model_dump())
            validated.save(CONFIG_PATH)
            st.success("config.json validated and saved. The engine will hot-reload it.")
        except ValidationError as exc:
            st.error(f"Configuration rejected by Pydantic:\n\n{exc}")


# ---------------------------------------------------------------------- #
# Page 3: Operations log                                                  #
# ---------------------------------------------------------------------- #
def page_logs() -> None:
    st.title("Operations log and alerts")
    trades = load_csv(str(TRADES_LOG), _mtime(TRADES_LOG))
    if not trades.empty:
        st.dataframe(trades.tail(100), use_container_width=True)
    else:
        st.info("No trades recorded yet in logs/trades.csv.")

    st.subheader("Active configuration")
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        st.json(json.load(fh))


# ---------------------------------------------------------------------- #
# Navigation                                                              #
# ---------------------------------------------------------------------- #
pages = st.navigation([
    st.Page(page_metrics, title="P&L Metrics", icon="📈", default=True),
    st.Page(page_parameters, title="Parameters", icon="🎛️"),
    st.Page(page_logs, title="Logs", icon="🧾"),
])
pages.run()
