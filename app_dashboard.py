"""Panel de control Streamlit del sistema de trading algorítmico.

Páginas (st.navigation):
  1. Métricas    - saldo, P&L diario, Sharpe, Max Drawdown, curva de equidad.
  2. Parámetros  - edición en caliente de config.json validada con Pydantic.
  3. Registro    - últimas operaciones (st.dataframe) y config activa (st.json).

Refresco: se compara el mtime del log de operaciones en cada rerun; si el
motor escribió nuevos datos, se invalida la caché y se recargan los gráficos
sin bucles bloqueantes.

Ejecución:  streamlit run app_dashboard.py
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
TRADES_LOG = BASE_DIR / "logs" / "trades.csv"      # escrito por el motor Lumibot
EQUITY_LOG = BASE_DIR / "logs" / "equity.csv"      # columnas: timestamp,equity

st.set_page_config(page_title="Multi-Asset Trading Bot", layout="wide")


# ---------------------------------------------------------------------- #
# Carga de datos con invalidación por mtime                               #
# ---------------------------------------------------------------------- #
def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(show_spinner=False)
def load_csv(path_str: str, mtime: float) -> pd.DataFrame:
    """`mtime` forma parte de la clave de caché: si el motor escribe el
    archivo, la clave cambia y Streamlit recarga desde disco."""
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def load_config() -> TradingConfig:
    return TradingConfig.load(CONFIG_PATH)


# ---------------------------------------------------------------------- #
# Métricas financieras                                                    #
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
# Página 1: Métricas                                                      #
# ---------------------------------------------------------------------- #
def page_metrics() -> None:
    st.title("Métricas financieras")
    equity = load_csv(str(EQUITY_LOG), _mtime(EQUITY_LOG))
    m = compute_metrics(equity)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Saldo neto de liquidación", f"${m['balance']:,.2f}")
    c2.metric("P&L del día", f"${m['pnl_day']:,.2f}", f"{m['pnl_day_pct']:+.2f}%")
    c3.metric("Ratio de Sharpe", f"{m['sharpe']:.2f}")
    c4.metric("Max Drawdown", f"{m['max_drawdown']:.2f}%")

    if not equity.empty:
        fig = px.line(equity, x="timestamp", y="equity",
                      title="Curva de equidad acumulada")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Aún no hay datos de equidad en logs/equity.csv.")


# ---------------------------------------------------------------------- #
# Página 2: Consola de parámetros (control en caliente)                   #
# ---------------------------------------------------------------------- #
def page_parameters() -> None:
    st.title("Consola de ajuste de parámetros")
    cfg = load_config()
    wheel, guards = cfg.wheel_parameters, cfg.risk_guards

    st.subheader("Estado del sistema")
    col_a, col_b, col_c = st.columns(3)
    active = col_a.toggle("Sistema activo", value=cfg.system_status.active)
    kill = col_b.toggle("Kill switch de emergencia",
                        value=cfg.system_status.emergency_kill_switch)
    live = col_c.toggle("Modo live trading (⚠ real)",
                        value=cfg.system_status.live_trading_mode)

    st.subheader("La Rueda (The Wheel)")
    delta_csp = st.slider("Delta objetivo CSP (put corta)", -0.50, -0.05,
                          float(wheel.delta_limit_csp), 0.01)
    delta_cc = st.slider("Delta objetivo Covered Call", 0.05, 0.50,
                         float(wheel.delta_limit_cc), 0.01)
    early_close = st.slider("Cierre anticipado (% de prima capturada)", 0.50, 0.95,
                            float(wheel.early_close_percentage_gain), 0.05)
    dte = st.slider("Ventana de expiración (días, DTE)", 7, 90,
                    (wheel.target_expiration_days_min,
                     wheel.target_expiration_days_max))
    whitelist = st.text_input("Whitelist de activos (separados por coma)",
                              ", ".join(wheel.whitelist_assets))

    st.subheader("Guardas de riesgo")
    max_risk = st.number_input("Riesgo máx. por operación (fracción de cartera)",
                               0.005, 0.10, float(guards.max_portfolio_risk_per_trade),
                               0.005, format="%.3f")
    daily_loss = st.number_input("Límite de pérdida diaria (fracción)",
                                 0.005, 0.20, float(guards.daily_loss_limit_percentage),
                                 0.005, format="%.3f")

    if st.button("Guardar configuración", type="primary"):
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
            # Revalidación completa antes de tocar el disco.
            validated = TradingConfig.model_validate(updated.model_dump())
            validated.save(CONFIG_PATH)
            st.success("config.json validado y guardado. El motor lo recargará en caliente.")
        except ValidationError as exc:
            st.error(f"Configuración rechazada por Pydantic:\n\n{exc}")


# ---------------------------------------------------------------------- #
# Página 3: Registro operativo                                            #
# ---------------------------------------------------------------------- #
def page_logs() -> None:
    st.title("Registro operativo y alertas")
    trades = load_csv(str(TRADES_LOG), _mtime(TRADES_LOG))
    if not trades.empty:
        st.dataframe(trades.tail(100), use_container_width=True)
    else:
        st.info("Sin operaciones registradas en logs/trades.csv.")

    st.subheader("Configuración activa")
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        st.json(json.load(fh))


# ---------------------------------------------------------------------- #
# Navegación                                                              #
# ---------------------------------------------------------------------- #
pages = st.navigation([
    st.Page(page_metrics, title="Métricas P&L", icon="📈", default=True),
    st.Page(page_parameters, title="Parámetros", icon="🎛️"),
    st.Page(page_logs, title="Registro", icon="🧾"),
])
pages.run()
