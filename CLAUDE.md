# CLAUDE.md — Polymarket Crypto Latency Arbitrage Bot

This file gives AI assistants all context needed to understand, modify, and maximize profitability for this codebase. The bot runs a **single, focused strategy**: crypto binary latency arbitrage on Polymarket.

**When working on this codebase, always prioritize changes that increase trade volume, reduce blocked trades, and widen the net of capturable opportunities.** Every fix below has a dollar impact estimate.

---

## The $263k Strategy — What We're Replicating

The Polymarket account [@vague-sourdough](https://polymarket.com/@vague-sourdough):
- **$263,831 profit** on **$20.6M volume** across **19,400+ trades**
- Trades crypto binary markets: BTC, ETH, SOL, XRP
- Average profit per trade: **~$13.60** on ~$1,063 average size = **1.28% edge per trade**
- Largest single win: **$5,070**
- Runs 24/7, catching dozens of opportunities per hour
- Joined February 2026, steady equity curve — not luck, structural edge

### How the Edge Works

Polymarket has thousands of markets like *"Will BTC be above $95,000 at 3:15 PM?"*

These resolve to $1.00 (YES) or $0.00 (NO). In the final 30–90 seconds:
- The outcome is **mathematically certain** from live spot price
- The order book **hasn't repriced yet** — market makers are slow
- Fair value of the winning token: ~$0.98
- Actual ask price: still $0.70–$0.85

**Bot action:** buy winning token at $0.80 → collect $1.00 at resolution → **25% return in 45 seconds**

The strategy is inside `strategies/latency_arb.py` line 1–16:
> *"This is the exact strategy used by high-profit bots like 'gabagool'."*

---

## Repository Structure

```
polymarket-bot/
├── bot.py                        # Main orchestrator — runs strategy loops
├── client.py                     # PolymarketClient: CLOB API wrapper (aiohttp)
├── config.py                     # ⚠️ Env var loading — HAS BUGS (see below)
├── health_state.py               # Health FSM: NORMAL → DEGRADED → SAFE_MODE → CIRCUIT_BREAKER
├── dashboard.py                  # Rich console stats dashboard
├── preflight.py                  # Startup validation
├── auth_probe.py                 # Auth test utility
│
├── strategies/
│   └── latency_arb.py            # ★ THE STRATEGY — all core profit logic
│
├── feeds/
│   └── price_feed.py             # BTC/USD via Binance WS; Coinbase REST fallback
│
├── risk/
│   ├── sizing.py                 # PositionSizer: edge × confidence × vol-adjusted capital
│   ├── risk_engine.py            # Daily loss limit, exposure caps, circuit breaker
│   └── kill_switch.py            # 3-tier escalation: API errors → stale feed → drawdown
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

## Entry Points

| Command | Description |
|---|---|
| `python bot.py` | Run the bot (paper mode by default) |
| `python -m analytics.performance_report` | Post-session P&L, Sharpe, win rate |
| `python -m analytics.fill_quality` | Fill ratio and adverse selection stats |
| `python -m analytics.param_optimizer` | Grid/random search over key parameters |
| `python -m analytics.walk_forward` | Overfit detection across time windows |
| `python -m agent_loop.orchestrator cycle` | Run one supervised improvement cycle |

---

## ⚠️ CRITICAL BUGS — Fix These First

These bugs silently kill profitability. Fix them before any tuning.

### Bug 1: Config Params Not Loading From .env

`config.py` lines 88–92 **hardcode** `LATENCY_ARB_WINDOW_SECS` and `LATENCY_ARB_MIN_DISCOUNT` as literal values. Any `.env` overrides are silently ignored.

```python
# BROKEN (current — config.py lines 88-92):
LATENCY_ARB_WINDOW_SECS = 60
LATENCY_ARB_MIN_DISCOUNT = 0.05

# FIX:
LATENCY_ARB_WINDOW_SECS = int(os.getenv("LATENCY_ARB_WINDOW_SECS", "60"))
LATENCY_ARB_MIN_DISCOUNT = float(os.getenv("LATENCY_ARB_MIN_DISCOUNT", "0.05"))
```

### Bug 2: CERTAINTY_GAP_PCT Hardcoded in Strategy

`latency_arb.py` line 45 hardcodes `CERTAINTY_GAP_PCT = 0.005`. Not configurable. Must be moved to `config.py` with `os.getenv()`.

### Bug 3: API Kill-Switch Never Triggers

`kill_switch.py` has `record_api_call()` but `client.py` **never calls it**. API error rate is always 0%. Tiers 1–3 for API errors are dead.

**Fix:** In `client.py`, after every HTTP request, call `self.kill_switch.record_api_call(success=resp.status < 400)`.

### Bug 4: Exposure Tracking Disconnected

`risk_engine.py` has `add_exposure()` / `remove_exposure()` but `latency_arb.py` never calls them. `RISK_MAX_PORTFOLIO_EXPOSURE` is never enforced.

**Fix:** Wire `add_exposure()` in `execute()` and `remove_exposure()` on resolution/cancel.

### Bug 5: Trade Logger Missing Realized Edge

`trade_logger.py` has `update_realized_edge()` (line 175–214) and a `realized_edge` field (line 102) — but **nothing ever calls it**. The analytics pipeline is blind to actual performance.

### Bug 6: Execution Manager Live Controls Dead

`execution_manager.py` line 54–58 defines `_LIVE_CONTROLS` but it's **never used**. Line 61–62 redefines a different variable `_LIVE_CONTROLS_ENABLED`. The first definition is orphaned dead code.

Also: `record_book_update()`, `record_spread()`, and `record_signal_price()` exist in execution_manager.py but **no strategy calls them**. LiveRealism spread-stability and chase-detection data is never populated, making those gates unreliable.

### Bug 7: get_best_prices() Failure Returns Dangerous Defaults

`client.py` line 294 returns `(0.0, 1.0)` on failure. An ask of 1.0 passes the discount check and can cause the bot to buy a token at full price (zero profit). Should return `(0.0, 0.0)` or raise an exception.

---

## 🔴 PROFIT-KILLING GAPS — Priority Order

These are the gaps between this bot and a $263k/year bot. Each has a dollar estimate.

### Gap 1: BTC-Only Market Filter (Cost: ~70% of trade volume)

`latency_arb.py` line 129–132 only matches "btc" or "bitcoin". Vague-sourdough trades BTC, ETH, SOL, and XRP. This single filter **throws away 70% of available crypto binary markets.**

**Fix:** Rename `_parse_btc_market()` to `_parse_crypto_market()`:
```python
_CRYPTO_KEYWORDS = {
    "btc": "btcusdt", "bitcoin": "btcusdt",
    "eth": "ethusdt", "ethereum": "ethusdt",
    "sol": "solusdt", "solana": "solusdt",
    "xrp": "xrpusdt", "ripple": "xrpusdt",
}

def _parse_crypto_market(self, market: Market) -> Optional[CryptoMarket]:
    q = market.question.lower()
    matched_asset = None
    for keyword, symbol in _CRYPTO_KEYWORDS.items():
        if keyword in q:
            matched_asset = symbol
            break
    if not matched_asset:
        return None
    # ... rest of parsing unchanged, threshold regex already works for any number
```

Also requires multi-asset price feeds — see Gap 2.

**Expected impact: 3–4× more trades/day** at the same win rate.

### Gap 2: Single-Asset Price Feed (Cost: blocks Gap 1)

`price_feed.py` only connects to `btcusdt@trade`. Binance supports multi-stream:

```
wss://stream.binance.com:9443/ws/btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade
```

**Fix:** Generalize `BTCPriceFeed` into `CryptoPriceFeed` with a `prices: dict[str, float]` keyed by symbol. Parse `msg["s"]` to route prices. The strategy then looks up `self.feeds[matched_asset]` instead of `self.price_feed.price`.

### Gap 3: Hardcoded 0.5% Certainty Gap (Cost: ~40% of marginal trades)

`latency_arb.py` line 45: `CERTAINTY_GAP_PCT = 0.005`. At $95k BTC, this requires a $475 gap. Many profitable trades exist at 0.2–0.3% gaps with only seconds remaining.

**Fix:** Make configurable + time-adjusted:
```python
CERTAINTY_GAP_PCT = float(os.getenv("CERTAINTY_GAP_PCT", "0.003"))

# In _evaluate():
time_factor = max(0.4, secs_remaining / WINDOW_SECS)
adjusted_gap = CERTAINTY_GAP_PCT * time_factor
if gap_pct < adjusted_gap:
    return None
```

With 5 seconds left, a 0.12% gap is sufficient (the price won't move that far). With 55 seconds left, require the full 0.3%.

### Gap 4: 5% Minimum Discount Too High (Cost: ~30% of opportunities)

`config.py` line 92: `LATENCY_ARB_MIN_DISCOUNT = 0.05`. After slippage, only the fattest opportunities qualify. Vague-sourdough likely uses 2–3%.

**Recommended:** Set `LATENCY_ARB_MIN_DISCOUNT=0.03` in `.env` (after fixing Bug 1).

### Gap 5: Sequential Trade Execution (Cost: ~500ms per cycle)

`latency_arb.py` lines 359–361 and `bot.py` lines 507–508 execute trades sequentially:
```python
# SLOW (current):
for trade in trades:
    await self.latency_arb.execute(trade)

# FAST:
if trades:
    await asyncio.gather(*[self.latency_arb.execute(t) for t in trades])
```

Each trade hits the API (~100–200ms). With 3 simultaneous opportunities, sequential costs 300–600ms of the 60-second window.

### Gap 6: No Time-Decay Position Scaling (Cost: ~12% avg return)

Sizer treats a trade at T-55s the same as T-5s. At T-5s, outcome certainty is >99.9% — position should be 1.5× larger.

**Fix in `_evaluate()`:**
```python
time_urgency = 1.0 - (secs_remaining / WINDOW_SECS)
time_multiplier = 1.0 + (time_urgency * 0.5)  # up to 1.5× at final seconds
size_usdc = min(size_usdc * time_multiplier, config.SIZE_MAX_USDC)
```

### Gap 7: 120-Second Market Cache (Cost: ~10 trades/day)

`latency_arb.py` line 99: `_market_refresh_interval = 120.0`. New 15-minute markets appear constantly. With 120s staleness, the bot misses the first scan pass on fresh markets.

**Fix:** Reduce to 45–60 seconds. The market list is already cached in `bot.py` — this just re-parses it. Minimal API cost.

Also fix `bot.py` line 412: the market refresh timer sleeps a fixed 120 seconds. Reduce to 60.

### Gap 8: 5-Second Poll Interval Always (Cost: ~20 trades/day)

`bot.py` latency arb loop polls every 5 seconds regardless of how close the nearest market is to expiry.

**Fix:** Adaptive polling:
```python
# If any market expires in <30s, poll every 1s
nearest_expiry = min(secs_remaining for each tracked market)
poll_interval = 1.0 if nearest_expiry < 30 else 3.0 if nearest_expiry < 60 else 5.0
await asyncio.sleep(poll_interval)
```

### Gap 9: Confidence Floor Blocks Edge-Case Trades (Cost: ~15% of trades)

`sizing.py` line 197–198: trades with `confidence < 0.20` get `size=0`. But latency arb confidence = `gap_pct / (CERTAINTY_GAP_PCT × 4)`. At the minimum gap (0.5%), confidence = 0.125 — **below the floor**. These trades are profitable but blocked.

**Fix:** Either lower `CONFIDENCE_FLOOR` to 0.10 or change the confidence formula:
```python
# Better formula: sigmoid-like scaling
confidence = min(1.0, (gap_pct / CERTAINTY_GAP_PCT) ** 0.7 / 4.0)
```

### Gap 10: Double Edge Gate (Cost: ~15% of medium-edge trades)

Two independent edge floors stack:
1. `sizing.py` line 193: `EDGE_FLOOR_BPS = 50` (0.5%)
2. `execution_manager.py` line 99: `EXEC_MIN_EDGE = 0.01` (1.0%)

A trade with 0.8% edge passes the sizer but gets blocked by execution. Redundant — one gate is enough.

**Fix:** Remove `EXEC_MIN_EDGE` gate for latency arb, or set `EXEC_MIN_EDGE=0.005` (0.5%) to match the sizer floor. Or bypass execution_manager entirely for latency arb since it places market orders directly.

### Gap 11: Spread Stability Gate Blocks Best Moments (Cost: unknown, potentially large)

`live_realism.py` line 208: If spread widened >50% in last 5 seconds, trade is blocked. **But spread widening is exactly when latency arb opportunities appear** — it means the book is repricing. This gate actively prevents the bot from trading at the most profitable moments.

**Fix:** Disable for latency arb: set `LIVE_SPREAD_STABILITY_SECS=0` in `.env`, or add a strategy-specific bypass.

### Gap 12: Default LIMIT Orders Instead of MARKET (Cost: missed fills)

`execution_manager.py` lines 145–148: Only uses MARKET orders when `confidence >= 0.80 OR urgency >= 0.90`. LatencyArb never sets urgency (defaults to 0.5). At minimum certainty gap, confidence = 0.25 → always LIMIT order. **LIMIT orders in a 60-second window often don't fill.**

**Fix:** Latency arb should always use MARKET orders (it already does via `client.place_market_order()` directly — but if routed through ExecutionManager, it gets downgraded to LIMIT).

### Gap 13: No Order Book Depth Check (Cost: 1–2% slippage)

`_evaluate()` fetches ask price but never checks depth. A $500 order into $50 of depth = severe adverse fill.

**Fix:** Add depth check before entry:
```python
depth = await self.client.get_depth(token_id)
if depth < 50.0:  # configurable LATENCY_ARB_MIN_DEPTH_USDC
    return None
# Also: cap size to 50% of available depth
size_usdc = min(size_usdc, depth * 0.5)
```

---

## Configuration Reference

All config lives in `.env` (loaded by `config.py`). **Fix Bug 1 first** or `.env` overrides for latency arb params have no effect.

### Latency Arb Parameters

| Variable | Default | Optimal | Description |
|---|---|---|---|
| `LATENCY_ARB_WINDOW_SECS` | `60` | `90` | Seconds before resolution to enter. Wider = more trades. |
| `LATENCY_ARB_MIN_DISCOUNT` | `0.05` | `0.03` | Min discount vs fair value. Lower = more trades, smaller edge. |
| `CERTAINTY_GAP_PCT` | `0.005` | `0.003` | Min spot-threshold gap %. Lower = more trades near threshold. |

### Position Sizing

| Variable | Default | Optimal | Description |
|---|---|---|---|
| `RISK_BUDGET_PCT` | `0.02` | `0.04` | Fraction of balance per trade. |
| `SIZE_MIN_USDC` | `5.0` | `5.0` | Min trade size. |
| `SIZE_MAX_USDC` | `500.0` | `1000.0` | Max trade size. Higher at scale. |
| `EDGE_FLOOR_BPS` | `50` | `30` | Min edge in bps. Lower catches more trades. |
| `CONFIDENCE_FLOOR` | `0.20` | `0.10` | Min confidence. Lower allows marginal trades. |

### Risk Management

| Variable | Default | Optimal | Description |
|---|---|---|---|
| `RISK_DAILY_LOSS_LIMIT` | `100.0` | `500.0` | Daily loss hard stop (scale with bankroll). |
| `RISK_MAX_DRAWDOWN` | `200.0` | `1000.0` | Session drawdown limit (scale with bankroll). |
| `RISK_MAX_MARKET_EXPOSURE` | `200.0` | `500.0` | Max per single market. |
| `RISK_MAX_PORTFOLIO_EXPOSURE` | `1000.0` | `5000.0` | Max total deployed. |
| `RISK_MAX_CONSECUTIVE_LOSSES` | `3` | `5` | Consecutive losses before cooldown. |
| `RISK_COOLDOWN_SECS` | `300` | `60` | Cooldown duration. 5min is too long — miss 30+ opportunities. |

### Execution

| Variable | Default | Optimal | Description |
|---|---|---|---|
| `EXEC_MIN_EDGE` | `0.010` | `0.005` | Redundant with EDGE_FLOOR. Lower or remove. |
| `LIVE_MIN_DEPTH_USDC` | `100.0` | `50.0` | Binary markets are thin. Too high blocks trades. |
| `LIVE_SPREAD_STABILITY_SECS` | `5` | `0` | Disable for latency arb — spread widening IS the opportunity. |
| `LIVE_MAX_ORDER_RATE` | `10` | `30` | Orders/minute. 10 is too low for high-frequency. |

### Optimal .env for Vague-Sourdough Scale

```env
# Strategy
LATENCY_ARB_WINDOW_SECS=90
LATENCY_ARB_MIN_DISCOUNT=0.03
CERTAINTY_GAP_PCT=0.003

# Sizing
RISK_BUDGET_PCT=0.04
SIZE_MAX_USDC=1000
EDGE_FLOOR_BPS=30
CONFIDENCE_FLOOR=0.10

# Risk (scale with your bankroll — these are for ~$10k)
RISK_DAILY_LOSS_LIMIT=500
RISK_MAX_DRAWDOWN=1000
RISK_MAX_MARKET_EXPOSURE=500
RISK_MAX_PORTFOLIO_EXPOSURE=10000
RISK_MAX_CONSECUTIVE_LOSSES=5
RISK_COOLDOWN_SECS=60

# Execution
EXEC_MIN_EDGE=0.005
LIVE_MIN_DEPTH_USDC=50
LIVE_SPREAD_STABILITY_SECS=0
LIVE_MAX_ORDER_RATE=30

# Always paper first
DRY_RUN=1
```

---

## Data Flow

```
Binance WebSocket (real-time BTC/ETH/SOL/XRP prices)
         │
         ▼
  CryptoPriceFeed.prices[asset]   ←  Coinbase REST fallback
         │
         ▼
  LatencyArbStrategy.scan_once()
    │  • filter markets: crypto threshold ("above $X" / "below $X")
    │  • match asset to price feed
    │  • check time remaining <= WINDOW_SECS
    │  • check gap >= CERTAINTY_GAP_PCT (time-adjusted)
    │  • check discount >= MIN_DISCOUNT
         │
         ▼
  PositionSizer.compute()
    │  • edge_bps = discount × 10,000
    │  • confidence = f(gap_pct, certainty_gap)
    │  • time_multiplier = 1.0 + urgency × 0.5
    │  • size = balance × RISK_BUDGET × edge × conf × vol × time
         │
         ▼
  asyncio.gather(*[execute(trade) for trade in trades])
    │  • client.place_market_order(token_id, "BUY", size_usdc)
         │
         ▼
  trade_logger.log_trade()   →   logs/quant_trades.csv
```

---

## Dead Code & Disconnected Wiring

These modules exist but are **not connected** to the latency arb strategy. They appear functional but are never called in the hot path. Knowing what's dead prevents wasted effort.

| Module | Status | What's Dead |
|---|---|---|
| `risk_engine.py` `add_exposure()` / `remove_exposure()` | Never called | Portfolio exposure limit is unenforced |
| `risk_engine.py` `update_vol()` / `realized_vol()` | Never called | Vol-targeting is dead; always `vol_scalar = 1.0` |
| `risk_engine.py` `size_position()` | Never called | Orphaned; sizing is done by `sizing.py` instead |
| `kill_switch.py` `record_api_call()` | Never called | API error tiers 1–3 always show 0% error rate |
| `trade_logger.py` `update_realized_edge()` | Never called | `realized_edge` always 0.0 in CSV |
| `execution_manager.py` `record_book_update()` | Never called | Spread stability data never populated |
| `execution_manager.py` `record_spread()` | Never called | Chase detection data never populated |
| `execution_manager.py` `record_signal_price()` | Never called | Signal-vs-fill comparison impossible |
| `health_state.py` `escalate()` / `recover()` | Never called | Only `set()` is used directly |
| `latency_arb.py` `stats()` | Never called | Strategy stats never displayed or logged |
| `latency_arb.py` lines 293–295 (sizer fallback) | Unreachable | Sizer is always set by `bot.py` before `run()` |

**Why this matters for Claude:** If asked to "improve execution quality" or "add vol-targeting", the infrastructure already exists but needs to be **wired**, not rewritten.

---

## Key Modules In Depth

### `strategies/latency_arb.py` — The Strategy (386 lines)

**Classes:**
- `BTCMarket` (line 66) — parsed market with threshold, direction, resolution time, token IDs
- `LatencyArbTrade` (line 76) — opportunity with side, size, entry price, expected profit
- `LatencyArbStrategy` (line 88) — main class with `run()` loop

**Hot path (lines called every 5 seconds):**
- `scan_once()` (line 200) — scans all markets, returns opportunities
- `_evaluate()` (line 227) — core opportunity detection per market
- `execute()` (line 324) — places market buy order

**Key decision points:**
- Line 129–132: Asset filter (BTC only — **must extend**)
- Line 215–217: Fresh price gate (aborts entire scan if stale)
- Line 242–243: Time window gate
- Line 249–250: Certainty gap gate
- Line 270–271: Discount gate
- Line 286–291: Sizer blocking gate

### `feeds/price_feed.py` — Price Feed (140 lines)

- Binance WS: `btcusdt@trade` only (line 21)
- `is_fresh`: updated < 5 seconds ago (line 62, hardcoded)
- `is_stale`: no update in 60 seconds (line 69)
- Fallback chain: Binance REST → Coinbase REST (lines 112–139)
- **Must generalize to multi-asset** (see Gap 2)

### `client.py` — CLOB API (563 lines)

- `get_markets()` — paginated, fetches all active markets (lines 211–260)
- `get_best_prices(token_id)` — returns `(bid, ask)`, **returns (0.0, 1.0) on failure** (Bug 7)
- `place_market_order()` — signed taker order (lines 480–520)
- **Does not call kill_switch.record_api_call()** (Bug 3)
- Auth: Use `POLY_SIGNATURE_TYPE=2`. Do not pass `funder` for type 0 (fixed in `b10fbd7`).

### `risk/sizing.py` — Position Sizer (231 lines)

```
base   = balance × RISK_BUDGET_PCT
edge   = clamp(edge_bps / EDGE_REFERENCE_BPS, 0, 2)
conf   = clamp(confidence, CONFIDENCE_FLOOR, 1)
vol    = VOL_MULT_LOW | MID | HIGH
size   = clamp(base × edge × conf × vol, SIZE_MIN, SIZE_MAX)
```

**Gates that return size=0 (trade killed):**
1. `health_level >= SAFE_MODE` (line 188–190)
2. `edge_bps < EDGE_FLOOR_BPS` (line 193–194)
3. `confidence < CONFIDENCE_FLOOR` (line 197–198)

LatencyArb always passes `vol_regime="mid"` (vol_mult=1.0).

### `risk/risk_engine.py` — Risk Engine (383 lines)

**Active guards (cannot be disabled):**
- Daily loss limit: `-$100` default → circuit breaker OPEN (line 228–237)
- Max drawdown: `$200` default → circuit breaker OPEN, **no auto-reset** (line 240–249)
- Consecutive loss cooldown: 3 losses → 5 min pause (line 280–295)

**Dead/disconnected:**
- Exposure tracking (`add_exposure`, `remove_exposure`) — never called
- Vol-targeting (`update_vol`, `realized_vol`) — never called
- `size_position()` — orphaned, unused

### `risk/kill_switch.py` — 3-Tier (302 lines)

| Tier | API Errors | Stale Feed | Drawdown Pace | Effect |
|---|---|---|---|---|
| 1 | ≥20% | ≥30s | ≥20%/hr | Warning only |
| 2 | ≥40% | ≥90s | ≥50%/hr | 50% size reduction |
| 3 | ≥60% | ≥300s | ≥80%/hr | Block all entries |

**API error tracking is broken** — never wired. Only stale-feed and drawdown-pace triggers actually work.

### `health_state.py` — Health FSM (99 lines)

- `NORMAL` → trades allowed
- `DEGRADED` → trades allowed (reduced)
- `SAFE_MODE` → no new entries
- `CIRCUIT_BREAKER` → all orders cancelled, full stop

`ok_to_trade()` (line 97) returns True **only** in NORMAL state. Any degradation blocks all trades.

---

## Development Workflow

### 1. Paper Mode First (Always)
```bash
mkdir -p logs
DRY_RUN=1 python bot.py
```

### 2. Analyze Results
```bash
python -m analytics.performance_report   # win rate, Sharpe, P&L
python -m analytics.fill_quality         # fill ratio, adverse selection
```

### 3. Optimize Parameters
```bash
python -m analytics.param_optimizer      # grid/random search
python -m analytics.walk_forward         # overfit detection (target ratio < 0.3)
```

### 4. Live Deployment (Staged)
```bash
LIVE_DEPLOY_MODE=staircase_A DRY_RUN=0 python bot.py   # $5 max, 3 positions
LIVE_DEPLOY_MODE=staircase_B DRY_RUN=0 python bot.py   # $20 max, 5 positions
LIVE_DEPLOY_MODE=staircase_C DRY_RUN=0 python bot.py   # $50 max, 10 positions
LIVE_DEPLOY_MODE=production  DRY_RUN=0 python bot.py   # full caps
```

---

## Impact Scorecard — All Fixes Ranked

| # | Fix | Estimated Impact | Effort |
|---|-----|-----------------|--------|
| 1 | Multi-asset filter (Gap 1 + Gap 2) | **3–4× more trades/day** | 4 hours |
| 2 | Fix config loading (Bug 1 + Bug 2) | **Enables all tuning** | 15 min |
| 3 | Lower CERTAINTY_GAP_PCT + time-adjust (Gap 3) | **+40% eligible trades** | 30 min |
| 4 | Lower MIN_DISCOUNT to 0.03 (Gap 4) | **+30% opportunities** | 5 min |
| 5 | Parallel execution (Gap 5) | **+5–10 trades/hour** | 15 min |
| 6 | Time-decay sizing (Gap 6) | **+12% avg return/trade** | 30 min |
| 7 | Reduce market cache to 60s (Gap 7) | **+10 trades/day** | 5 min |
| 8 | Adaptive poll interval (Gap 8) | **+20 trades/day** | 30 min |
| 9 | Lower confidence floor to 0.10 (Gap 9) | **+15% more trades** | 5 min |
| 10 | Remove double edge gate (Gap 10) | **+15% medium-edge trades** | 10 min |
| 11 | Disable spread stability gate (Gap 11) | **Unblocks best moments** | 5 min |
| 12 | Fix get_best_prices failure default (Bug 7) | **Prevents full-price buys** | 10 min |
| 13 | Wire exposure tracking (Bug 4) | **Risk safety** | 1 hour |
| 14 | Wire kill-switch API tracking (Bug 3) | **Reliability** | 30 min |
| 15 | Wire trade logger realized edge (Bug 5) | **Enables analytics loop** | 30 min |
| 16 | Depth gating (Gap 13) | **–1–2% slippage** | 30 min |
| 17 | Reduce cooldown to 60s | **Miss 5 opps vs 30** | 5 min |

**Total estimated gain:** Current bot catches ~50–80 trades/day on BTC only.
After all fixes: ~300–500 trades/day across 4 assets, with better sizing and fewer blocked trades.
At vague-sourdough's ~$13.60 avg profit/trade → **~$4,000–$7,000/day** at scale.

---

## Code Conventions

### Async/Await
- All strategies are async; use `asyncio.sleep()`, never `time.sleep()`
- Multiple trades in one cycle: `asyncio.gather()`, not `for` loop
- Shared resources passed by reference, not global

### Error Handling
- Catch specific exceptions; never bare `except:`
- API failures update `health_state`, don't crash the bot
- Log with context: `log.error("msg: %s", e, exc_info=True)`

### Naming
- `snake_case` — functions, variables, modules
- `CamelCase` — classes
- `UPPER_CASE` — constants in `config.py`
- `_underscore_prefix` — private helpers

### Adding Config Variables
1. Add to `config.py` with `os.getenv(...)` and a safe default
2. Document in `.env.example`
3. Add to `controller_policy.json` `forbidden` list if safety-critical
4. **Never hardcode values** that affect trade frequency or sizing

---

## Safety Rules (Never Violate)

1. **Never set `DRY_RUN=0` automatically.** Live mode requires explicit operator action.
2. **Never modify `PRIVATE_KEY` or `WALLET_ADDRESS` in code.** Only from `.env`.
3. **Never place orders without the edge check.** The discount gate is the profit filter.
4. **Never bypass health state checks.** Respect `CIRCUIT_BREAKER` and `SAFE_MODE`.
5. **Never auto-apply `logs/deployment_plan.json`.** Requires operator review.
6. **Never use synchronous blocking calls inside async loops.**
7. **Never add risk/drawdown limits to `controller_policy.json` `tunable` list.**

---

## External Services

| Service | URL | Auth | Purpose |
|---|---|---|---|
| Polymarket CLOB | `https://clob.polymarket.com` | Private key signing | Orders and market data |
| Binance WS | `wss://stream.binance.com:9443/ws/btcusdt@trade` | None | BTC price (extend to multi-stream) |
| Coinbase REST | `https://api.coinbase.com/v2/prices/BTC-USD/spot` | None | BTC fallback |
| Polygon RPC | `https://polygon-rpc.com` | None | On-chain state (optional) |

**Multi-asset stream:** `wss://stream.binance.com:9443/ws/btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade`

---

## Common Gotchas

- **Config hardcoding**: `LATENCY_ARB_WINDOW_SECS` and `LATENCY_ARB_MIN_DISCOUNT` don't load from `.env`. Fix before tuning.
- **`ClobClient` EOA mode**: Don't pass `funder` for `POLY_SIGNATURE_TYPE=0`. Fixed in `b10fbd7`.
- **`get_best_prices()` failure**: Returns `(0.0, 1.0)` — ask of 1.0 can pass discount checks. Fix to `(0.0, 0.0)`.
- **Market cache**: Refreshes every 120s. Don't call `client.get_markets()` inside the scan loop.
- **`logs/` directory**: Must exist. `mkdir -p logs` before running.
- **Price feed freshness**: `is_fresh` = updated < 5s. If stale, entire scan cycle returns empty.
- **Spread stability gate**: Blocks trades when spread is widening — exactly when latency arb is most profitable. Disable with `LIVE_SPREAD_STABILITY_SECS=0`.
- **Live depth gate**: `LIVE_MIN_DEPTH_USDC=100` is too high for thin binary markets. Set to 50.
- **Cooldown after 3 losses**: 5-minute pause (300s). Missing ~30 opportunities. Reduce to 60s.
- **QUANT_MODE_ENABLED**: Default 0. Advanced risk/execution logic is disabled. Enable with `QUANT_MODE_ENABLED=1` but beware of the double edge gate (Gap 10).
