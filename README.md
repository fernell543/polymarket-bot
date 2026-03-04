# Polymarket Bot

An async Python trading bot for [Polymarket](https://polymarket.com) with three
concurrent strategies and a layered quant upgrade system.

---

## Strategies

| Strategy | Description |
|---|---|
| **PriceArb** | Buys YES + NO when combined cost < $1.00 − fees. Guaranteed locked profit. |
| **LatencyArb** | Near-expiry BTC threshold markets: enter when spot clearly exceeds threshold. |
| **MarketMaker** | Two-sided quotes on reward-earning markets; earns spread + liquidity rewards. |

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill environment file
cp .env.example .env
# Edit .env: set PRIVATE_KEY, WALLET_ADDRESS

# 3. Paper run (safe default — DRY_RUN=1)
python bot.py

# 4. Live trading
DRY_RUN=0 python bot.py
```

---

## Risk-Based Sizing (how it works + knobs)

Every order size is computed dynamically by `risk/sizing.py` rather than using
hardcoded constants.  There is **no martingale logic** — sizes only scale with
the quality of the trade, never to recover prior losses.

### Formula

```
edge_scalar = clamp(edge_bps / EDGE_REFERENCE_BPS, 0, 2.0)
size        = base_usdc × edge_scalar × confidence × vol_mult
size        = clamp(size, SIZE_MIN_USDC, SIZE_MAX_USDC)
```

Where `base_usdc` is:
- **PriceArb / LatencyArb**: `balance × RISK_BUDGET_PCT` (risk-budget approach)
- **MarketMaker**: `balance × MM_CAPITAL_PCT / (markets × 4)` (capital-fraction, same as before)

### Protection guards (size=0 if any trigger)

| Guard | Condition | Reason code |
|---|---|---|
| Health | `health >= SAFE_MODE` | `safe_mode` |
| Edge floor | `edge_bps < EDGE_FLOOR_BPS` | `edge_below_floor` |
| Confidence floor | `confidence < CONFIDENCE_FLOOR` | `confidence_below_floor` |

### Sizing knobs

| Variable | Default | Description |
|---|---|---|
| `RISK_BUDGET_PCT` | `0.02` | Fraction of balance risked per directional trade (2%) |
| `SIZE_MIN_USDC` | `5.0` | Absolute floor on any computed size |
| `SIZE_MAX_USDC` | `500.0` | Hard ceiling on any computed size |
| `VOL_MULT_LOW` | `1.2` | Size multiplier in low-vol / ranging markets |
| `VOL_MULT_MID` | `1.0` | Size multiplier at baseline |
| `VOL_MULT_HIGH` | `0.6` | Size multiplier in high-vol / trending markets |
| `CONFIDENCE_FLOOR` | `0.20` | Signal confidence below this → size=0 |
| `EDGE_FLOOR_BPS` | `50.0` | Net edge below 50 bps (0.5%) → size=0 |
| `EDGE_REFERENCE_BPS` | `100.0` | Edge at this BPS → edge_scalar=1.0 (baseline) |

### Before vs after

| Strategy | Before | After |
|---|---|---|
| PriceArb | `min(MAX_POSITION / 2, 100)` per leg — fixed $100 | `balance × RISK_BUDGET_PCT × edge_scalar × 1.0 × VOL_MULT_LOW / 2` per leg |
| LatencyArb | `min(MAX_POSITION, 200)` — fixed $200 | `balance × RISK_BUDGET_PCT × edge_scalar × confidence × VOL_MULT_MID` |
| MarketMaker | `balance × MM_CAPITAL_PCT / slots × kill_switch_mult` | Same base, now gated by edge / confidence / health guards; regime adjusts vol_mult |

With default `$5,000` balance and `RISK_BUDGET_PCT=0.02` (`base=$100`):
- PriceArb at 3% profit: `$100 × 2.0 × 1.0 × 1.2 / 2 = $120` per leg (was $100)
- LatencyArb at 28% discount, full certainty: `$100 × 2.0 × 1.0 × 1.0 = $200` (same; scales with balance)
- MarketMaker at 2% spread: base `$50`, edge_scalar=1.0, unchanged unless signal or health blocks

---

## Configuration Reference

All settings can be set as environment variables or in `.env`.

### Core
| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `1` | `1` = paper mode (no real orders); `0` = live |
| `PRIVATE_KEY` | — | Ethereum private key (live mode only) |
| `WALLET_ADDRESS` | — | Polygon wallet address (live mode only) |
| `MAX_POSITION_USDC` | `500` | Hard cap per order in USDC |
| `MIN_MARKETS_THRESHOLD` | `10` | Minimum market count before safe-mode engages |

### Strategy Parameters
| Variable | Default | Description |
|---|---|---|
| `ARB_MIN_PROFIT_PCT` | `0.03` | Minimum net profit % for price-arb to trigger |
| `MARKET_MAKER_SPREAD` | `0.02` | MM quote spread (2%) |
| `MM_CAPITAL_PCT` | `0.20` | Fraction of balance deployed in MM orders |
| `MM_TARGET_MARKETS` | `5` | How many reward markets to quote simultaneously |
| `LATENCY_ARB_WINDOW_SECS` | `60` | Enter latency-arb within N seconds of expiry |
| `LATENCY_ARB_MIN_DISCOUNT` | `0.05` | Min price discount vs fair value |

### Quant Mode
| Variable | Default | Description |
|---|---|---|
| `QUANT_MODE_ENABLED` | `0` | **Master switch** — set `1` to enable all quant logic |
| `SIGNAL_CONFIDENCE_THRESHOLD` | `0.40` | Minimum signal confidence to open a new position |
| `SIGNAL_WEIGHT_MICRO` | `0.30` | Weight: order-book microstructure factor |
| `SIGNAL_WEIGHT_MOMENTUM` | `0.35` | Weight: short-horizon momentum factor |
| `SIGNAL_WEIGHT_MEAN_REV` | `0.35` | Weight: mean-reversion z-score factor |
| `REGIME_HIGH_VOL_THRESHOLD` | `0.02` | Rolling vol/mean ratio above this → trending regime |
| `RISK_TARGET_VOL` | `0.05` | Vol-targeting: target daily vol fraction |
| `RISK_MAX_MARKET_EXPOSURE` | `200` | Max USDC per single market |
| `RISK_MAX_PORTFOLIO_EXPOSURE` | `1000` | Max total USDC deployed |
| `RISK_DAILY_LOSS_LIMIT` | `100` | Circuit breaker: max daily loss (USDC) |
| `RISK_MAX_DRAWDOWN` | `200` | Circuit breaker: max session drawdown (USDC) |
| `RISK_MAX_CONSECUTIVE_LOSSES` | `3` | After N losses → cooldown pause |
| `RISK_COOLDOWN_SECS` | `300` | Cooldown duration (seconds) |
| `EXEC_MIN_EDGE` | `0.010` | Min net edge (after fees+slippage) to place order |
| `EXEC_TAKER_CONFIDENCE_THRESHOLD` | `0.80` | Use market order if signal confidence >= this |
| `EXEC_TAKER_URGENCY_THRESHOLD` | `0.90` | Use market order if urgency >= this |
| `EXEC_ORDER_TIMEOUT_SECS` | `120` | Cancel unmatched limit orders after N seconds |

### Performance Pass 2 — Profiles, Kill-Switch, Optimizer
| Variable | Default | Description |
|---|---|---|
| `PARAM_PROFILE` | `auto` | `auto` / `conservative` / `balanced` / `aggressive` — regime-adaptive profile |
| `KS_TIER1_API_ERROR_RATE` | `0.20` | Kill-switch Tier 1 (warn): API error rate threshold |
| `KS_TIER2_API_ERROR_RATE` | `0.40` | Kill-switch Tier 2 (reduce 50%): API error rate threshold |
| `KS_TIER3_API_ERROR_RATE` | `0.60` | Kill-switch Tier 3 (no-trade): API error rate threshold |
| `KS_TIER1_STALE_SECS` | `30` | Kill-switch Tier 1: price-feed staleness (seconds) |
| `KS_TIER2_STALE_SECS` | `90` | Kill-switch Tier 2: price-feed staleness (seconds) |
| `KS_TIER3_STALE_SECS` | `300` | Kill-switch Tier 3: price-feed staleness (seconds) |
| `KS_TIER1_DD_PACE_PCT` | `0.20` | Kill-switch Tier 1: drawdown pace (fraction of daily limit per hour) |
| `KS_TIER2_DD_PACE_PCT` | `0.50` | Kill-switch Tier 2: drawdown pace |
| `KS_TIER3_DD_PACE_PCT` | `0.80` | Kill-switch Tier 3: drawdown pace |

---

## Architecture

```
bot.py                   — Orchestrator; spawns all async tasks + kill-switch loop
├── client.py            — CLOB API wrapper (aiohttp + py-clob-client)
├── feeds/price_feed.py  — Binance WS → Coinbase REST BTC/USD feed
├── strategies/
│   ├── price_arb.py     — Price arbitrage
│   ├── latency_arb.py   — Latency arbitrage
│   └── market_maker.py  — Market making (signal-engine + kill-switch size gate)
├── signals/
│   └── signal_engine.py — Multi-factor signal: micro + momentum + mean-rev
├── risk/
│   ├── risk_engine.py   — Vol-targeting, caps, drawdown/loss circuit breakers
│   ├── kill_switch.py   — 3-tier kill switch: API errors / stale data / DD pace
│   └── param_profiles.py — Conservative / balanced / aggressive regime profiles
├── execution/
│   └── execution_manager.py — Adaptive maker/taker, edge check, latency tracking
├── health_state.py      — Bot-wide health (NORMAL/DEGRADED/SAFE_MODE/CB) + size_multiplier
└── analytics/
    ├── trade_logger.py        — Quant trade log (spread_at_entry, submit_latency_ms)
    ├── performance_report.py  — Performance summary CLI
    ├── fill_quality.py        — Fill ratio, cancel ratio, adverse selection CLI
    ├── param_optimizer.py     — Grid/random search over key params; outputs CSV+JSON
    └── walk_forward.py        — Window-based overfit + stability validation CLI
```

### Health State Machine

```
NORMAL -> DEGRADED -> SAFE_MODE -> CIRCUIT_BREAKER
                                        |
                              (manual restart required)
```

| Level | New Trades | Maintenance (cancel/requote) |
|---|---|---|
| `NORMAL` | Yes | Yes |
| `DEGRADED` | Conservative | Yes |
| `SAFE_MODE` | No | Yes |
| `CIRCUIT_BREAKER` | No | No |

Safe-mode triggers: market list < threshold, price feed stale, risk cooldown active.
Circuit-breaker triggers: daily loss > limit OR drawdown > max.

---

## Quant Mode Rollout Plan

### Phase 1 — Paper-Only Validation (minimum 5 days)

**Goal**: Validate signal quality and risk parameters with zero real capital.

```bash
# Phase 1 paper command (copy/paste ready)
QUANT_MODE_ENABLED=1 \
SIGNAL_CONFIDENCE_THRESHOLD=0.50 \
RISK_DAILY_LOSS_LIMIT=50 \
RISK_MAX_DRAWDOWN=100 \
python bot.py
```

**Collect metrics** after running:
```bash
python -m analytics.performance_report
```

**Phase 1 pass criteria** (all must be met before proceeding):
- [ ] Win rate >= 52% across >= 100 simulated trades
- [ ] Average expected edge >= 0.015 (1.5%)
- [ ] Sharpe-like ratio >= 0.5
- [ ] Max simulated drawdown <= 20% of paper starting balance
- [ ] Zero crashes or data-integrity incidents over 5 days
- [ ] Signal regime not stuck in "unknown" > 30% of trades

---

### Phase 2 — Small Live Caps (after Phase 1 passes)

**Goal**: Validate live execution, fee costs, and fill rates with minimal risk.

```bash
# Phase 2 live command — small caps
DRY_RUN=0 \
QUANT_MODE_ENABLED=1 \
MAX_POSITION_USDC=25 \
MM_CAPITAL_PCT=0.05 \
RISK_MAX_MARKET_EXPOSURE=50 \
RISK_MAX_PORTFOLIO_EXPOSURE=150 \
RISK_DAILY_LOSS_LIMIT=25 \
RISK_MAX_DRAWDOWN=50 \
SIGNAL_CONFIDENCE_THRESHOLD=0.50 \
python bot.py
```

**Phase 2 pass criteria** (minimum 10 live days):
- [ ] Realized edge >= 80% of expected edge (fill quality)
- [ ] Live win rate within 5 pp of paper win rate
- [ ] No circuit-breaker trips in first 5 days
- [ ] Net PnL positive after fees/slippage on rolling 7-day window

---

### Phase 3 — Scale Conditions

**Goal**: Increase position sizing after Phase 2 metrics are sustained for 30 days.

**Hard prerequisites**:
- [ ] Phase 2 metrics sustained >= 30 consecutive live days
- [ ] Live Sharpe-like ratio >= 0.6
- [ ] Max live drawdown <= 15% of deployed capital
- [ ] No unhandled exceptions or stale-data incidents

**Incremental scale steps** (only one step per week, monitor after each):
1. `MAX_POSITION_USDC=50`, `RISK_MAX_PORTFOLIO_EXPOSURE=300`
2. `MAX_POSITION_USDC=100`, `RISK_MAX_PORTFOLIO_EXPOSURE=600`
3. `MAX_POSITION_USDC=200`, `RISK_MAX_PORTFOLIO_EXPOSURE=1000`

---

### Kill-Switch Checklist

Stop the bot immediately if **any** of the following occur:

- [ ] Daily P&L < -`RISK_DAILY_LOSS_LIMIT` (circuit breaker should auto-fire)
- [ ] Session drawdown > `RISK_MAX_DRAWDOWN` (circuit breaker should auto-fire)
- [ ] BTC price feed offline > 5 minutes (bot enters SAFE_MODE automatically)
- [ ] Market cache < 10 markets for > 2 consecutive refresh cycles
- [ ] Any unhandled exception in live order placement
- [ ] Realized edge < 40% of expected edge over a 20-trade rolling window
- [ ] External event: Polymarket API changes, UMA oracle incident, network outage

**Manual kill** (Ctrl+C gracefully cancels all open orders):
```bash
# Graceful shutdown — cancels all open orders:
Ctrl+C

# Immediate force-kill (skips cancel — manually cancel via Polymarket UI after):
kill -9 <bot_pid>
```

---

## Running the Performance Report

```bash
# Default (reads logs/quant_trades.csv):
python -m analytics.performance_report

# Verbose (trade-by-trade P&L bar chart):
python -m analytics.performance_report -v

# Fill quality + latency report:
python -m analytics.fill_quality

# Custom fee/slippage assumptions:
python -m analytics.performance_report --fee 0.02 --slip 0.003
```

---

## Daily Tuning Workflow (Paper → Live)

### Tonight — paper collection + analysis

**Step 1: Run paper super mode** (collect paper trades for 2–4+ hours)
```powershell
.\run_paper_super.ps1
# Options: -Profile conservative|balanced|aggressive|auto
#          -ConfThreshold 0.50 -MinEdge 0.015
```

**Step 2: Optimize parameters** (after ≥50 trades)
```powershell
.\run_paper_optimize.ps1
# Outputs: logs/opt_results.csv, logs/opt_best.json
# Options: -Mode random -NSamples 1000 -DdPenalty 1.0
```

**Step 3: Validate against overfitting**
```powershell
.\run_walkforward.ps1
# Outputs: per-window stats, overfit ratio, stability score
# Options: -Windows 6 -Verbose
```

**Step 4: Full analytics pass**
```bash
python -m analytics.performance_report
python -m analytics.fill_quality
```

---

### Tomorrow — small live gate (only after paper criteria met)

**Small live gate criteria (ALL must pass — no exceptions):**
- [ ] Stability score ≥ 0.70 (from walk_forward output)
- [ ] Overfit ratio < 2.0
- [ ] Val Sharpe ≥ 0.30
- [ ] Win rate ≥ 52% in validation window
- [ ] Fill rate ≥ 70% (no excessive cancels)
- [ ] Adverse selection avg < +0.5% (from fill_quality output)
- [ ] Zero circuit-breaker trips in paper session

**If all criteria pass, use the Phase 2 live command from the rollout plan below.**
Do NOT force live if any criterion fails — collect more paper data instead.

---

## Kill-Switch Escalation Tiers

Operates independently of the hard circuit breaker, triggering proportional responses:

| Tier | Label | Action | Trigger |
|---|---|---|---|
| 0 | NORMAL | Full size (1.0×) | All signals nominal |
| 1 | WARN | Full size, log warning | API errors ≥20% or stale ≥30s or DD pace ≥20%/hr |
| 2 | REDUCE | Half size (0.5×) | API errors ≥40% or stale ≥90s or DD pace ≥50%/hr |
| 3 | NO-TRADE | Block new entries | API errors ≥60% or stale ≥300s or DD pace ≥80%/hr |

All thresholds are env-configurable: `KS_TIER1_API_ERROR_RATE`, `KS_TIER2_STALE_SECS`, etc.

---

## Regime-Adaptive Parameter Profiles

Three profiles selected automatically by market regime (or forced via `PARAM_PROFILE`):

| Profile | Conf Threshold | Min Edge | MM Spread | Trigger |
|---|---|---|---|---|
| `conservative` | 0.55 | 0.018 | 2.5% | regime=unknown |
| `balanced` | 0.42 | 0.012 | 2.0% | regime=ranging |
| `aggressive` | 0.32 | 0.008 | 1.5% | regime=trending |

Hard caps from the global risk engine are always enforced.

```bash
# Force a specific profile:
PARAM_PROFILE=conservative python bot.py

# Auto-select by regime (default):
PARAM_PROFILE=auto python bot.py
```

---

## Running the Performance Report

```bash
python -m analytics.performance_report
python -m analytics.performance_report -v
python -m analytics.fill_quality
python -m analytics.fill_quality -v
```

---

## Account details in dashboard

Every 60-second stats refresh (and at shutdown) the console output includes:

```
  Mode:          DRY_RUN (simulated execution)   ← or "LIVE"
  Wallet:        0xYourAddress                   ← or "(not set — WALLET_ADDRESS missing)"
  Balance:       $5,000.00 USDC (simulated)      ← or "(live)" / "(API unavailable …)"
```

**Behaviour by mode:**

| Situation | Mode line | Balance line |
|---|---|---|
| `DRY_RUN=1`, no wallet set | `DRY_RUN (simulated execution)` | `$5000.00 USDC (simulated)` |
| `DRY_RUN=1`, wallet set | `DRY_RUN (simulated execution)` | `$5000.00 USDC (simulated)` |
| `DRY_RUN=0`, wallet set | `LIVE` | real balance from API, e.g. `$312.45 USDC (live)` |
| `DRY_RUN=0`, API down | `LIVE` | `$0.00 USDC (API unavailable (…))` |
| `DRY_RUN=0`, no wallet set | aborts at startup | n/a |

`MM_STARTING_BALANCE` (default `5000`) controls the simulated paper balance.
The same fields (`mode`, `wallet`, `balance_usdc`, `balance_note`) are written to `logs/perf.json` on every cycle.

---

## Logs

| File | Contents |
|---|---|
| `logs/bot.log` | Rotating main log (5 MB x 5 files) |
| `logs/orders.csv` | All orders placed (basic client log) |
| `logs/quant_trades.csv` | Rich quant log with signal + risk reason codes |
| `logs/perf.json` | Live performance snapshot (written every 60s) — includes `mode`, `wallet`, `balance_usdc` |
| `logs/opt_results.csv` | Parameter optimizer results (ranked configs) |
| `logs/opt_best.json` | Top-3 configs from last optimizer run |
