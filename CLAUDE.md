# CLAUDE.md — Polymarket Bot Codebase Guide

This file provides AI assistants (Claude, Copilot, etc.) with the context needed to navigate, modify, and extend this codebase safely and effectively.

---

## Overview

This is an async Python trading bot for [Polymarket](https://polymarket.com), a decentralized prediction market exchange. The bot runs multiple concurrent strategies against Polymarket's CLOB (Central Limit Order Book) API, with a comprehensive risk management stack.

**Key design principles:**
- **Risk-first**: Every order passes through circuit breakers, kill-switches, and edge checks.
- **Operator control**: No automated promotion from paper→live; all critical config changes are manual.
- **Gradual deployment**: Paper → micro-live → scale, with explicit staged caps.
- **Supervised AI loop**: Claude-powered parameter proposals feed into an operator review step — no auto-deployment.

---

## Repository Structure

```
polymarket-bot/
├── bot.py                   # Main orchestrator; starts all async tasks
├── client.py                # PolymarketClient CLOB API wrapper (aiohttp)
├── config.py                # Env var loading (no function calls, read-only after load)
├── health_state.py          # Bot-wide health FSM (NORMAL→DEGRADED→SAFE_MODE→CIRCUIT_BREAKER)
├── dashboard.py             # Rich console stats dashboard
├── preflight.py             # Startup validation; blocks launch on config errors
├── auth_probe.py            # Auth testing utility (standalone)
│
├── strategies/              # Core trading strategies
│   ├── price_arb.py         # Buy YES+NO when sum < $1.00
│   ├── latency_arb.py       # Exploit near-expiry BTC threshold inefficiencies
│   ├── market_maker.py      # Two-sided quotes on reward-earning markets
│   └── wallet_clone.py      # Mirror behavioral patterns of a target wallet
│
├── risk/                    # Risk management stack
│   ├── sizing.py            # PositionSizer: edge/confidence/vol-based sizing
│   ├── clone_sizer.py       # Profile-matched dynamic sizer for clone strategy
│   ├── risk_engine.py       # Vol-targeting, exposure caps, daily loss circuit breaker
│   ├── kill_switch.py       # 3-tier escalation: API errors → stale data → drawdown pace
│   └── param_profiles.py   # Regime-adaptive profiles (conservative/balanced/aggressive)
│
├── execution/               # Order execution
│   ├── execution_manager.py # Edge check, adaptive maker/taker, timeout handling
│   └── live_realism.py      # Throttle, order book depth/spread stability gates
│
├── signals/
│   └── signal_engine.py     # Multi-factor: microstructure + momentum + mean-reversion
│
├── feeds/
│   └── price_feed.py        # BTC/USD: Binance WS primary, Coinbase REST fallback
│
├── analytics/               # Post-session analysis and parameter optimization
│   ├── performance_report.py
│   ├── fill_quality.py
│   ├── param_optimizer.py   # Grid/random search over env vars
│   ├── walk_forward.py      # Overfit detection and stability validation
│   ├── trade_logger.py      # Writes logs/quant_trades.csv
│   ├── clone_profile.py     # Wallet behavior profiling + archetype clustering
│   ├── clone_backtest.py    # Replay clone strategy against historical trades
│   └── live_proxy_validator.py
│
├── agent_loop/              # Supervised AI improvement cycle
│   ├── orchestrator.py      # ingest → propose → validate → scorecard → plan
│   ├── controller.py        # Auto-tuning within policy bounds → logs/applied_params.json
│   ├── propose_patch.py     # Generate parameter patch from optimizer output
│   ├── validate.py          # Replay trades through proposed params
│   ├── scorecard.py         # Compute metrics; decide pass/fail gate
│   ├── deploy.py            # Write logs/deployment_plan.json (no auto-apply)
│   ├── policy.py            # Policy constraint enforcement
│   └── controller_policy.json  # Tunable allowlist + forbidden param list
│
├── scripts/
│   ├── clone_extract.py     # Fetch historical trades from Polymarket public API
│   ├── hf_preflight.py      # HF mode pre-launch validation
│   └── live_staircase.py    # Staged live deployment with confirmation prompts
│
├── requirements.txt         # 8 Python packages
├── .env.example             # All config variables with documentation
└── run_*.ps1                # PowerShell launchers (Windows; 14 scripts)
```

---

## Entry Points

| Command | Description |
|---|---|
| `python bot.py` | Run bot (reads all config from env) |
| `python -m analytics.performance_report` | Post-session P&L, Sharpe, win rate |
| `python -m analytics.fill_quality` | Fill ratio, adverse selection, latency |
| `python -m analytics.param_optimizer` | Grid/random parameter search |
| `python -m analytics.walk_forward` | Overfit detection across windows |
| `python -m agent_loop.orchestrator cycle` | Run one supervised improvement cycle |
| `python -m agent_loop.orchestrator report` | Show latest scorecard |
| `python scripts/clone_extract.py --wallet <addr>` | Extract target wallet history |
| `python -m analytics.clone_profile --wallet <addr>` | Build wallet profile + backtest |

---

## Tech Stack

| Technology | Purpose |
|---|---|
| Python 3.10+ | Runtime |
| `asyncio` | Concurrent strategies and I/O |
| `aiohttp` | Async HTTP client for CLOB API |
| `websockets` | BTC price feed (Binance WS) |
| `py-clob-client` | Polymarket CLOB order signing/placement |
| `web3` | Polygon RPC interaction |
| `eth-account` | Ethereum key management |
| `python-dotenv` | `.env` loading |
| `rich` | Console dashboard formatting |

**No database.** All persistent state is written to CSV/JSON log files under `logs/`.

---

## Configuration

All configuration is via environment variables, loaded from `.env` by `config.py`. See `.env.example` for the full reference (100+ variables, organized by section).

**Critical variables:**

| Variable | Required | Description |
|---|---|---|
| `PRIVATE_KEY` | Live only | Ethereum private key for order signing |
| `WALLET_ADDRESS` | Live only | Your wallet address |
| `DRY_RUN` | Default: `1` | Paper mode (1) vs live mode (0) |
| `CLOB_HOST` | Default set | Polymarket CLOB API URL |
| `POLYGON_RPC` | Optional | Polygon node URL |

**Config loading pattern (`config.py`):**
```python
# No function calls — just env var reads at import time
import os
from dotenv import load_dotenv
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
```

When adding new config variables:
1. Add to `config.py` with a safe default
2. Document in `.env.example` with inline comments
3. Use the variable only from `config`, never re-read `os.getenv` in strategy code

---

## Key Modules In Depth

### `bot.py` — Main Orchestrator

Creates shared infrastructure and spawns async tasks for each strategy:

```python
# Startup sequence
client = PolymarketClient(...)
price_feed = BTCPriceFeed()
health = HealthState()
risk = RiskEngine(...)
kill_switch = KillSwitch(...)
sizer = PositionSizer(...)
exec_mgr = ExecutionManager(...)

# Concurrent tasks
asyncio.gather(
    strategy_price_arb.run(),
    strategy_latency_arb.run(),
    strategy_market_maker.run(),
    strategy_wallet_clone.run(),
    refresh_market_cache(),
    dashboard.run(),
    ...
)
```

### `client.py` — CLOB API Wrapper

Wraps `py-clob-client` with async aiohttp calls. Key methods:
- `get_markets()` — fetch active markets
- `get_book(token_id)` — fetch order book
- `place_order(market, side, price, size)` — submit signed order
- `cancel_order(order_id)` — cancel by ID
- `cancel_all_orders()` — called on shutdown

Authentication is handled by `py-clob-client` using the private key. **Signature type 0 (EOA)** is used for standard wallets — do not add a `funder` param to `ClobClient` for EOA mode.

### `health_state.py` — Health FSM

States: `NORMAL → DEGRADED → SAFE_MODE → CIRCUIT_BREAKER`

- `NORMAL`: All systems healthy
- `DEGRADED`: Elevated errors or stale data; strategies reduce activity
- `SAFE_MODE`: Only cancel/reduce operations; no new positions
- `CIRCUIT_BREAKER`: Hard stop; all orders cancelled, no new orders

Strategies must check `health.level` before placing orders.

### `risk/risk_engine.py` — Risk Engine

Enforces:
- Daily loss limit (hard circuit breaker)
- Max open exposure per market
- Drawdown pace limits (vs. kill-switch)
- Volatility-targeted position scaling

### `risk/kill_switch.py` — 3-Tier Kill Switch

| Tier | Trigger | Effect |
|---|---|---|
| 1 | API error rate > threshold | Slow down; enter DEGRADED |
| 2 | Price feed stale > threshold | Enter SAFE_MODE |
| 3 | Drawdown pace > threshold | Enter CIRCUIT_BREAKER |

### `execution/execution_manager.py` — Order Execution

Before placing any order:
1. **Edge check**: Expected edge must exceed `EDGE_FLOOR`
2. **Live realism gates**: Depth, spread stability, throttle limits
3. **Maker/taker selection**: Adaptive based on spread and market conditions
4. **Timeout handling**: Orders that don't fill are cancelled

### `agent_loop/` — Supervised AI Loop

The orchestrator runs: `ingest → propose → validate → scorecard → plan`

**Safety model:**
- Reads: `logs/quant_trades.csv`, `logs/opt_best.json`
- Writes: `logs/deployment_plan.json`, `logs/applied_params.json`
- **Never** modifies `.env`, source code, or executes trades
- Operator must manually review and apply `deployment_plan.json`

**Controller policy (`controller_policy.json`):**
- `tunable`: Parameters the controller may adjust (8 items, e.g. signal threshold, edge floor)
- `forbidden`: Parameters the controller may never touch (16 items, e.g. `PRIVATE_KEY`, `DRY_RUN`, drawdown limits)

---

## Strategies

### Price Arbitrage (`strategies/price_arb.py`)
Scans all markets for YES+NO combined price < $1.00. Buys both sides to lock in guaranteed profit. Simple, low-frequency.

### Latency Arbitrage (`strategies/latency_arb.py`)
Near-expiry BTC markets: when price crosses a threshold, resolution is knowable before the book adjusts. Requires real-time BTC/USD from `price_feed.py`.

### Market Maker (`strategies/market_maker.py`)
Two-sided quotes on reward-earning markets. Earns maker rewards. Uses `live_realism.py` gates to avoid quoting in illiquid conditions.

### Wallet Clone (`strategies/wallet_clone.py`)
- **Extraction**: `scripts/clone_extract.py` fetches a target wallet's historical trades.
- **Profiling**: `analytics/clone_profile.py` builds a behavioral profile (category preferences, timing, price bias).
- **Execution**: `wallet_clone.py` replicates the profile's patterns with profile-matched sizing.
- **HF mode**: Paired YES/NO execution for high-frequency arbitrage.

---

## Data Flow

```
Market Data (Polymarket CLOB API)
    ↓
client.py (market cache, refreshed every 120s)
    ↓
Strategy (price_arb / latency_arb / market_maker / wallet_clone)
    ↓
signal_engine.py (multi-factor signal score)
    ↓
risk_engine.py (exposure check, vol target, daily loss gate)
    ↓
sizing.py (position size = edge × confidence × vol-adjusted capital)
    ↓
execution_manager.py (edge check → maker/taker → place/cancel)
    ↓
client.py (sign + submit order to CLOB)
    ↓
trade_logger.py (append to logs/quant_trades.csv)
```

---

## Logging and Observability

- **Log files**: `logs/` directory (rotating, 5 files × 5MB per module)
- **Console**: Rich dashboard via `dashboard.py` (stats every 60s)
- **Trade log**: `logs/quant_trades.csv` — structured per-trade records
- **Optimization**: `logs/opt_results.csv`, `logs/opt_best.json`
- **Deployment plan**: `logs/deployment_plan.json`
- **Applied params**: `logs/applied_params.json`

Per-module loggers:
```python
import logging
log = logging.getLogger(__name__)
```

---

## Development Workflow

### Paper Mode (Safe Default)
```bash
DRY_RUN=1 python bot.py
```
All orders are simulated; no real funds at risk.

### Optimization Cycle
```bash
# 1. Collect paper data
DRY_RUN=1 python bot.py          # or run_paper_super.ps1

# 2. Analyze performance
python -m analytics.performance_report
python -m analytics.fill_quality

# 3. Optimize parameters
python -m analytics.param_optimizer  # or run_paper_optimize.ps1

# 4. Validate (overfit check)
python -m analytics.walk_forward     # or run_walkforward.ps1

# 5. Run supervised cycle
python -m agent_loop.orchestrator cycle

# 6. Review deployment plan (MANUAL)
cat logs/deployment_plan.json

# 7. Apply changes manually to .env
# 8. Restart bot
```

### Live Deployment (Staged)
```bash
# Start at very small caps (see live_staircase.py for gates)
python scripts/live_staircase.py
# Or for clone HF strategy:
# run_clone_hf_live_safe.ps1
```

---

## Code Conventions

### Async/Await
- All strategies are async; use `asyncio.sleep()`, not `time.sleep()`
- Every strategy has a `run()` coroutine as its main loop
- Shared resources (client, health, risk) are passed by reference, not global

### Type Hints
- Use dataclasses for records: `Market`, `TradeRecord`, `SignalResult`
- Use enums for state machines: `HealthLevel`, `Tier`, `CircuitBreakerState`
- All public functions should have type hints

### Error Handling
- Catch specific exceptions, not bare `except:`
- API failures should update health state, not crash the bot
- Log errors with context: `log.error("Failed to place order: %s", e, exc_info=True)`
- Provide fallbacks where possible (e.g., Coinbase REST fallback for Binance WS)

### Naming
- `snake_case` for functions, variables, modules
- `CamelCase` for classes
- `UPPER_CASE` for constants in `config.py`
- `_underscore_prefix` for module-private helpers

### Adding a New Strategy
1. Create `strategies/my_strategy.py` with a class and `async def run(self)` loop
2. Accept shared dependencies in `__init__`: `client`, `health`, `risk`, `sizer`, `exec_mgr`
3. Check `self.health.level` before placing orders
4. Route all orders through `self.exec_mgr.place_order()`, never `client` directly
5. Log trades via `self.trade_logger.log_trade()`
6. Register in `bot.py` alongside other strategies

### Adding a New Config Variable
1. Add to `config.py` with a safe default (fail-safe for paper mode)
2. Document in `.env.example` with a comment explaining the variable
3. Optionally add to `agent_loop/controller_policy.json` `forbidden` list if it's safety-critical

---

## Safety Rules (Do Not Violate)

1. **Never set `DRY_RUN=0` automatically.** Live mode requires explicit operator action.
2. **Never modify `PRIVATE_KEY` or `WALLET_ADDRESS` in code.** These come only from `.env`.
3. **Never place orders without passing through `execution_manager.py`.** It enforces edge checks.
4. **Never bypass health state checks.** Strategies must respect `CIRCUIT_BREAKER` and `SAFE_MODE`.
5. **Never auto-apply `logs/deployment_plan.json`.** It requires operator review.
6. **Never add parameters to `controller_policy.json` `tunable` list without review.** Especially not drawdown/loss limits.
7. **Never use synchronous blocking calls inside async strategy loops.** Use `asyncio`-compatible alternatives.

---

## External Services

| Service | URL | Auth | Purpose |
|---|---|---|---|
| Polymarket CLOB | `https://clob.polymarket.com` | Private key signing | Order management and market data |
| Polygon RPC | `https://polygon-rpc.com` | None | On-chain wallet state (optional) |
| Binance WS | `wss://stream.binance.com:9443/ws/btcusdt@trade` | None | Real-time BTC/USD price |
| Coinbase REST | `https://api.coinbase.com/v2/prices/BTC-USD/spot` | None | BTC/USD fallback |
| Polymarket Public API | `https://polymarket.com/api` | None | Historical trade data for cloning |

---

## Common Gotchas

- **`ClobClient` signature_type=0 (EOA)**: Do not pass a `funder` parameter. This caused a bug that was fixed in commit `b10fbd7`.
- **Market cache**: `bot.py` refreshes the market list every 120s. Strategies should use the cached list, not call the API directly per-cycle.
- **Windows vs Linux**: Scripts are PowerShell (`.ps1`) for Windows. On Linux/Mac, translate the env-var exports to bash `export` statements or use a `.env` file.
- **`logs/` directory**: Must exist before running. Create with `mkdir -p logs` if needed.
- **Clone workflow order**: Extract → Profile → Backtest → Paper → Live-Safe. Do not skip steps.
- **HF mode pairing**: Clone HF mode places paired YES/NO orders. The `live_realism.py` gates are stricter in HF mode to avoid toxic fills.
