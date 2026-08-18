# Running the Bot — From Paper Trading to Live Capital

This is the operational runbook. Follow it in order. Phases are sequential
by design: skipping ahead is how accounts get damaged.

---

## 0. Reality check — what works today

This repository is a **scaffold**. Before planning a live deployment, know
exactly what is implemented and what still needs to be built.

| Component | Status |
|---|---|
| `config_models.py` — validation and atomic persistence | Complete |
| `crypto_rebalancer.py` — rebalancing logic + `PaperSpotBroker` | Complete (simulated broker only) |
| `app_dashboard.py` — Streamlit panel | Complete (reads CSVs that nothing writes yet) |
| `wheel_strategy.py` — Wheel lifecycle and safety guards | Complete, with one caveat below |
| Delta estimation from broker greeks | **Fallback is a crude placeholder — must be replaced** |
| Live broker adapters (Alpaca Crypto / Coinbase Advanced) | **Not implemented** |
| Log writer (`logs/equity.csv`, `logs/trades.csv`) | **Not implemented** |
| Hedged spread strategy module | **Not implemented** (parameters exist in config) |
| LLM routing / analysis agents | **Not implemented** |

**Blocking items before any live trading:**

1. Replace `_estimate_delta()`'s no-greeks fallback in `wheel_strategy.py`.
   The current approximation is a rough moneyness proxy, not a real delta.
   Either confirm your broker always returns greeks, or compute them
   properly (Black-Scholes with implied volatility).
2. Implement the log writer, or the dashboard will stay empty and you will
   have no performance record to evaluate.
3. Implement a real `SpotBroker` adapter if you intend to trade crypto.

Everything below assumes these are addressed before Phase 3.

---

## 1. Prerequisites

- **Python 3.12** (`python3 --version`)

  Do not use 3.13 or 3.14. Lumibot declares `requires-python >=3.10` but only
  classifies support through 3.12, and newer builds — particularly those
  compiled from source by version managers such as mise or pyenv — frequently
  ship without required C extensions, producing errors like
  `ModuleNotFoundError: No module named '_decimal'` or `'math'`. Those are
  symptoms of an incomplete Python build, not a missing package.

  ```bash
  mise install python@3.12 && mise use python@3.12   # if using mise
  pyenv install 3.12 && pyenv local 3.12             # if using pyenv
  ```

- **git** (optional, for version control)
- A machine that can stay online during market hours — your own computer is
  fine for paper trading; consider a small VPS for live operation.
- Accounts (created in Section 3): Alpaca, and Coinbase only if you plan to
  trade crypto.

No capital is required for Phases 1 and 2.

---

## 2. Installation

```bash
cd /path/to/Multi-Asset_Trading_Bot

# Create an isolated environment (keeps this project's packages separate)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Confirm the interpreter before installing anything
python --version                   # must report 3.12.x

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Create the directory the dashboard reads from
mkdir -p logs
```

Re-activate the environment (`source .venv/bin/activate`) in every new
terminal session before running anything.

---

## 3. Account and credential setup

### 3.1 Alpaca (stocks and options)

1. Register at **alpaca.markets** and choose **Trading API** (not Broker API —
   that one is for firms opening accounts on behalf of clients).
2. Complete KYC. There is no minimum deposit to open the account.
3. Once approved, open the dashboard. In the top-left corner, confirm you are
   on the **paper** account (you can hold up to 3 paper accounts and 1 live).
4. Generate API keys **for the paper account**. The secret is shown once —
   copy it immediately.
5. Options are enabled by default in paper. No approval needed to test.

### 3.2 Coinbase (crypto — skip if not trading crypto)

1. Create a standard account at **coinbase.com** and complete KYC.
2. Go to the **Coinbase Developer Portal** (portal.cdp.coinbase.com), signing
   in with that same account. This is not a separate brokerage account — it is
   where API keys are issued against your existing portfolio.
3. Navigate to **Access → API keys** and create a key:
   - Permissions: **View + Trade only. Never enable Transfer.** If the key
     leaks, an attacker could trade but could not withdraw funds.
   - Key type: **Ed25519** (currently recommended).
   - Enable the **IP allowlist** if the bot will run from a fixed address.
4. Complete 2FA. The credentials download as a JSON file, shown only once.
5. For the paper phase, generate **separate sandbox keys** — sandbox and
   production credentials are not interchangeable.

### 3.3 Store credentials

```bash
cp .env.example .env
```

Edit `.env` and paste the values:

