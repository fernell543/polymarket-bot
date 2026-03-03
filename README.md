# polymarket-bot

An async Python trading bot for [Polymarket](https://polymarket.com) using the CLOB API.
Runs three concurrent strategies: price arbitrage, latency arbitrage, and market making.

---

## Strategies

| Strategy | Description |
|---|---|
| **PriceArb** | Buys YES+NO when combined price < $1.00 (risk-free arb) |
| **LatencyArb** | Enters near-certain BTC threshold markets in the final 60 s |
| **MarketMaker** | Posts two-sided quotes on reward-earning markets to earn spread + DAILY rewards |

---

## Quick start

```bash
cp .env.example .env          # fill in PRIVATE_KEY + WALLET_ADDRESS
pip install -r requirements.txt
python bot.py                 # DRY_RUN=1 by default — safe to run immediately
```

Set `DRY_RUN=0` in `.env` only when you are ready to trade with real funds.

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `PRIVATE_KEY` | — | Wallet private key (required for live trading) |
| `WALLET_ADDRESS` | — | Polygon wallet address |
| `DRY_RUN` | `1` | `1` = simulate orders only, `0` = live trading |
| `CLOB_HOST` | `https://clob.polymarket.com` | CLOB API base URL |
| `MAX_POSITION_USDC` | `500` | Maximum single position size |
| `MM_CAPITAL_PCT` | `0.20` | Fraction of balance deployed across MM orders |
| `MM_STARTING_BALANCE` | `5000` | Simulated balance for dry-run sizing |
| `MM_TARGET_MARKETS` | `5` | Number of markets to quote simultaneously |
| `MM_REBALANCE_INTERVAL` | `30` | Seconds between MM quote refreshes |
| `MARKET_MAKER_SPREAD` | `0.02` | Quoted bid-ask spread (2 %) |
| `ARB_MIN_PROFIT_PCT` | `0.03` | Minimum expected profit for price arb |
| `LATENCY_ARB_WINDOW_SECS` | `60` | Seconds-to-expiry window for latency arb entry |
| `LATENCY_ARB_MIN_DISCOUNT` | `0.05` | Minimum price discount vs fair value |
| `MIN_MARKETS_THRESHOLD` | `10` | Bot enters safe mode when fewer markets are available |

---

## Live safety checklist

Run through this list **before** setting `DRY_RUN=0`:

- [ ] Bot has run in `DRY_RUN=1` for at least one full session without errors
- [ ] `logs/bot.log` shows "All health checks passed" on startup
- [ ] `logs/orders.csv` from dry run looks correct (prices, sizes, sides)
- [ ] `PRIVATE_KEY` is an EOA wallet key (not a seed phrase)
- [ ] `WALLET_ADDRESS` matches the key (verify with `eth_account`)
- [ ] Wallet has USDC deposited on Polygon and approved for the CLOB contract
- [ ] `MAX_POSITION_USDC` is set to an amount you are comfortable losing entirely
- [ ] `MM_CAPITAL_PCT` × balance is within risk tolerance
- [ ] You understand Polymarket's 2 % fee on winnings (`POLYMARKET_FEE = 0.02`)
- [ ] You have reviewed the latency arb window — markets resolve in real-time; a single flip in BTC price near threshold can cause a full loss
- [ ] Log rotation is configured (`logs/bot.log` rotates at 5 MB × 5 backups)
- [ ] You have a plan to stop the bot quickly (Ctrl-C or `kill <pid>`)

---

## Startup health checks

On every startup the bot runs three checks and prints results to the log:

```
============================================================
  STARTUP HEALTH CHECKS
============================================================
  [OK]   PRIVATE_KEY present
  [OK]   WALLET_ADDRESS present
  [OK]   CLOB API reachable (1 market returned)
  [OK]   Auth client initialised
  All health checks passed — bot starting
============================================================
```

In **live mode**, a `[FAIL]` on any check aborts startup immediately.
In **dry-run mode**, failures print as warnings and the bot continues (so you can test connectivity without keys).

---

## Safe mode

The bot automatically suspends **new position entries** (while keeping the loop alive) when data confidence is low:

| Condition | Strategies paused | Recovery |
|---|---|---|
| Market list < `MIN_MARKETS_THRESHOLD` | All three | Automatic — next successful market refresh |
| BTC price feed has no data | LatencyArb | Automatic — when Binance/Coinbase connect |
| BTC price feed stale (> 60 s) | LatencyArb | Automatic — when feed recovers |

Safe-mode events are logged at `WARNING` level:
```
LatencyArb: SAFE MODE — BTC price stale (73.2s > 60s); no new entries until feed recovers
MarketMaker: SAFE MODE — market list too small (3 < 10), skipping rebalance
```

---

## Expected failure modes

| Symptom | Root cause | What the bot does |
|---|---|---|
| HTTP 502 on `/markets` | Transient Polymarket gateway error | Retries up to 3× with exponential backoff + jitter; keeps existing cache |
| `/markets` returns partial page | Mid-pagination network error | Stops pagination, returns markets collected so far; cache preserved if result is small |
| `/rewards/markets_earning_daily` → 405 | Endpoint not available in this environment | Latched disabled on first hit — no further calls or log spam |
| Binance WebSocket drops | Network blip | Reconnects automatically in 3 s; REST fallback available via `fetch_once()` |
| BTC price stale > 60 s | Extended feed outage | LatencyArb enters safe mode; MarketMaker and PriceArb unaffected |
| `get_balance_usdc` fails | API error | MarketMaker retains last known balance; logs at DEBUG level |
| `place_limit_order` fails | Auth error / bad params | Returns `PlacedOrder(success=False)`; logged at ERROR; bot continues |
| Startup: CLOB API unreachable | Network / DNS issue | Health check fails → live mode aborts; dry-run continues with warning |
| Startup: missing `PRIVATE_KEY` | `.env` not configured | Health check fails → live mode aborts with actionable error message |

---

## Logs

| File | Contents |
|---|---|
| `logs/bot.log` | All log output (rotates at 5 MB, 5 backups) |
| `logs/orders.csv` | Every order attempt (real and simulated) |
| `logs/perf.json` | MarketMaker stats snapshot (updated every 60 s) |
