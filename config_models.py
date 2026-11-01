"""Compatibility shim: the model moved to core/config/models.py.

Only app_dashboard.py still imports from here; both go away when Streamlit is
retired (Session 7).
"""

from core.config.models import CONFIG_PATH, TradingConfig

__all__ = ["CONFIG_PATH", "TradingConfig"]
