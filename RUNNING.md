# Running the Bot — From Paper Trading to Live Capital

This is the operational runbook. Follow it in order. Phases are sequential
by design: skipping ahead is how accounts get damaged.

It covers the two existing strategies (`strategies/wheel/`,
`strategies/crypto_rebalance/`) and the legacy `app_dashboard.py`. The platform is
specified in [docs/INFORME_TALOS.md](docs/INFORME_TALOS.md); its phase gates
(§10) are the ones that decide whether anything touches real money. This
runbook gains the homelab, DR and incident runbooks in Session 10 of
[docs/PROMPTS_CLAUDE_CODE.md](docs/PROMPTS_CLAUDE_CODE.md).

---

## 0. Reality check — what works today

This repository is a **scaffold in migration**. Before planning a live
deployment, know exactly what is implemented and what still needs to be built.

| Component | Status |
|---|---|
| `core/config/models.py` — validation and atomic persistence | Works; bounds import the risk floor |
| `strategies/crypto_rebalance/` — rebalancing logic + live runner (`live.py`) | Runs against **Alpaca paper** through the order router; dry-run by default (D-014). Its first 2 orders filled on 2026-11-02; Alpaca's 0.25 % fee in kind is recorded since D-015 |
| `app_dashboard.py` — Streamlit panel | Reads CSVs that nothing writes; retired in Session 7 |
| `strategies/wheel/` — Wheel cycle and Lumibot adapter | Logic tested behind a fake broker. Its equity and cash come from its own budget (`WHEEL_BUDGET_USD`) and its own fills, not from the Alpaca account it shares with the crypto sleeve (D-016). Orders still go through Lumibot as market orders, not through the order router |
| Delta (`core/greeks.py`) | Broker greeks → Black-Scholes with IV → skip and log. Never guessed |
| Ledger (`fills`, `orders`, `equity_curve`) | Done (Phase 0.1); the Wheel writes fills and equity |
| Kill switch + breakers + pre-trade checks (`risk/`) | Done (Phase 0.5); 100% branch coverage |
| Order router + reconciliation (`execution/`) | Write-ahead intent, never resends on timeout, startup and per-cycle reconciliation that halts on mismatch. Tested with injected faults |
| Broker adapters | Alpaca Crypto paper (`brokers/alpaca_crypto.py`). Coinbase Advanced and perps: **not implemented** |
| Hedged spread strategy module | **Not implemented** (parameters exist in config) |
| LLM routing / analysis agents | **Not implemented** (Phase 3) |

**Session 0 audit gaps:** fixed in Session 2, each with a guard test (dead
circuit breaker hook, early close lost on restart, daily loss failing open,
per-order cash check, untracked pending orders, per-buy USDC cap, moneyness
delta). See README "Known gaps" for the list and what is still open.

**Blocking items before any live trading:**

1. Run the Wheel's Lumibot adapter in **paper** and check that fills reach the
   ledger and that `equity_curve` gets a snapshot every iteration. It has never
   run against a broker.
2. Replace the Wheel's market orders with limit orders and move it onto the order
   router (the crypto sleeve already uses it).
3. Run the crypto sleeve in paper (§4b) and check that its reconciliation stays
   clean. Real crypto money also needs the venue and eligibility checks of
   informe §13 (deferred to Phase 5, D-014).

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

