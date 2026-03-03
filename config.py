import os
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

MAX_POSITION_USDC = float(os.getenv("MAX_POSITION_USDC", "500"))
ARB_MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.03"))
MARKET_MAKER_SPREAD = float(os.getenv("MARKET_MAKER_SPREAD", "0.02"))

# Dynamic position sizing: deploy this fraction of balance across all MM orders.
# order_size = balance * MM_CAPITAL_PCT / (MM_TARGET_MARKETS * 4 orders)
# e.g. $5000 * 0.20 / (5 * 4) = $50/order, $1000 total deployed (20% of balance)
MM_CAPITAL_PCT      = float(os.getenv("MM_CAPITAL_PCT", "0.20"))
MM_STARTING_BALANCE = float(os.getenv("MM_STARTING_BALANCE", "5000"))

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

POLYGON_CHAIN_ID = 137

# Polymarket fee is 2% of winnings
POLYMARKET_FEE = 0.02

# Binance WebSocket for BTC/USDT real-time trades
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"

# Coinbase Advanced Trade WebSocket
COINBASE_WS_URL = "wss://advanced-trade-api.coinbase.com/ws"

# Latency arb: enter position if this many seconds remain before market resolution
LATENCY_ARB_WINDOW_SECS = 60

# Latency arb: minimum price discount vs fair value to enter
LATENCY_ARB_MIN_DISCOUNT = 0.05

# Market making: rebalance interval in seconds
MM_REBALANCE_INTERVAL = 30

# How many top reward markets to target for market making
MM_TARGET_MARKETS = 5

# Minimum active-market count below which the bot enters safe mode (no new entries).
# Protects against acting on a severely truncated market list (e.g. API degraded).
MIN_MARKETS_THRESHOLD = int(os.getenv("MIN_MARKETS_THRESHOLD", "10"))


# ===========================================================================
# Quant Mode — all advanced logic disabled by default (safe for all phases)
# Set QUANT_MODE_ENABLED=1 in .env to activate.
# ===========================================================================

# Master switch: enables signal engine, vol-targeting, adaptive execution,
# daily-loss / drawdown circuit breakers, and consecutive-loss cooldowns.
QUANT_MODE_ENABLED = os.getenv("QUANT_MODE_ENABLED", "0") == "1"

# ---------------------------------------------------------------------------
# Signal layer
# ---------------------------------------------------------------------------

# Factor weights — must not need to sum exactly to 1; they are applied
# relative to each other within the regime-gated composite formula.
SIGNAL_WEIGHT_MICRO    = float(os.getenv("SIGNAL_WEIGHT_MICRO",    "0.30"))
SIGNAL_WEIGHT_MOMENTUM = float(os.getenv("SIGNAL_WEIGHT_MOMENTUM", "0.35"))
SIGNAL_WEIGHT_MEAN_REV = float(os.getenv("SIGNAL_WEIGHT_MEAN_REV", "0.35"))

# Minimum signal confidence [0..1] required to open a new position.
# Lower = more trades, lower average quality.  Start conservative (0.40).
SIGNAL_CONFIDENCE_THRESHOLD = float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "0.40"))

# Rolling price std/mean above this ratio → "trending" regime
# (momentum-dominant).  Below → "ranging" (mean-reversion-dominant).
REGIME_HIGH_VOL_THRESHOLD = float(os.getenv("REGIME_HIGH_VOL_THRESHOLD", "0.02"))

# ---------------------------------------------------------------------------
# Risk engine
# ---------------------------------------------------------------------------

# Target annualised-ish volatility for vol-targeting (fraction of notional).
# 0.05 = 5% daily vol target.  Lower = smaller positions in volatile markets.
RISK_TARGET_VOL = float(os.getenv("RISK_TARGET_VOL", "0.05"))

# Maximum USDC deployed in any single market at one time.
RISK_MAX_MARKET_EXPOSURE = float(os.getenv("RISK_MAX_MARKET_EXPOSURE", "200.0"))

# Maximum total USDC deployed across the entire portfolio.
RISK_MAX_PORTFOLIO_EXPOSURE = float(os.getenv("RISK_MAX_PORTFOLIO_EXPOSURE", "1000.0"))