```
ALPACA_API_KEY=your_paper_key_here
ALPACA_API_SECRET=your_paper_secret_here
COINBASE_CDP_API_KEY_NAME=
COINBASE_CDP_PRIVATE_KEY=
```

`.env` is already listed in `.gitignore`. Never commit it, never paste keys
into chat, screenshots, or issue trackers. Bots scan public repositories for
leaked keys within minutes.

### 3.4 Confirm paper mode

Open `config.json` and verify:

```json
"system_status": {
  "active": true,
  "emergency_kill_switch": false,
  "live_trading_mode": false
}
```

`live_trading_mode: false` is what keeps `wheel_strategy.py` pointed at
Alpaca's paper endpoint and the rebalancer in dry-run.

---

## 4. Smoke tests — verify before trading anything

Run these in order. Each one should pass before moving on.

```bash
# 1. Configuration is valid and parses
python config_models.py
# Expected: "config.json is valid ✔" followed by the parsed config
```

```bash
# 2. Rebalancing logic runs against the simulated broker
python crypto_rebalancer.py
# Expected: DRY-RUN log lines for the drifted demo portfolio,
#           then a total value line. No orders executed.
```

```bash
# 3. Dashboard launches
streamlit run app_dashboard.py
# Opens http://localhost:8501
# Metrics will read zero until the log writer exists — that is expected.
# Confirm all three pages load and the Parameters page saves without error.
```

Test the validation layer deliberately: on the Parameters page, try saving a
covered-call delta and a CSP delta that violate the model rules. The panel
should reject the save with a Pydantic error rather than writing bad data.
If it writes anyway, stop and fix that before proceeding.

---

## 5. Phase 1 — Backtesting

Backtest before paper trading. It costs nothing and filters out broken logic
quickly. Lumibot can pull free historical data without a broker connection.

Consult the Lumibot documentation for the backtesting entry point that matches
your installed version, then run the Wheel across at least one full market
cycle — including a drawdown period such as 2022 — not just a bull run.

What to look for:

- Does the assignment → covered call transition actually fire?
- Does the 80% early close trigger, and how often?
- What is the max drawdown, and could you tolerate living through it?
- How does it behave when a held stock keeps falling after assignment?

**Warning signs of overfitting:** a Sharpe above 3, almost no losing months,
or results that collapse when you shift the date range by a few months. A
backtest that looks perfect is usually measuring your parameter tuning, not
the strategy.

---

## 6. Phase 2 — Paper trading (minimum 60 days)

This phase is not optional and the duration is not arbitrary. Sixty days
covers at least two options expiration cycles, which is the minimum needed to
observe a full Wheel rotation including assignment.

### Launch

```bash
source .venv/bin/activate
python wheel_strategy.py
```

To keep it running after closing the terminal:

```bash
# Simple approach
nohup python wheel_strategy.py > logs/engine.log 2>&1 &

# Better for long-running deployments: a systemd service (Linux)
# or a process manager such as supervisor / pm2.
```

Run the dashboard in a second terminal:

```bash
streamlit run app_dashboard.py
```

### Daily routine (5 minutes)

- Check the dashboard: balance, daily P&L, open positions.
- Scan `logs/engine.log` for `WARNING` and `ERROR` lines.
- Confirm the engine is still alive (`ps aux | grep wheel_strategy`).

### Weekly routine (30 minutes)

- Record the week's numbers in a journal: P&L, trades opened and closed,
  assignments, any order rejected by a safety guard.
- Compare what the bot did against what you expected it to do. Every
  divergence is either a bug or a gap in your understanding — investigate
  both.
- Verify the guards fired correctly whenever conditions warranted.

### What to watch for specifically

- **Assignments.** When a CSP is assigned, does the bot correctly switch to
  selling covered calls on the resulting shares?
- **Alpaca's pre-expiration liquidation.** If an ITM option approaches expiry
  without sufficient buying power, Alpaca liquidates roughly an hour before
  the session closes. Your cash-secured check should prevent this — confirm
  it does.
- **Contract selection.** Are the strikes and deltas the bot picks what you
  would have picked manually? This is where the delta placeholder shows up.
- **Guard behavior.** Did the daily loss limit ever trigger? Did the kill
  switch stop everything when you tested it? (Test it deliberately.)

---

## 7. Go / No-Go checklist before live capital

Every item must be true. If any is false, stay in paper.

- [ ] 60+ consecutive days of paper operation completed
- [ ] At least two full expiration cycles observed, including one assignment
- [ ] The delta placeholder has been replaced with a real calculation
- [ ] The log writer is implemented and the dashboard shows real metrics
- [ ] Every safety guard has been observed firing correctly at least once
      (cash-secured rejection, covered-call limit, daily loss limit, kill switch)