- **[uv](https://docs.astral.sh/uv/)** — manages the environment and
  dependencies from `pyproject.toml` and `uv.lock`.
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

# Create .venv with Python 3.12 and install every dependency group
uv sync
uv run python --version            # must report 3.12.x

# Commit hooks: ruff + gitleaks
uv run pre-commit install

# Create the directory the dashboard reads from
mkdir -p logs
```

Prefix every command with `uv run`; it uses `.venv` without activating it.

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

Before relying on Coinbase or Kraken, confirm Puerto Rico is in each one's
supported regions. It is point 1 of the informe's §13 checklist and is still
unverified.

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
leaked keys within minutes. The gitleaks commit hook is the backstop. `.env` on
disk is temporary: Phase 1.2 replaces it with SOPS + age decrypted to tmpfs.

### 3.4 Confirm paper mode

Open `config.json` and verify:

```json
"system_status": {
  "active": true,
  "emergency_kill_switch": false,
  "live_trading_mode": false
}
```

`live_trading_mode: false` is what keeps the Wheel pointed at
Alpaca's paper endpoint and the rebalancer in dry-run.

---

## 4. Smoke tests — verify before trading anything

Run these in order. Each one should pass before moving on.

```bash
# 0. Guard tests, lint and types (the same checks CI and merges require)
uv run pytest -m guards
uv run ruff check .
uv run mypy core risk
# Expected: all green. If a guard test fails, the change is wrong, not the test.
```

```bash
# 0b. Ledger against a local Postgres + TimescaleDB (dev only, localhost:5433)
docker compose -f docker/compose.dev.yml up -d
export TEST_DATABASE_URL=postgresql+psycopg://trading@127.0.0.1:5433/postgres
uv run pytest -m db
uv run python -m scripts.synthetic_fills --database-url "$TEST_DATABASE_URL"
# Expected: all green, and the script ends with "todo idéntico" and
#           "transacción deshecha": it never leaves the synthetic fills behind.
```

```bash
# 1. Configuration is valid and parses
uv run python -m core.config.models
# Expected: "config.json is valid ✔" followed by the parsed config
```

```bash
# 2. Rebalancing logic runs against the simulated broker
uv run python -m strategies.crypto_rebalance
# Expected: JSON log lines with mode "dry_run" for the drifted demo portfolio,
#           then a total value line. No orders executed.
```

```bash
# 3. Dashboard launches
uv run streamlit run app_dashboard.py
# Opens http://localhost:8501
# Metrics will read zero until the log writer exists — that is expected.
# Confirm all three pages load and the Parameters page saves without error.
```

Test the validation layer deliberately: on the Parameters page, try saving a
covered-call delta and a CSP delta that violate the model rules. The panel
should reject the save with a Pydantic error rather than writing bad data.
If it writes anyway, stop and fix that before proceeding.

### Derivatives archive (Coinalyze)

The archive only stays complete if it runs at least every 12 hours: Coinalyze
deletes intraday data older than ~25 h. Run it in a terminal that stays open
(or under tmux), every 6 hours:

```bash
export DATABASE_URL=postgresql+psycopg://trading@127.0.0.1:5433/trading
uv run python -m data.ingest.coinalyze run --every-hours 6
```

Check it with `uv run python -m data.ingest.coinalyze report --hours 48`; exit code
0 means no gaps and no lag. Holes "ausentes en la fuente" are minutes Coinalyze
itself does not have (Kraken Futures has a few a day); they are reported, not
filled.

---

## 4b. Running on Sadida (Proxmox homelab node)

Interim setup until the full Phase 1 compose (DECISIONS.md D-012): containers on the
Proxmox host's Docker, no VM. It runs the paper Wheel and the Coinalyze archiver
continuously; it is **not** ready for real money (no SOPS, no off-host backups, no
external dead-man's switch yet).

**Before you start:** merge PRs #6, #7 and #8, and check what `/mnt/data` is
(`lsblk -d -o NAME,ROTA,MODEL,SIZE,TRAN`; `ROTA 0` is SSD/NVMe).

All commands run on Sadida, as root.

1. Get the code and the secrets in place:
   ```bash
   git clone https://github.com/SethSterling22/Multi-Asset_Trading_Bot.git /opt/trading
   cd /opt/trading
   install -d -m 700 /etc/trading
   openssl rand -hex 24 > /etc/trading/pg_password && chmod 444 /etc/trading/pg_password
   install -m 600 docker/trading.env.example /etc/trading/env
   nano /etc/trading/env        # PAPER Alpaca keys and the Coinalyze key
   install -m 644 docker/compose.env.example /etc/trading/compose.env
   install -d -m 700 /mnt/data/trading
   # Postgres runs as uid 1000 inside its image; it must own its data directory.
   install -d -o 1000 -g 1000 -m 700 /mnt/data/trading/postgres
   ```
   `pg_password` is world-readable on purpose, inside a `700` directory. Outside
   Swarm, a compose secret is a bind mount that keeps the host file's permissions,
   and neither Postgres (uid 1000) nor the app (uid 10001) runs as root; the
   directory is what keeps other host users out. Postgres failing with
   `/run/secrets/pg_password: Permission denied` means this step was missed.
   After fixing it, recreate the container: one left `unhealthy` by earlier failed
   starts makes `up -d` fail immediately, before checking again.
   ```bash
   docker compose -f docker/compose.runtime.yml --env-file /etc/trading/compose.env \
     up -d --force-recreate --wait postgres
   ```
2. Build and start everything (migrations run first, on their own):
   ```bash
   docker compose -f docker/compose.runtime.yml --env-file /etc/trading/compose.env up -d --build
   docker compose -f docker/compose.runtime.yml --env-file /etc/trading/compose.env ps
   ```
3. *(Optional; Sadida has a battery backup, so steps 3–4 can be skipped, D-014.)*
   Limit the battery charge if the firmware allows it (a lithium battery at 100% day
   and night swells):
   ```bash
   ls /sys/class/power_supply/AP21A7T/charge_control_end_threshold && \
     echo 80 > /sys/class/power_supply/AP21A7T/charge_control_end_threshold
   ```
4. *(Optional.)* Install the battery watcher (laptop battery as UPS):
   ```bash
   install -m 755 ops/power/battery-watch.sh /usr/local/sbin/trading-battery-watch
   install -m 644 ops/power/trading-battery-watch.service /etc/systemd/system/
   echo 'COMPOSE_FILE=/opt/trading/docker/compose.runtime.yml' > /etc/trading/battery-watch.conf
   systemctl daemon-reload && systemctl enable --now trading-battery-watch
   ```
   After 2 min on battery it creates `/run/trading/HALT` (the Wheel's kill switch); at
   25% it stops the containers cleanly. When power returns, **lift the kill switch
   yourself** with `rm /run/trading/HALT`. A host reboot clears it, because `/run` is
   tmpfs.

### The crypto sleeve (Alpaca paper)

It rebalances BTC and ETH every hour (`config.json`, `crypto_parameters`) inside its
own budget, through the order router: pre-trade checks, kill switch, write-ahead
intent, and a reconciliation against Alpaca before every cycle. Any mismatch
between the ledger and Alpaca sets the kill switch for `crypto_rebalance` and the
sleeve stops until you look at it.

1. Preferably create a **second Alpaca paper account** for it: the first one's cash
   is the Wheel's. In `/etc/trading/env` set `ALPACA_CRYPTO_API_KEY`,
   `ALPACA_CRYPTO_API_SECRET` and `CRYPTO_BUDGET_USD` (see
   `docker/trading.env.example`). Leave `CRYPTO_DRY_RUN=true` for now.
2. Start it in dry-run. It plans and logs, sends nothing:
   ```bash
   C="docker compose -f docker/compose.runtime.yml --env-file /etc/trading/compose.env"
   $C --profile crypto up -d --build crypto
   $C logs -f crypto              # look for order_dry_run lines and cycle_done
   ```
3. When the dry-run plans look right, set `CRYPTO_DRY_RUN=false` in `/etc/trading/env`
   and recreate it with `$C --profile crypto up -d crypto`. From then on it sends paper
   orders.

If a mismatch comes from fees taken in kind on fills recorded before D-015, correct
them with the tool. It shows what it would write unless you pass `--apply`, and it
lifts the kill switch only if the ledger then matches Alpaca:
```bash
$C --profile crypto run --rm --no-deps crypto python -m execution.fee_correction
$C --profile crypto run --rm --no-deps crypto python -m execution.fee_correction --apply --lift-kill-switch --actor seth
```

For any other mismatch, check what did not match, fix the cause, and only then lift the
kill switch (`risk_events` is append-only: lifting it is a new row, not a delete):
```bash
$C exec postgres psql -U trading -d trading -c \
  "SELECT ts, detail FROM reconciliation_runs ORDER BY ts DESC LIMIT 3"
$C exec postgres psql -U trading -d trading -c \
  "INSERT INTO risk_events (kind, scope, reason, actor)
   VALUES ('kill_switch_off', 'crypto_rebalance', 'mismatch revisado: <qué era>', 'seth')"
```

### The dashboard

It listens on Sadida's `127.0.0.1:8000`. Publish it on your tailnet with HTTPS:

```bash
tailscale serve --bg 8000
```

Then open `https://<sadida>.<tailnet>.ts.net` from any device on the tailnet. To
listen directly on Sadida's Tailscale IP instead, set `DASHBOARD_BIND=100.x.y.z` in
`/etc/trading/compose.env` and recreate the container. The dashboard is read-only
and has no broker keys; the tailnet is its only access control (D-013).

What each view is for (all times UTC):

| View | Answers |
|---|---|
| Header | Paper or live; whether any kill switch is on (global **or any sleeve**) and why; total equity, today's realized P&L, worst drawdown and which sleeve it belongs to |
| Live | Each sleeve's equity over 7 days on its own scale, with ▼ at every fill; today's fills |
| P&L por estrategia | Realized P&L since the start, gross → fees → funding → slippage → net, plus the unrealized P&L of open positions |
| Historial | One row per day and sleeve: equity change split into realized net, change in unrealized, change in allocated capital (not a gain or loss), and "outside the ledger". Click a day for the price move of each open position (why the unrealized changed), its fills, orders (with rejection reasons), reconciliations, risk events and snapshots. CSV export: `/export/historial.csv`, `/export/fills.csv?day=YYYY-MM-DD` |
| Operaciones | Every entry paired with its exit (FIFO): price, amount paid and received (fees included), the reason the strategy gave for each order, net P&L and holding time. Open ones are valued at the last mark. CSV export: `/export/operaciones.csv` |
| Mercado | The Coinalyze archive: open interest, funding, liquidations per venue |
| Salud | Kill-switch state per scope, with the last event, who set it and why; archive coverage |
| Riesgo | Open positions from the ledger and the risk-floor limits in force |

"Outside the ledger" is the part of a day's equity change that the sleeve's own fills do
not explain. It should be ~0. Today it is not for the Wheel: it takes its equity from
the whole Alpaca account, so the crypto sleeve's moves in the same account land there.

Day to day (`C="docker compose -f docker/compose.runtime.yml --env-file /etc/trading/compose.env"`):

```bash
$C logs -f wheel                      # the Wheel
$C --profile crypto logs -f crypto     # the crypto sleeve
$C logs --tail 20 archiver            # last archiver runs
$C run --rm --no-deps archiver python -m data.ingest.coinalyze report --hours 48
touch /run/trading/HALT               # kill switch: no new orders
git pull && $C up -d --build          # deploy a new version
$C logs --tail 20 dashboard           # the dashboard
nano config.json                      # takes effect in the Wheel without rebuilding
```

`run --rm --no-deps` (not `exec`) goes through the entrypoint, which is what sets
`DATABASE_URL` and `REDIS_URL` from the secret.

Re-run the backtest once on Sadida so the N registry lives there too; the same 20
configurations get the same hashes, so N stays 20 (README, "Backtest"). The single
quotes matter: `$DATABASE_URL` has to expand inside the container, not on the host.

```bash
$C run --rm --no-deps archiver sh -c 'python -m backtest --start 2023-11-01 \
  --end 2026-10-31 --fee-bps 60 --slippage-bps 5 --database-url "$DATABASE_URL"'
```

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

Count every parameter set you try, including the ones you discard. A plain
Sharpe from the best of N variants is inflated by N (informe §2.4). The
`backtest/` module of Phase 0.7 records N and reports Deflated Sharpe and PBO
instead of Sharpe; until it exists, treat any Lumibot backtest as a smoke test
of the logic, not as evidence of an edge.

---

## 6. Phase 2 — Paper trading (minimum 60 days)

This phase is not optional and the duration is not arbitrary. Sixty days
covers at least two options expiration cycles, which is the minimum needed to
observe a full Wheel rotation including assignment.

### Launch

The Wheel needs the ledger (Postgres) and the kill switch's Redis source, and
refuses to start without them (§4, step 0b brings both up):

```bash
export DATABASE_URL=postgresql+psycopg://trading@127.0.0.1:5433/trading
export REDIS_URL=redis://127.0.0.1:6380/0
DATABASE_URL=$DATABASE_URL uv run alembic upgrade head
uv run python -m strategies.wheel
```

To keep it running after closing the terminal:

```bash
# Simple approach
mkdir -p logs
nohup uv run python -m strategies.wheel > logs/engine.log 2>&1 &

# Better for long-running deployments: a systemd service (Linux)
# or a process manager such as supervisor / pm2. Phase 1 moves this to
# Docker Compose on the homelab.
```

Run the dashboard in a second terminal:

```bash
uv run streamlit run app_dashboard.py
```

### Daily routine (5 minutes)

- Check the dashboard: balance, daily P&L, open positions.
- Scan `logs/engine.log` for `WARNING` and `ERROR` lines.
- Confirm the engine is still alive (`ps aux | grep strategies.wheel`).

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
  the session closes. The cash-secured check is per order, not aggregate, so
  with several whitelisted symbols it may not prevent this. Watch for it.
- **Contract selection.** Are the strikes and deltas the bot picks what you
  would have picked manually? This is where the delta placeholder shows up.
- **Guard behavior.** Did the daily loss limit ever trigger? Did the kill
  switch stop everything when you tested it? (Test it deliberately.) The
  daily loss limit is blind on any day the engine started after the open.
- **Restarts.** After every restart, check whether open short options still
  get their 80% early close. They don't today (premiums are in memory).

---

## 7. Go / No-Go checklist before live capital

Every item must be true. If any is false, stay in paper.

This list is the Wheel-specific minimum. The gate that governs is the Phase 4
table in [docs/INFORME_TALOS.md](docs/INFORME_TALOS.md) §10 (zero unexplained
reconciliation mismatches, zero restarts with lost state, a timed restore
game-day, fixed cost under 4% a year of capital, and the rest); every row must
be green as well. That gate is operational, not statistical: 60–90 days cannot
prove an edge exists, only that the system does not break.

- [ ] 60+ consecutive days of paper operation completed
- [ ] At least two full expiration cycles observed, including one assignment
- [ ] The delta placeholder has been replaced with a real calculation
- [ ] Every gap in §0 is fixed and covered by a test in `tests/guards/`
- [ ] The ledger is implemented and the dashboard shows real, net metrics
- [ ] Every safety guard has been observed firing correctly at least once
      (cash-secured rejection, covered-call limit, daily loss limit, kill switch)
- [ ] Zero unexplained crashes or unhandled exceptions in the last 30 days
- [ ] You can explain, without reading the code, why the bot took each of its
      last ten trades
- [ ] Max drawdown observed in paper is one you could tolerate in real money
- [ ] Capital allocated is money you can lose entirely without consequence
- [ ] Emergency fund funded and high-interest debt cleared first
- [ ] You have consulted a Puerto Rico tax professional about options income,
      assignments, Act 60 / Act 38-2026 and §1256 (informe §3.4 and §13
      points 2–3; the first may have a near deadline)

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
living through it. For the new crypto sleeves the staging is fixed in the
informe's Phase 5: $500, then $2,000, then $10,000, each step with its own
condition to move on.

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
   uv run python -m strategies.wheel
   ```

   `live_trading_mode` is only read at startup. Until you restart, the engine
   keeps using the endpoint it started with, whatever the dashboard shows.

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
default) and stops submitting orders. For an immediate stop, stop the process:
Ctrl+C in its terminal, or from another one:

```bash
pkill -f strategies.wheel        # SIGTERM: orderly stop, forced after 30 s
```

Both end the process (`strategies/runtime.py`); a second signal forces it at
once. Stopping the process does not close positions: the protective stops live
on the exchange.

Note that the kill switch stops *new* orders; it does not close existing
positions. Closing them is a manual decision.

The kill switch is fail-closed and triple redundant (CLAUDE.md §1.7): it is
on if **any** of these says so, and an error while checking one counts as on.
Any of them stops new orders on the next iteration:

```bash
sudo mkdir -p /run/trading && sudo touch /run/trading/HALT     # everything
sudo touch /run/trading/HALT.wheel                            # only the Wheel
redis-cli -p 6380 SET trading:halt 1                          # everything
psql -h 127.0.0.1 -p 5433 -U trading -d trading -c \
  "INSERT INTO risk_events (kind, scope, reason, actor)
   VALUES ('kill_switch_on', 'global', 'manual', 'human:sebastian')"
```

The `config.json` flag from the dashboard is a fourth source on top of those
three. To lift a stop, remove the file or the key, or insert a
`kill_switch_off` row for the same scope. Never delete a `risk_events` row: the
table is append-only and the database refuses.

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
| `ModuleNotFoundError` | Command run without `uv run`, or `uv sync` not run after pulling |
| `RuntimeError: DATABASE_URL no está definida` | Alembic needs `DATABASE_URL` in the environment; it is never read from `alembic.ini` |
| `TEST_DATABASE_URL no está definida` | The `db` tests need the dev database (§4, step 0b); they fail rather than skip. Use `pytest -m "not db"` to exclude them on purpose |
| `permission denied ... docker.sock` | The Docker daemon is stopped or your user is not in the `docker` group. Start it yourself; membership of `docker` is root-equivalent |
| `RuntimeError: RISK FLOOR MUTADO EN RUNTIME` | Something patched a constant in `core/risk_floor.py` at runtime. Stop and find it; never work around it |
| `KeyError: 'ALPACA_API_KEY'` | `.env` missing or not loaded; confirm the file exists and the variable name matches |
| `ValidationError` on startup | `config.json` violates a model rule — the error names the offending field |
| Engine runs but never trades | Check `active`, `emergency_kill_switch`, whether the daily loss limit fired, and whether cash covers the collateral for your whitelist |
| "CSP rejected: cash < collateral" | Working as intended — your account cannot cover strike × 100 for that symbol |
| Dashboard shows all zeros | Nothing writes `logs/equity.csv`; the ledger replaces it in Phase 0.1 |
| Orders rejected by Alpaca | In live: options approval level insufficient, or insufficient buying power |
| Engine died overnight | Check `logs/engine.log` for the traceback; consider a process manager with auto-restart |

---

## 11. Non-negotiables

- `live_trading_mode` defaults to `false`. Changing it is always a deliberate,
  manual act — never automated.
- Never commit `.env`. Never share API keys.
- Coinbase keys: View + Trade only, never Transfer.
- No naked options. Leverage and shorts only within `core/risk_floor.py`:
  at most 1.5× and shorts only in BTC/ETH perps; alts are spot at 1×, so
  long/flat. These are structural properties of the system, not preferences.
  The floor is Python constants, never config; changing it takes a commit and a
  review.
- Anything an LLM does can only reduce risk (CLAUDE.md §1.2).
- `tests/guards/` must pass on every change. If a change breaks one, the change
  is wrong.
- Backtest, then paper, then live, for any code change, including changes that
  look trivial.
- The kill switch must remain reachable by a human at all times.

This software is a technical tool, not financial advice. Options selling
produces steady income until the event that was not in the plan. No amount of
validation protects against a market in panic.
