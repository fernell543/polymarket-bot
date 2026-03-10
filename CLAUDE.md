# CLAUDE.md — Polymarket Crypto Latency Arbitrage Bot

This file gives AI assistants all context needed to understand, modify, and extend this codebase. The bot runs a **single, focused strategy**: crypto binary latency arbitrage on Polymarket.

---

## Why This Strategy

The Polymarket account [@vague-sourdough](https://polymarket.com/@vague-sourdough) generated **$263,831 profit on $20.6M volume across 19,400+ trades** — almost entirely in crypto binary markets (BTC, ETH, SOL, XRP). The edge is structural, not predictive:

- Polymarket has thousands of markets like *"Will BTC be above $95,000 at 3:15 PM?"*
- These markets resolve to $1.00 (YES wins) or $0.00 (NO wins)
- In the final 30–90 seconds before resolution, the outcome is often **mathematically certain** from live spot price — but the Polymarket order book hasn't repriced yet
- The bot detects this lag, buys the winning side at a discount (e.g., 0.80 for a token worth 0.98), and holds to resolution

**Expected edge per trade:** 5–20 cents per dollar deployed
**Why it works at scale:** Polymarket creates dozens of new 15-minute BTC/ETH/SOL markets every day. This bot catches many of them each hour.

The exact strategy is documented inside `strategies/latency_arb.py`:
> *"This is the exact strategy used by high-profit bots like 'gabagool'."*

---

## Repository Structure

```
polymarket-bot/
├── bot.py                        # Main orchestrator — only runs LatencyArbStrategy
├── client.py                     # PolymarketClient: CLOB REST API wrapper (aiohttp)
├── config.py                     # All env var loading (read-only after import)
├── health_state.py               # Health FSM: NORMAL → DEGRADED → SAFE_MODE → CIRCUIT_BREAKER
├── dashboard.py                  # Rich console stats dashboard
├── preflight.py                  # Startup validation — blocks launch on config errors
├── auth_probe.py                 # Standalone auth test utility
│
├── strategies/
│   └── latency_arb.py            # ★ THE STRATEGY — all core logic lives here
│
├── feeds/
│   └── price_feed.py             # BTC/USD via Binance WS; Coinbase REST fallback
│
├── risk/
│   ├── sizing.py                 # PositionSizer: edge × confidence × vol-adjusted capital
│   ├── risk_engine.py            # Daily loss limit, max exposure, drawdown circuit breaker
│   └── kill_switch.py            # 3-tier: API errors → stale feed → drawdown pace
│
├── execution/
│   ├── execution_manager.py      # Edge check → maker/taker selection → place/cancel
│   └── live_realism.py           # Throttle, depth gate, spread stability gate
│
├── analytics/
│   ├── trade_logger.py           # Writes logs/quant_trades.csv
│   ├── performance_report.py     # P&L, Sharpe, win rate CLI
│   ├── fill_quality.py           # Fill ratio, adverse selection, latency
│   ├── param_optimizer.py        # Grid/random search over env vars
│   └── walk_forward.py           # Overfit detection across time windows
│
├── agent_loop/
│   ├── orchestrator.py           # Supervised cycle: ingest → propose → validate → plan
│   ├── controller.py             # Auto-tuning within policy bounds
│   └── controller_policy.json    # Tunable allowlist + forbidden param list
│
├── requirements.txt              # 8 Python packages
├── .env.example                  # All config variables with comments
└── run_*.ps1                     # PowerShell launchers (Windows)
```

---

## How the Strategy Works

### Core Loop (`strategies/latency_arb.py`)

```
Every 5 seconds:
  1. Fetch the full market list (cached, refreshed every 120s)
  2. Filter to crypto threshold markets ("Will BTC be above $X at TIME?")
  3. For each market with <= LATENCY_ARB_WINDOW_SECS remaining:
     a. Get live spot price from BTCPriceFeed
     b. Compare spot to threshold — is the gap >= CERTAINTY_GAP_PCT (0.5%)?
     c. If yes: which side wins? (YES if above threshold for "above" market)
     d. Fetch the current ask price for the winning token
     e. If discount vs fair value (1.0 - fee) >= LATENCY_ARB_MIN_DISCOUNT (5%):
        → Place a market BUY order for the winning token
```

### Why the Edge Exists

When BTC is at $97,000 and the market asks *"Will BTC be above $95,000?"* with 45 seconds left:
- **Fair value**: ~$0.98 (2% Polymarket fee)
- **Actual ask**: may still be $0.72–$0.85 because market makers haven't repriced
- **Bot action**: buy at $0.80, collect $1.00 at resolution → **25% return in 45 seconds**

The more extreme the gap between spot and threshold, the more certain the outcome and the larger the position the bot takes (via confidence-scaled sizing).

---

## Entry Points

| Command | Description |
|---|---|
| `python bot.py` | Run the bot (paper mode by default) |
| `python -m analytics.performance_report` | Post-session P&L, Sharpe, win rate |
| `python -m analytics.fill_quality` | Fill ratio and adverse selection stats |
| `python -m analytics.param_optimizer` | Grid/random search over key parameters |
| `python -m analytics.walk_forward` | Overfit detection across time windows |
| `python -m agent_loop.orchestrator cycle` | Run one supervised improvement cycle |
| `python -m agent_loop.orchestrator report` | Show latest scorecard |

---

## Configuration

All config lives in `.env` (loaded by `config.py`). Never read `os.getenv` directly in strategy code — always import from `config`.

### Critical Latency Arb Parameters

| Variable | Default | Description |
|---|---|---|
| `LATENCY_ARB_WINDOW_SECS` | `60` | Only enter if this many seconds remain before resolution |
| `LATENCY_ARB_MIN_DISCOUNT` | `0.05` | Minimum price discount vs fair value (5%) to enter |
| `CERTAINTY_GAP_PCT` | `0.005` | BTC must be ≥0.5% from threshold (hardcoded in strategy) |
| `DRY_RUN` | `1` | Paper mode (1) vs live mode (0) — **never set 0 automatically** |
| `PRIVATE_KEY` | — | Ethereum private key for signing orders (live only) |
| `WALLET_ADDRESS` | — | Your Polygon wallet address (live only) |
| `POLY_SIGNATURE_TYPE` | `2` | Signing mode: 0=EOA, 2=API-key proxy (use 2 for standard accounts) |

### Position Sizing Parameters

| Variable | Default | Description |
|---|---|---|
| `RISK_BUDGET_PCT` | `0.02` | Fraction of balance to risk per trade (2%) |
| `SIZE_MIN_USDC` | `5.0` | Minimum trade size in USDC |
| `SIZE_MAX_USDC` | `500.0` | Maximum trade size in USDC |
| `EDGE_FLOOR_BPS` | `50` | Minimum edge in basis points to trade (50 = 0.5%) |
| `CONFIDENCE_FLOOR` | `0.20` | Minimum signal confidence to trade |

### Risk / Circuit Breaker Parameters

| Variable | Default | Description |
|---|---|---|
| `RISK_DAILY_LOSS_LIMIT` | `100.0` | Hard stop if daily loss exceeds this (USDC) |
| `RISK_MAX_DRAWDOWN` | `200.0` | Session drawdown limit — requires manual restart |
| `RISK_MAX_MARKET_EXPOSURE` | `200.0` | Max USDC deployed in any single market |
| `RISK_MAX_PORTFOLIO_EXPOSURE` | `1000.0` | Max total USDC across all open positions |

---

## Data Flow

```
Binance WebSocket (real-time BTC/USD)
         │
         ▼
  BTCPriceFeed.price   ←  Coinbase REST fallback if WS drops
         │
         ▼
  LatencyArbStrategy.scan_once()
    │  • parse market list for crypto threshold markets
    │  • compare spot price vs resolution threshold
    │  • check time remaining <= WINDOW_SECS
    │  • check gap >= CERTAINTY_GAP_PCT
         │
         ▼
  PositionSizer.compute()
    │  • edge_bps = discount × 10,000
    │  • confidence = gap / (CERTAINTY_GAP_PCT × 4), capped at 1.0
    │  • size = balance × RISK_BUDGET_PCT × edge_scalar × conf_scalar × vol_mult
         │
         ▼
  LatencyArbStrategy.execute()
    │  • client.place_market_order(token_id, "BUY", size_usdc)
         │
         ▼
  trade_logger.log_trade()   →   logs/quant_trades.csv
```

---

## Key Modules In Depth

### `strategies/latency_arb.py` — The Strategy

**Key classes:**
- `BTCMarket` — parsed market with threshold, direction, resolution time, token IDs
- `LatencyArbTrade` — a detected opportunity with side, size, entry price, expected profit
- `LatencyArbStrategy` — main class with `run()` async loop

**Key methods:**
- `_parse_btc_market(market)` — regex-parses "above $X" / "below $X" from question text
- `_evaluate(btc_market, spot)` — core opportunity detection and sizing
- `execute(trade)` — places the market buy order
- `scan_once(markets)` — called every 5 seconds; returns list of opportunities

**To extend to ETH/SOL/XRP markets:**
Modify `_parse_btc_market()` to also match "ETH", "Ethereum", "SOL", "Solana", "XRP", "Ripple" in the question. Add separate price feeds (or pull from the same Binance WS with additional symbols). The threshold-parsing regex already works for any coin.

### `feeds/price_feed.py` — BTC/USD Feed

- Connects to Binance WebSocket (`wss://stream.binance.com:9443/ws/btcusdt@trade`)
- Updates `self._price` on every trade message
- `is_fresh`: True if updated within last 5 seconds
- `is_stale`: True if connected but no update in 60 seconds → triggers kill-switch Tier 2
- Falls back to Coinbase REST via `fetch_once()` when WS fails

### `client.py` — CLOB API Wrapper

- `get_markets()` — fetches all active markets; bot caches for 120s
- `get_best_prices(token_id)` — returns `(bid, ask)` for a token
- `place_market_order(token_id, side, size_usdc)` — submits a signed taker order
- `cancel_all_orders()` — called on graceful shutdown

**Critical auth note:** Use `POLY_SIGNATURE_TYPE=2` (API-key proxy) for standard Polymarket accounts. Do **not** pass a `funder` param to `ClobClient` when using signature type 0 (EOA) — this was a bug fixed in commit `b10fbd7`.

### `risk/sizing.py` — Position Sizer

Formula:
```
base_size  = balance × RISK_BUDGET_PCT
edge_scalar = clamp(edge_bps / EDGE_REFERENCE_BPS, 0, 2)
conf_scalar = clamp(confidence / 1.0, CONFIDENCE_FLOOR, 1)
vol_mult   = VOL_MULT_LOW | VOL_MULT_MID | VOL_MULT_HIGH
size_usdc  = clamp(base × edge_scalar × conf_scalar × vol_mult, SIZE_MIN, SIZE_MAX)
```

For latency arb, `vol_regime="mid"` is always passed (binary, time-pressured outcome).

### `health_state.py` — Health FSM

States: `NORMAL → DEGRADED → SAFE_MODE → CIRCUIT_BREAKER`

The strategy checks `health.level` before every order. In `CIRCUIT_BREAKER`, no new orders are placed and all open orders are cancelled.

### `risk/kill_switch.py` — 3-Tier Kill Switch

| Tier | Trigger | Effect |
|---|---|---|
| 1 | API error rate > 20% | Log warning; full size |
| 2 | BTC feed stale > 30s | Enter SAFE_MODE; no new entries |
| 3 | Drawdown pace > 80% of daily limit/hour | Enter CIRCUIT_BREAKER |

---

## Development Workflow

### 1. Paper Mode (Always Start Here)
```bash
DRY_RUN=1 python bot.py
```
No real funds. All orders are simulated. Trade log written to `logs/quant_trades.csv`.

### 2. Analyze Performance
```bash
python -m analytics.performance_report   # win rate, Sharpe, total P&L
python -m analytics.fill_quality         # fill ratio, adverse selection
```

### 3. Optimize Parameters
```bash
python -m analytics.param_optimizer     # grid/random search
python -m analytics.walk_forward        # overfit detection
```

### 4. Supervised Improvement Cycle
```bash
python -m agent_loop.orchestrator cycle   # ingest → propose → validate → plan
cat logs/deployment_plan.json             # review proposed changes MANUALLY
# Apply changes manually to .env, then restart
```

### 5. Live Deployment (Staged)
```bash
# Stage A: $5 max, 3 positions, validate fill rate >= 65%
LIVE_DEPLOY_MODE=staircase_A DRY_RUN=0 python bot.py

# Stage B: $20 max, 5 positions, validate Sharpe >= 0.40
LIVE_DEPLOY_MODE=staircase_B DRY_RUN=0 python bot.py

# Stage C: $50 max, 10 positions
LIVE_DEPLOY_MODE=staircase_C DRY_RUN=0 python bot.py

# Production: full caps
LIVE_DEPLOY_MODE=production DRY_RUN=0 python bot.py
```
Or use the guided launcher: `python scripts/live_staircase.py`

---

## Tuning for Maximum Profit

These are the highest-leverage parameters to tune (all in `.env`):

| Parameter | Conservative | Aggressive | Notes |
|---|---|---|---|
| `LATENCY_ARB_WINDOW_SECS` | 30 | 90 | Wider window = more trades, more uncertainty |
| `LATENCY_ARB_MIN_DISCOUNT` | 0.08 | 0.03 | Lower = more trades, smaller edge |
| `RISK_BUDGET_PCT` | 0.01 | 0.05 | Higher = bigger positions per trade |
| `SIZE_MAX_USDC` | 200 | 1000 | Hard ceiling per trade |
| `RISK_MAX_PORTFOLIO_EXPOSURE` | 500 | 5000 | Total capital deployed at once |
| `RISK_DAILY_LOSS_LIMIT` | 50 | 500 | Must match your bankroll |

**Optimal tuning approach:**
1. Run paper mode for 48+ hours to collect `logs/quant_trades.csv`
2. Run `python -m analytics.param_optimizer` to find best settings for your market conditions
3. Validate with `python -m analytics.walk_forward` (check overfit ratio < 0.3)
4. Review `logs/deployment_plan.json` before applying anything live

**To replicate vague-sourdough scale (~$20M volume):**
- Run 24/7 (the bot handles this natively via `asyncio`)
- Set `SIZE_MAX_USDC=1000`, `RISK_MAX_PORTFOLIO_EXPOSURE=10000`
- Extend coverage to ETH, SOL, XRP markets in addition to BTC
- Keep `LATENCY_ARB_WINDOW_SECS=60` and `LATENCY_ARB_MIN_DISCOUNT=0.04`

---

## Code Conventions

### Async/Await
- All strategies are fully async; use `asyncio.sleep()`, never `time.sleep()`
- `LatencyArbStrategy.run()` is the main coroutine — called from `bot.py`
- Shared resources (`client`, `health`, `sizer`) are passed by reference, not globals

### Error Handling
- Catch specific exceptions; never bare `except:`
- API failures update `health_state`, they don't crash the bot
- Log with context: `log.error("msg: %s", e, exc_info=True)`
- Feed failures fall back (Coinbase REST) and set `is_fresh=False`

### Naming
- `snake_case` — functions, variables, modules
- `CamelCase` — classes
- `UPPER_CASE` — constants in `config.py`
- `_underscore_prefix` — module-private helpers

### Adding Config Variables
1. Add to `config.py` with a safe default (paper-safe if variable is omitted)
2. Document in `.env.example` with inline comment
3. Add to `agent_loop/controller_policy.json` `forbidden` list if safety-critical

---

## Safety Rules (Never Violate)

1. **Never set `DRY_RUN=0` automatically.** Live mode requires explicit operator action.
2. **Never modify `PRIVATE_KEY` or `WALLET_ADDRESS` in code.** These come only from `.env`.
3. **Never place orders without the edge check.** The discount gate in `_evaluate()` is the primary profit filter.
4. **Never bypass health state checks.** The strategy must respect `CIRCUIT_BREAKER` and `SAFE_MODE`.
5. **Never auto-apply `logs/deployment_plan.json`.** It requires operator review.
6. **Never use synchronous blocking calls inside async loops.** Use `asyncio`-compatible alternatives.
7. **Never add risk/drawdown limits to the `tunable` list in `controller_policy.json`.** They are forbidden by design.

---

## External Services

| Service | URL | Auth | Purpose |
|---|---|---|---|
| Polymarket CLOB | `https://clob.polymarket.com` | Private key signing | Order placement and market data |
| Polygon RPC | `https://polygon-rpc.com` | None | On-chain wallet state (optional) |
| Binance WS | `wss://stream.binance.com:9443/ws/btcusdt@trade` | None | Real-time BTC/USD price (primary) |
| Coinbase REST | `https://api.coinbase.com/v2/prices/BTC-USD/spot` | None | BTC/USD fallback |

---

## Common Gotchas

- **`ClobClient` EOA mode**: Do not pass `funder` param when `POLY_SIGNATURE_TYPE=0`. Bug fixed in commit `b10fbd7`. Use `POLY_SIGNATURE_TYPE=2` for standard accounts.
- **Market cache**: The bot refreshes the market list every 120s. The strategy reads from this cache — it does not call the API per cycle. Do not call `client.get_markets()` inside the scan loop.
- **`logs/` directory**: Must exist before running. Create with `mkdir -p logs`.
- **Windows vs Linux**: Launch scripts are PowerShell (`.ps1`). On Linux/Mac, set env vars in `.env` and run `python bot.py` directly.
- **Price feed freshness**: `price_feed.is_fresh` checks within the last 5 seconds. If False, `scan_once()` aborts and logs a warning. This is intentional — a stale price is dangerous.
- **Resolution time parsing**: The strategy parses resolution time from `market.end_date_iso` first, then falls back to parsing the question text. Markets without parseable times are skipped.
- **Near-threshold caution**: If `gap_pct < CERTAINTY_GAP_PCT` (BTC too close to threshold), the trade is skipped even if time is running out. This prevents losses on uncertain outcomes.