- [ ] Zero unexplained crashes or unhandled exceptions in the last 30 days
- [ ] You can explain, without reading the code, why the bot took each of its
      last ten trades
- [ ] Max drawdown observed in paper is one you could tolerate in real money
- [ ] Capital allocated is money you can lose entirely without consequence
- [ ] Emergency fund funded and high-interest debt cleared first
- [ ] You have consulted a tax professional about your local treatment of
      options income and assignments

---

## 8. Phase 3 — Going live

### 8.1 Options approval

Live options trading requires approval under FINRA Rule 2360 — paper does not.
Apply inside Alpaca:

- **Level 1** covers covered calls and cash-secured puts. This is all the
  Wheel needs.
- **Level 3** is required for spreads and multi-leg strategies. Only request
  it if you have implemented and paper-tested the spread module.

Answer the suitability questionnaire honestly; it determines the level granted.

### 8.2 Fund the account

Transfer only the capital you decided on — and start below that. If your plan
allows $20,000, begin with a fraction of it. The gap between a simulated
drawdown and a real one is psychological, and it can only be measured by
living through it.

### 8.3 Switch to live

1. Generate **live** API keys in Alpaca (separate from your paper keys).
2. Update `.env` with the live credentials. Keep the paper keys somewhere
   safe — you will want to return to paper for testing changes.
3. Set live mode. Prefer doing this through the dashboard's Parameters page
   so the change is validated and written atomically:

   `Parameters → System status → Live trading mode → Save configuration`

   Or edit `config.json` directly:

   ```json
   "live_trading_mode": true
   ```

4. Restart the engine so it reconnects to the live endpoint:

   ```bash
   # Stop the running process, then
   python wheel_strategy.py
   ```

5. Watch the first trade end to end before walking away.

### 8.4 Scale gradually

Add capital only after several weeks in which live behavior matches what
paper trading predicted. If live results diverge from paper, find out why
before adding a dollar — the difference is usually slippage, fills, or an
assumption that only held in simulation.

---

## 9. Day-to-day operations

### Emergency stop

The fastest way to halt everything:

**Dashboard → Parameters → Emergency kill switch → Save configuration**

The engine picks up the change on its next iteration (within 15 minutes by
default) and stops submitting orders. For an immediate stop, kill the process:

```bash
pkill -f wheel_strategy.py
```

Note that the kill switch stops *new* orders; it does not close existing
positions. Closing them is a manual decision.

### Adjusting parameters live

Edit through the dashboard, never by hand-editing `config.json` while the
engine runs. The dashboard validates with Pydantic and writes atomically; the
engine detects the file's modification time and hot-reloads without a restart.

### Logs

```bash
tail -f logs/engine.log              # live engine output
grep -E "WARNING|ERROR" logs/engine.log
```

### Backups

Keep a copy of `config.json` and your log files. The logs are your only
performance record and your only forensic trail when something goes wrong.

---

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ModuleNotFoundError` | Virtual environment not activated, or `pip install -r requirements.txt` not run |
| `KeyError: 'ALPACA_API_KEY'` | `.env` missing or not loaded; confirm the file exists and the variable name matches |
| `ValidationError` on startup | `config.json` violates a model rule — the error names the offending field |
| Engine runs but never trades | Check `active`, `emergency_kill_switch`, whether the daily loss limit fired, and whether cash covers the collateral for your whitelist |
| "CSP rejected: cash < collateral" | Working as intended — your account cannot cover strike × 100 for that symbol |
| Dashboard shows all zeros | The log writer is not implemented yet; `logs/equity.csv` does not exist |
| Orders rejected by Alpaca | In live: options approval level insufficient, or insufficient buying power |
| Engine died overnight | Check `logs/engine.log` for the traceback; consider a process manager with auto-restart |

---

## 11. Non-negotiables

- `live_trading_mode` defaults to `false`. Changing it is always a deliberate,
  manual act — never automated.
- Never commit `.env`. Never share API keys.
- Coinbase keys: View + Trade only, never Transfer.
- No leverage, no shorts, no naked options. These are structural properties of
  the system, not preferences — do not add them.
- Paper trade any code change before it touches live capital, including
  changes that look trivial.
- The kill switch must remain reachable by a human at all times.

This software is a technical tool, not financial advice. Options selling
produces steady income until the event that was not in the plan. No amount of
validation protects against a market in panic.