# Hard stop: no new trades when daily realised P&L falls below -LIMIT USDC.
# Resets at midnight UTC.
RISK_DAILY_LOSS_LIMIT = float(os.getenv("RISK_DAILY_LOSS_LIMIT", "100.0"))

# Hard stop: no new trades when session drawdown exceeds this USDC.
# Does NOT auto-reset — requires manual restart or config change.
RISK_MAX_DRAWDOWN = float(os.getenv("RISK_MAX_DRAWDOWN", "200.0"))

# After this many consecutive losing trades → enter cooldown.
RISK_MAX_CONSECUTIVE_LOSSES = int(os.getenv("RISK_MAX_CONSECUTIVE_LOSSES", "3"))

# Duration of consecutive-loss cooldown in seconds (default 5 minutes).
RISK_COOLDOWN_SECS = int(os.getenv("RISK_COOLDOWN_SECS", "300"))

# ---------------------------------------------------------------------------
# Execution layer
# ---------------------------------------------------------------------------

# Minimum net edge (after fee + slippage) required to place an order.
# 0.010 = 1%.  Set higher to be more selective.
EXEC_MIN_EDGE = float(os.getenv("EXEC_MIN_EDGE", "0.010"))

# Signal confidence >= this → prefer market (taker) order for faster fill.
EXEC_TAKER_CONFIDENCE_THRESHOLD = float(
    os.getenv("EXEC_TAKER_CONFIDENCE_THRESHOLD", "0.80")
)

# Urgency >= this → prefer market order regardless of confidence.
EXEC_TAKER_URGENCY_THRESHOLD = float(
    os.getenv("EXEC_TAKER_URGENCY_THRESHOLD", "0.90")
)

# Bid-ask spread <= this → prefer limit (maker) order even at high confidence.
EXEC_TAKER_SPREAD_THRESHOLD = float(
    os.getenv("EXEC_TAKER_SPREAD_THRESHOLD", "0.005")
)

# Cancel unmatched limit orders after this many seconds.
EXEC_ORDER_TIMEOUT_SECS = int(os.getenv("EXEC_ORDER_TIMEOUT_SECS", "120"))


# ===========================================================================
# Regime-adaptive parameter profiles
# ===========================================================================

# Which profile to use: "conservative" | "balanced" | "aggressive" | "auto"
# "auto" selects the profile based on the detected market regime.
PARAM_PROFILE = os.getenv("PARAM_PROFILE", "auto")


# ===========================================================================
# Kill-switch escalation tiers
# ===========================================================================
# All three signals are checked independently; the worst tier applies.
#
# Tier 1 — WARN      : log only, full size
# Tier 2 — REDUCE    : 50% size reduction
# Tier 3 — NO_TRADE  : block new entries (escalates health to SAFE_MODE)

# API error rate triggers (fraction of calls in last 60s)
KS_TIER1_API_ERROR_RATE = float(os.getenv("KS_TIER1_API_ERROR_RATE", "0.20"))
KS_TIER2_API_ERROR_RATE = float(os.getenv("KS_TIER2_API_ERROR_RATE", "0.40"))
KS_TIER3_API_ERROR_RATE = float(os.getenv("KS_TIER3_API_ERROR_RATE", "0.60"))

# Price-feed staleness triggers (seconds since last valid price)
KS_TIER1_STALE_SECS     = float(os.getenv("KS_TIER1_STALE_SECS",  "30.0"))
KS_TIER2_STALE_SECS     = float(os.getenv("KS_TIER2_STALE_SECS",  "90.0"))
KS_TIER3_STALE_SECS     = float(os.getenv("KS_TIER3_STALE_SECS", "300.0"))

# Drawdown pace triggers (fraction of daily limit lost per hour)
KS_TIER1_DD_PACE_PCT    = float(os.getenv("KS_TIER1_DD_PACE_PCT", "0.20"))
KS_TIER2_DD_PACE_PCT    = float(os.getenv("KS_TIER2_DD_PACE_PCT", "0.50"))
KS_TIER3_DD_PACE_PCT    = float(os.getenv("KS_TIER3_DD_PACE_PCT", "0.80"))


def validate():
    if not PRIVATE_KEY:
        raise ValueError("PRIVATE_KEY not set in .env")
    if not WALLET_ADDRESS:
        raise ValueError("WALLET_ADDRESS not set in .env")
