# CLAUDE.md — Polymarket Crypto Latency Arbitrage Bot

This file gives AI assistants all context needed to understand, modify, and extend this codebase. The bot runs a **single, focused strategy**: crypto binary latency arbitrage on Polymarket.

---

## Why This Strategy

The Polymarket account [@vague-sourdough](https://polymarket.com/@vague-sourdough) generated **$263,831 profit on $20.6M volume across 19,400+ trades** — almost entirely in crypto binary markets (BTC, ETH, SOL, XRP). The edge is structural, not predictive:

- Polymarket has thousands of markets like *"Will BTC be above $95,000 at 3:15 PM?"*
- These markets resolve to $1.00 (YES wins) or $0.00 (NO wins)
- In the final 30–90 seconds before resolution, the outcome is often **mathematically certain** from live spot price — but the Polymarket order book hasn't repriced yet
- The bot detects this lag, buys the winning side at a discount (e.g., 0.80 for a token worth 0.98), and holds to resolution

**Math:** 19,400 trades × ~$1,063 avg = $20.6M volume → $263k profit = **~1.28% edge per trade**.
**Per trade**: average profit of ~$13.57 in ~45 seconds. That's the target to replicate.

The exact strategy is documented inside `strategies/latency_arb.py`:
> *"This is the exact strategy used by high-profit bots like 'gabagool'."*

---

## Repository Structure

```
polymarket-bot/
├── bot.py                        # Main orchestrator — runs LatencyArbStrategy
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
  1. Fetch market list (cached 120s) — filter to crypto threshold markets
  2. For each market with <= LATENCY_ARB_WINDOW_SECS remaining:
     a. Get live spot price from price feed
     b. Compare spot to threshold — gap >= CERTAINTY_GAP_PCT?
     c. Determine winning side (YES if "above" market and spot > threshold)
     d. Fetch ask price for the winning token
     e. If discount vs fair value >= LATENCY_ARB_MIN_DISCOUNT:
        → Place a market BUY order for the winning token
```

### Why the Edge Exists

When BTC is at $97,000 and the market asks *"Will BTC be above $95,000?"* with 45 seconds left:
- **Fair value**: ~$0.98 (accounting for 2% Polymarket fee)
- **Actual ask**: may still be $0.72–$0.85 because market makers haven't repriced
- **Bot action**: buy at $0.80, collect $1.00 at resolution → **25% return in 45 seconds**

The gap between spot and threshold drives confidence. The larger the gap, the more certain the outcome, the larger the position.

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

### ⚠️ Known Config Bug — Must Fix Before Tuning

`LATENCY_ARB_WINDOW_SECS` and `LATENCY_ARB_MIN_DISCOUNT` are **hardcoded** in `config.py` (lines 88–92). They are **not** read from the environment even though `.env.example` documents them. Any `.env` overrides are silently ignored.

**Fix in `config.py`:**
```python
# WRONG (current):
LATENCY_ARB_WINDOW_SECS = 60
LATENCY_ARB_MIN_DISCOUNT = 0.05

# CORRECT:
LATENCY_ARB_WINDOW_SECS = int(os.getenv("LATENCY_ARB_WINDOW_SECS", "60"))
LATENCY_ARB_MIN_DISCOUNT = float(os.getenv("LATENCY_ARB_MIN_DISCOUNT", "0.05"))
```

Same issue applies to `CERTAINTY_GAP_PCT` (hardcoded in `latency_arb.py` line 45). Make it configurable.

### Critical Latency Arb Parameters

| Variable | Default | Description |
|---|---|---|
| `LATENCY_ARB_WINDOW_SECS` | `60` | Only enter if this many seconds remain before resolution |
| `LATENCY_ARB_MIN_DISCOUNT` | `0.05` | Minimum price discount vs fair value (5%) to enter |
| `CERTAINTY_GAP_PCT` | `0.005` | Spot must be ≥0.5% from threshold (hardcoded — see bug above) |
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

## Known Gaps Limiting Profitability

These are confirmed gaps identified by code analysis. Fixing them in order of impact will move the bot toward vague-sourdough scale.

### 🔴 Gap 1: BTC-Only Market Filter (–70% Trade Volume)

`_parse_btc_market()` in `latency_arb.py` line 129 only matches "btc" or "bitcoin". ETH, SOL, and XRP markets — which vague-sourdough actively trades — are completely ignored.

**Fix:** Extend the check and rename the method:
```python
SUPPORTED_ASSETS = {
    "btc": "btcusdt", "bitcoin": "btcusdt",
    "eth": "ethusdt", "ethereum": "ethusdt",
    "sol": "solusdt", "solana": "solusdt",
    "xrp": "xrpusdt", "ripple": "xrpusdt",
}

def _parse_crypto_market(self, market: Market) -> Optional[BTCMarket]:
    q = market.question.lower()
    asset_feed = None
    for keyword, feed_symbol in SUPPORTED_ASSETS.items():
        if keyword in q:
            asset_feed = feed_symbol
            break
    if not asset_feed:
        return None
    # ... rest of parsing unchanged
```

Also requires adding ETH/SOL/XRP price feeds in `feeds/price_feed.py`. Binance WS supports multi-stream:
```
wss://stream.binance.com:9443/ws/btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade
```

**Expected impact:** 3–4× more trades per day at the same win rate.

---

### 🔴 Gap 2: Sequential Trade Execution (–500ms Per Cycle)

`bot.py` lines 507–508 execute multiple detected trades one-at-a-time:
```python
# WRONG (current):
for trade in trades:
    await self.latency_arb.execute(trade)

# CORRECT:
await asyncio.gather(*[self.latency_arb.execute(t) for t in trades])
```

In a 60-second window with 3 simultaneous opportunities, sequential execution costs ~1.5 seconds of the available window. Parallel execution is safe since each trade targets a different token.

---

### 🔴 Gap 3: Hardcoded CERTAINTY_GAP_PCT at 0.5% (–40% Eligible Trades)

`latency_arb.py` line 45 hardcodes 0.5% gap requirement. At a $95,000 BTC price this requires a $475 gap. Real edge exists at 0.3% (~$285) with only 10 seconds remaining — time makes up for the smaller gap.

**Fix:** Make it configurable and time-sensitive:
```python
CERTAINTY_GAP_PCT = float(os.getenv("CERTAINTY_GAP_PCT", "0.005"))

# In _evaluate(), use time-adjusted gap floor:
# Tighter gap allowed as resolution approaches
time_adj_gap = CERTAINTY_GAP_PCT * max(0.5, secs_remaining / WINDOW_SECS)
if gap_pct < time_adj_gap:
    return None
```

---

### 🟡 Gap 4: No Time-Decay Position Scaling (+12% Avg Return)

The sizer treats a trade with 55 seconds left the same as one with 5 seconds left. With 5 seconds remaining, the outcome is far more certain and the position should be larger.

**Fix in `_evaluate()`:**
```python
# Scale size up as time runs out (outcome becomes more certain)
time_urgency = 1.0 - (secs_remaining / WINDOW_SECS)   # 0 at entry, 1 at expiry
time_multiplier = 1.0 + (time_urgency * 0.5)           # up to 1.5× at final seconds
size_usdc = min(size_usdc * time_multiplier, config.SIZE_MAX_USDC)
```

---

### 🟡 Gap 5: Missing Depth/Liquidity Gate (+1–2% Slippage Reduction)

`_evaluate()` fetches the ask price but never checks order book depth. A $500 order into a $50-depth book causes severe adverse selection. Add a depth check before entry:

```python
bid, ask = await self.client.get_best_prices(token_id)
depth = await self.client.get_depth(token_id)   # sum of top-N levels
if depth < config.LATENCY_ARB_MIN_DEPTH_USDC:   # new config var, suggest 50.0
    log.debug("LatencyArb: skipping %s — insufficient depth %.0f", ...)
    return None
```

---

### 🟡 Gap 6: Market Cache Too Stale (Missing Early Windows)

`_market_refresh_interval = 120.0` (line 99, latency_arb.py). New crypto markets appear every 15 minutes. A 2-minute stale cache means missing the first scan pass on fresh markets.

**Fix:** Reduce to 60 seconds:
```python
self._market_refresh_interval = 60.0
```

API cost is minimal (the market list is cached in `bot.py`; this just re-parses it).

---

### 🟡 Gap 7: Exposure Tracking Not Wired (Risk Control Gap)

`risk_engine.py` has `add_exposure()` / `remove_exposure()` methods but `latency_arb.py` never calls them. The `RISK_MAX_PORTFOLIO_EXPOSURE` limit is therefore never enforced — all strategies can deploy capital simultaneously without accounting for each other.

**Fix in `execute()` in `latency_arb.py`:**
```python
async def execute(self, trade: LatencyArbTrade) -> LatencyArbTrade:
    if self._risk_engine:
        self._risk_engine.add_exposure(trade.market.market.market_id, trade.size_usdc)
    result = await self.client.place_market_order(...)
    # On resolution/cancel, call remove_exposure()
```

---

### 🟡 Gap 8: Poll Interval Too Slow Near Expiry (–20 Trades/Day)

`bot.py` polls every 5 seconds regardless of how close the nearest market is to expiry. In the critical final 30 seconds, polling every 5 seconds means only 6 checks.

**Fix:** Adaptive polling in `bot.py`:
```python
min_secs_to_expiry = min(
    (m.resolution_dt - now).total_seconds()
    for m in self.latency_arb._btc_markets
    if m.resolution_dt
) if self.latency_arb._btc_markets else 60

poll_interval = 1.0 if min_secs_to_expiry < 30 else 5.0
await asyncio.sleep(poll_interval)
```

---

### 🟠 Gap 9: API Kill-Switch Not Wired (Error Tracking Broken)

`kill_switch.py` tracks API error rates across calls to `record_api_call()`. But `client.py` never calls `record_api_call()`. The kill-switch always sees 0% error rate and never escalates on API degradation.

**Fix in `client.py`:** After each request, call the kill-switch:
```python
self.kill_switch.record_api_call(success=resp.status < 400)
```

---

### 🟠 Gap 10: Trade Logger Not Fully Wired (Analytics Blind)

`latency_arb.py` does not populate `realized_edge`, `spread_at_entry`, or `regime` in the trade log. This means `analytics/fill_quality.py` and `analytics/param_optimizer.py` produce incomplete or wrong results.

**Fix in `execute()`:**
```python
self.trade_logger.log_trade(
    strategy="latency_arb",
    ...
    spread_at_entry=trade.fair_value - trade.entry_price,
    regime="time_critical" if secs_remaining < 20 else "standard",
    expected_edge=trade.expected_profit / trade.size_usdc,
)
```

---

## Key Modules In Depth

### `strategies/latency_arb.py` — The Strategy

**Key classes:**
- `BTCMarket` — parsed market with threshold, direction, resolution time, token IDs
- `LatencyArbTrade` — a detected opportunity with side, size, entry price, expected profit
- `LatencyArbStrategy` — main class with `run()` async loop

**Key methods:**
- `_parse_btc_market(market)` — regex-parses "above $X" / "below $X" from question text. **Currently BTC-only — must be extended.**
- `_evaluate(btc_market, spot)` — core opportunity detection and sizing
- `execute(trade)` — places the market buy order
- `scan_once(markets)` — called every 5 seconds; returns list of opportunities

### `feeds/price_feed.py` — Price Feed

- Connects to Binance WebSocket for BTC/USDT
- Updates `self._price` on every trade message
- `is_fresh`: True if updated within last 5 seconds
- `is_stale`: True if no update in 60 seconds → triggers kill-switch Tier 2
- Falls back to Coinbase REST via `fetch_once()` when WS fails
- **Currently BTC-only.** To add ETH/SOL/XRP, subscribe to the Binance multi-stream endpoint and route by the `msg["s"]` (symbol) field.

### `client.py` — CLOB API Wrapper

- `get_markets()` — fetches all active markets; bot caches for 120s
- `get_best_prices(token_id)` — returns `(bid, ask)` for a token
- `place_market_order(token_id, side, size_usdc)` — submits a signed taker order
- `cancel_all_orders()` — called on graceful shutdown

**Critical auth note:** Use `POLY_SIGNATURE_TYPE=2` (API-key proxy) for standard Polymarket accounts. Do **not** pass a `funder` param to `ClobClient` when using signature type 0 (EOA) — this was a bug fixed in commit `b10fbd7`.

### `risk/sizing.py` — Position Sizer

Formula:
```
base_size   = balance × RISK_BUDGET_PCT
edge_scalar = clamp(edge_bps / EDGE_REFERENCE_BPS, 0, 2)
conf_scalar = clamp(confidence / 1.0, CONFIDENCE_FLOOR, 1)
vol_mult    = VOL_MULT_LOW | VOL_MULT_MID | VOL_MULT_HIGH
size_usdc   = clamp(base × edge_scalar × conf_scalar × vol_mult, SIZE_MIN, SIZE_MAX)
```

For latency arb, `vol_regime="mid"` is always passed (binary, time-pressured outcome).

**Known limitation:** `balance` is set once per cycle; it doesn't update between trades within a cycle. For rapid multi-trade cycles, this can slightly oversize later positions. Acceptable at current frequency.

### `health_state.py` — Health FSM

States: `NORMAL → DEGRADED → SAFE_MODE → CIRCUIT_BREAKER`

The strategy checks `health.level` before every order. In `CIRCUIT_BREAKER`, no new orders are placed and all open orders are cancelled.

### `risk/kill_switch.py` — 3-Tier Kill Switch

| Tier | Trigger | Effect |
|---|---|---|
| 1 | API error rate > 20% | Log warning; full size |
| 2 | BTC feed stale > 30s | Enter SAFE_MODE; no new entries |
| 3 | Drawdown pace > 80% of daily limit/hour | Enter CIRCUIT_BREAKER |

**Note:** API error tracking is currently broken — `client.py` never calls `record_api_call()`. Tiers 1–3 for API errors will never trigger. See Gap 9 above.

---

## Development Workflow

### 1. Paper Mode (Always Start Here)
```bash
mkdir -p logs
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

These are the highest-leverage parameters to tune (all in `.env`). Fix the config bug first (Gap 1 in the Config section) or these will have no effect.

| Parameter | Conservative | Aggressive | Notes |
|---|---|---|---|
| `LATENCY_ARB_WINDOW_SECS` | 30 | 90 | Wider window = more trades, slightly more uncertainty |
| `LATENCY_ARB_MIN_DISCOUNT` | 0.08 | 0.03 | Lower = more trades, smaller edge per trade |
| `CERTAINTY_GAP_PCT` | 0.005 | 0.003 | Lower = more trades in tight-gap scenarios |
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
- Fix all 10 gaps above — these are what separate a $5k/month bot from a $50k/month bot
- Run 24/7 (the bot handles this natively via `asyncio`)
- Set `SIZE_MAX_USDC=1000`, `RISK_MAX_PORTFOLIO_EXPOSURE=10000`
- Extend coverage to ETH, SOL, XRP markets (Gap 1 — biggest single improvement)
- Keep `LATENCY_ARB_WINDOW_SECS=60` and `LATENCY_ARB_MIN_DISCOUNT=0.04`

### Expected Impact of Fixes

| Fix | Estimated Impact |
|---|---|
| ETH/SOL/XRP market support (Gap 1) | **3–4× more trades/day** |
| Parallel execution (Gap 2) | +5–10 trades/hour |
| Lower CERTAINTY_GAP_PCT (Gap 3) | +40% eligible trades |
| Time-decay position scaling (Gap 4) | +12% avg return per trade |
| Depth gating (Gap 5) | –1–2% slippage per trade |
| Faster cache refresh (Gap 6) | +5–10 trades/day |
| Exposure tracking (Gap 7) | Risk control only (no P&L impact) |
| Adaptive poll interval (Gap 8) | +20 trades/day |
| Kill-switch wiring (Gap 9) | Reliability only |
| Trade logger wiring (Gap 10) | Enables analytics feedback loop |

---

## Code Conventions

### Async/Await
- All strategies are fully async; use `asyncio.sleep()`, never `time.sleep()`
- `LatencyArbStrategy.run()` is the main coroutine — called from `bot.py`
- Shared resources (`client`, `health`, `sizer`) are passed by reference, not globals
- Multiple trades in one cycle must be executed with `asyncio.gather()`, not a `for` loop

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
1. Add to `config.py` with `os.getenv(...)` and a safe default
2. Document in `.env.example` with inline comment
3. Add to `agent_loop/controller_policy.json` `forbidden` list if safety-critical
4. **Never hardcode values** that affect trade frequency or sizing

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

**To add multi-asset:** change Binance WS to the combined stream:
`wss://stream.binance.com:9443/ws/btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade`

---

## Common Gotchas

- **`ClobClient` EOA mode**: Do not pass `funder` param when `POLY_SIGNATURE_TYPE=0`. Bug fixed in commit `b10fbd7`. Use `POLY_SIGNATURE_TYPE=2` for standard accounts.
- **Config hardcoding bug**: `LATENCY_ARB_WINDOW_SECS` and `LATENCY_ARB_MIN_DISCOUNT` in `config.py` are NOT loaded from env. `.env` overrides are silently ignored. Fix before tuning (see Gap above).
- **Market cache**: The bot refreshes every 120s. Do not call `client.get_markets()` inside the scan loop — use the cache.
- **`logs/` directory**: Must exist before running. Create with `mkdir -p logs`.
- **Windows vs Linux**: Launch scripts are PowerShell (`.ps1`). On Linux/Mac, use `.env` and run `python bot.py` directly.
- **Price feed freshness**: `price_feed.is_fresh` checks within the last 5 seconds. If False, `scan_once()` aborts. Intentional — stale price = dangerous entry.
- **Resolution time parsing**: Parsed from `market.end_date_iso` first, then question text. Markets without parseable times are skipped silently (check logs if trade count is low).
- **Near-threshold caution**: If `gap_pct < CERTAINTY_GAP_PCT`, trade is skipped even with 5 seconds left. This is intentional but the threshold is currently too conservative (see Gap 3).
- **Execution gates in live mode**: `live_realism.py` has a spread-stability gate that blocks orders when spread is widening rapidly. This will block exactly when latency arb is most profitable. Consider disabling `LIVE_SPREAD_STABILITY_SECS` or setting it to 0 for this strategy.
- **Depth filter in live mode**: `LIVE_MIN_DEPTH_USDC` defaults to 100. Binary markets near expiry often have thin books. Reduce to 50 for latency arb or add a strategy-specific override.
