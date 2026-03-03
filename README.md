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

---

## Architecture

```
bot.py                   — Orchestrator; spawns all async tasks
├── client.py            — CLOB API wrapper (aiohttp + py-clob-client)
├── feeds/price_feed.py  — Binance WS → Coinbase REST BTC/USD feed
├── strategies/
│   ├── price_arb.py     — Price arbitrage
│   ├── latency_arb.py   — Latency arbitrage
│   └── market_maker.py  — Market making (signal-engine integrated)
├── signals/
│   └── signal_engine.py — Multi-factor signal: micro + momentum + mean-rev
├── risk/
│   └── risk_engine.py   — Vol-targeting, caps, drawdown/loss circuit breakers
├── execution/
│   └── execution_manager.py — Adaptive maker/taker, edge check, timeout mgr
├── health_state.py      — Bot-wide health level (NORMAL/DEGRADED/SAFE_MODE/CB)
└── analytics/
    ├── trade_logger.py        — Quant trade log (logs/quant_trades.csv)
    └── performance_report.py  — Performance summary CLI
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

# Custom CSV:
python -m analytics.performance_report --csv logs/quant_trades.csv

# Verbose (trade-by-trade P&L bar chart):
python -m analytics.performance_report -v

# Custom fee/slippage assumptions:
python -m analytics.performance_report --fee 0.02 --slip 0.003
```

---

## Logs

| File | Contents |
|---|---|
| `logs/bot.log` | Rotating main log (5 MB x 5 files) |
| `logs/orders.csv` | All orders placed (basic client log) |
| `logs/quant_trades.csv` | Rich quant log with signal + risk reason codes |
| `logs/perf.json` | Live performance snapshot (written every 60s) |
