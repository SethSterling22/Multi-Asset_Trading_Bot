# Multi-Asset Trading Bot

Plataforma híbrida multi-activo (acciones, opciones, cripto spot) sobre Lumibot.

## Módulos

| Archivo | Función |
|---|---|
| `config.json` | Estado y parámetros del sistema (fuente única de verdad) |
| `config_models.py` | Validación Pydantic v2 + persistencia atómica |
| `wheel_strategy.py` | Estrategia The Wheel (CSP → CC, cierre al 80%) vía Alpaca |
| `crypto_rebalancer.py` | Rebalanceo spot BTC/ETH/USDC con umbral ±5% |
| `app_dashboard.py` | Dashboard Streamlit: métricas, control en caliente, logs |

## Uso

```bash
pip install -r requirements.txt
cp .env.example .env        # completar claves API

python crypto_rebalancer.py          # demo dry-run del rebalanceador
python wheel_strategy.py             # motor Lumibot (paper por defecto)
streamlit run app_dashboard.py       # panel de control
```

## Seguridad

- Puts solo cash-secured; calls solo covered; cripto solo spot (sin margen ni cortos).
- `live_trading_mode: false` por defecto → paper trading. Mantener ≥60 días antes de pasar a real.
- Kill switch y límite de pérdida diaria en `config.json`, editables desde el dashboard.
- Credenciales solo por `.env` (excluido en `.gitignore`).
