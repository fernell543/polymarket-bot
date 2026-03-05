import os
import re
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

MAX_POSITION_USDC = float(os.getenv("MAX_POSITION_USDC", "500"))
ARB_MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.03"))

# ===========================================================================
# Risk-based position sizing  (risk/sizing.py — always active)
# ===========================================================================
# Fraction of account balance risked on a single directional trade.
# PriceArb / LatencyArb base = balance × RISK_BUDGET_PCT.
# MarketMaker uses its own capital-fraction base; guards still apply.
RISK_BUDGET_PCT     = float(os.getenv("RISK_BUDGET_PCT",     "0.02"))   # 2 % of balance

# Absolute floor / ceiling applied after all multipliers.
SIZE_MIN_USDC       = float(os.getenv("SIZE_MIN_USDC",       "5.0"))
SIZE_MAX_USDC       = float(os.getenv("SIZE_MAX_USDC",       "500.0"))

# Vol-regime multipliers applied to the base size.
# low  = calm/ranging market (arb or tight spread) → slight size increase
# mid  = neutral baseline
# high = trending/volatile market                  → size reduction
VOL_MULT_LOW        = float(os.getenv("VOL_MULT_LOW",        "1.2"))
VOL_MULT_MID        = float(os.getenv("VOL_MULT_MID",        "1.0"))
VOL_MULT_HIGH       = float(os.getenv("VOL_MULT_HIGH",       "0.6"))

# Guard: signal confidence below this → size=0 (trade skipped entirely).
CONFIDENCE_FLOOR    = float(os.getenv("CONFIDENCE_FLOOR",    "0.20"))

# Guard: net expected edge below this → size=0.
# 50 bps = 0.5 %.  Must exceed fees + slippage to be worth trading.
EDGE_FLOOR_BPS      = float(os.getenv("EDGE_FLOOR_BPS",      "50.0"))

# Normalisation point: edge at this BPS → edge_scalar=1.0.
# Edge above → scalar up to 2× cap; edge below (but above floor) → scalar <1.
EDGE_REFERENCE_BPS  = float(os.getenv("EDGE_REFERENCE_BPS",  "100.0"))
MARKET_MAKER_SPREAD = float(os.getenv("MARKET_MAKER_SPREAD", "0.02"))

# Dynamic position sizing: deploy this fraction of balance across all MM orders.
# order_size = balance * MM_CAPITAL_PCT / (MM_TARGET_MARKETS * 4 orders)
# e.g. $5000 * 0.20 / (5 * 4) = $50/order, $1000 total deployed (20% of balance)
MM_CAPITAL_PCT      = float(os.getenv("MM_CAPITAL_PCT", "0.20"))
MM_STARTING_BALANCE = float(os.getenv("MM_STARTING_BALANCE", "5000"))

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

POLYGON_CHAIN_ID = 137

# Polymarket signing mode:
# 0 = EOA wallet signs and trades directly
# 1 = proxy/funder style (common with Polymarket account setup)
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

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

# Optional safety pin: if set, WALLET_ADDRESS must match this exact polygon/funder address.
EXPECTED_POLYMARKET_WALLET = os.getenv("EXPECTED_POLYMARKET_WALLET", "").strip()


# ===========================================================================
# Wallet Clone Strategy
# ===========================================================================
# Approximates the trading behavior of a target Polymarket wallet using
# publicly extracted trade history.  Disabled by default (safe).
#
# To activate:
#   CLONE_ENABLED=1
#   CLONE_WALLET=0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd
#   (then run scripts/clone_extract.py and analytics/clone_profile.py)

# Master switch — default OFF.  Must be explicitly set to activate.
CLONE_ENABLED = os.getenv("CLONE_ENABLED", "0") == "1"

# Target wallet address to approximate.
CLONE_WALLET = os.getenv("CLONE_WALLET", "").lower()

# Path to the inferred profile JSON (auto-derived from CLONE_WALLET if not set).
CLONE_PROFILE_PATH = os.getenv("CLONE_PROFILE_PATH", "")

# Size multiplier: >1 = more aggressive, <1 = more conservative (default 1.0).
CLONE_AGGRESSIVENESS = float(os.getenv("CLONE_AGGRESSIVENESS", "1.0"))

# Minimum composite score (0–1) required to open a clone position.
# Higher = fewer but higher-conviction trades.  Default 0.40.
CLONE_SCORE_THRESHOLD = float(os.getenv("CLONE_SCORE_THRESHOLD", "0.40"))

# Maximum concurrent clone positions at any time.
CLONE_MAX_OPEN_POSITIONS = int(os.getenv("CLONE_MAX_OPEN_POSITIONS", "3"))

# Seconds between clone market scans.
CLONE_POLL_INTERVAL = int(os.getenv("CLONE_POLL_INTERVAL", "60"))

# Apply YES/NO directional bias from profile (default True).
CLONE_BIAS_ENABLED = os.getenv("CLONE_BIAS_ENABLED", "1") == "1"

# ---------------------------------------------------------------------------
# Clone HF Hedge Mode
# ---------------------------------------------------------------------------
# High-frequency paired YES/NO execution mode.  Places both legs simultaneously
# and hedges the unfilled leg when one fills.  State machine: collecting →
# partially_filled → hedged/aborted.  Paper-safe by default.
#
# Requires CLONE_ENABLED=1.  Safe defaults — enable with CLONE_HF_MODE_ENABLED=1.
#
# CLONE_HF_MODE_ENABLED=1           activate HF mode
# CLONE_COMBINED_PRICE_MIN=0.85     min (YES_ask + NO_ask) to enter
# CLONE_COMBINED_PRICE_MAX=0.97     max (YES_ask + NO_ask) to enter
# CLONE_HEDGE_TIMEOUT_SECS=30       abort if unfilled leg not done in N secs
# CLONE_HEDGE_TAKER_FALLBACK_SECS=10  switch to taker after N secs (< TIMEOUT)
# CLONE_MAX_SLIPPAGE_BPS=50         abort hedge if price moved > N bps from entry
# CLONE_CYCLE_INTERVAL_SECS=5       scan/monitor interval in HF mode
# CLONE_HF_MAX_POSITIONS=5          max concurrent HF pair positions
# CLONE_MIN_DEPTH_USDC=100          per-leg depth floor
# CLONE_HF_PAPER_SIMULATE_PARTIAL=0 set 1 to simulate partial fills in paper mode

CLONE_HF_MODE_ENABLED           = os.getenv("CLONE_HF_MODE_ENABLED",           "0") == "1"
CLONE_COMBINED_PRICE_MIN        = float(os.getenv("CLONE_COMBINED_PRICE_MIN",        "0.85"))
CLONE_COMBINED_PRICE_MAX        = float(os.getenv("CLONE_COMBINED_PRICE_MAX",        "0.97"))
CLONE_HEDGE_TIMEOUT_SECS        = int(os.getenv("CLONE_HEDGE_TIMEOUT_SECS",          "30"))
CLONE_HEDGE_TAKER_FALLBACK_SECS = int(os.getenv("CLONE_HEDGE_TAKER_FALLBACK_SECS",   "10"))
CLONE_MAX_SLIPPAGE_BPS          = float(os.getenv("CLONE_MAX_SLIPPAGE_BPS",          "50.0"))
CLONE_CYCLE_INTERVAL_SECS       = int(os.getenv("CLONE_CYCLE_INTERVAL_SECS",         "5"))
CLONE_HF_MAX_POSITIONS          = int(os.getenv("CLONE_HF_MAX_POSITIONS",            "5"))
CLONE_HF_MIN_DEPTH_USDC         = float(os.getenv("CLONE_MIN_DEPTH_USDC",            "100.0"))
CLONE_HF_PAPER_SIMULATE_PARTIAL = os.getenv("CLONE_HF_PAPER_SIMULATE_PARTIAL",  "0") == "1"


# ===========================================================================
# Live Deployment Mode
# ===========================================================================
# Controls live-specific execution frictions and throttles.
# Conservative defaults; override in .env to tune aggressiveness.
#
# Stages (used by scripts/live_staircase.py):
#   staircase_A — tiny-size validation ($5 max, 3 positions)
#   staircase_B — small-size scaling  ($20 max, 5 positions)
#   staircase_C — scale candidate     ($50 max, 10 positions)
#   production  — full deployment     (SIZE_MAX_USDC, unrestricted positions)

LIVE_DEPLOY_MODE = os.getenv("LIVE_DEPLOY_MODE", "staircase_A")

# Max orders per minute (token-bucket throttle, shared across all strategies).
# Hard cap prevents runaway loops from hammering the API.
LIVE_MAX_ORDER_RATE = int(os.getenv("LIVE_MAX_ORDER_RATE", "10"))

# Minimum market depth (sum of top-N bid+ask levels, USDC) to allow entry.
# Markets thinner than this have unpredictable fill rates and high slippage.
LIVE_MIN_DEPTH_USDC = float(os.getenv("LIVE_MIN_DEPTH_USDC", "100.0"))

# Spread stability window: require that spread has not changed >X% in last Y secs.
LIVE_SPREAD_STABILITY_SECS = int(os.getenv("LIVE_SPREAD_STABILITY_SECS", "5"))
LIVE_SPREAD_MAX_MOVE_PCT = float(os.getenv("LIVE_SPREAD_MAX_MOVE_PCT", "0.50"))  # 50% spread-widening allowed

# Avoid-chase: if price has moved more than this fraction of spread since signal,
# block the trade unless urgency exceeds LIVE_CHASE_URGENCY_OVERRIDE.
LIVE_CHASE_MAX_MOVE_SPREADS = float(os.getenv("LIVE_CHASE_MAX_MOVE_SPREADS", "1.5"))
LIVE_CHASE_URGENCY_OVERRIDE = float(os.getenv("LIVE_CHASE_URGENCY_OVERRIDE", "0.92"))

# Slippage model parameters (used by execution/live_realism.py).
# realized_slip = SLIP_BASE + spread * SLIP_SPREAD_FACTOR + (size/$100) * SLIP_IMPACT_FACTOR
LIVE_SLIP_BASE           = float(os.getenv("LIVE_SLIP_BASE",           "0.001"))  # 10 bps baseline
LIVE_SLIP_SPREAD_FACTOR  = float(os.getenv("LIVE_SLIP_SPREAD_FACTOR",  "0.30"))   # 30% of spread
LIVE_SLIP_IMPACT_FACTOR  = float(os.getenv("LIVE_SLIP_IMPACT_FACTOR",  "0.001"))  # per $100 notional

# Partial-fill model: fraction of size assumed to fill immediately.
# Conservative: assume only this fraction of depth available on top level.
LIVE_DEPTH_FILL_FRACTION = float(os.getenv("LIVE_DEPTH_FILL_FRACTION", "0.50"))   # 50% of top depth

# Cancel/replace latency budget (ms): if cancel+repost round-trip exceeds this,
# the stale-quote cost exceeds the spread benefit — skip re-quoting.
LIVE_CANCEL_REPLACE_BUDGET_MS = int(os.getenv("LIVE_CANCEL_REPLACE_BUDGET_MS", "500"))

# Book staleness: refuse to trade if best-price data is older than this (secs).
LIVE_BOOK_STALE_SECS = int(os.getenv("LIVE_BOOK_STALE_SECS", "10"))

# Staircase stage size/position limits (applied as hard caps, non-overridable by optimizer).
LIVE_STAGE_A_MAX_SIZE_USDC    = float(os.getenv("LIVE_STAGE_A_MAX_SIZE_USDC",   "5.0"))
LIVE_STAGE_A_MAX_POSITIONS    = int(os.getenv("LIVE_STAGE_A_MAX_POSITIONS",      "3"))
LIVE_STAGE_A_MIN_TRADES_GATE  = int(os.getenv("LIVE_STAGE_A_MIN_TRADES_GATE",    "5"))
LIVE_STAGE_A_MAX_LOSS_GATE    = float(os.getenv("LIVE_STAGE_A_MAX_LOSS_GATE",   "10.0"))
LIVE_STAGE_A_MIN_FILL_RATE_GATE = float(os.getenv("LIVE_STAGE_A_MIN_FILL_RATE_GATE", "0.65"))

LIVE_STAGE_B_MAX_SIZE_USDC    = float(os.getenv("LIVE_STAGE_B_MAX_SIZE_USDC",  "20.0"))
LIVE_STAGE_B_MAX_POSITIONS    = int(os.getenv("LIVE_STAGE_B_MAX_POSITIONS",     "5"))
LIVE_STAGE_B_MIN_TRADES_GATE  = int(os.getenv("LIVE_STAGE_B_MIN_TRADES_GATE",  "10"))
LIVE_STAGE_B_MAX_LOSS_GATE    = float(os.getenv("LIVE_STAGE_B_MAX_LOSS_GATE",  "40.0"))
LIVE_STAGE_B_MIN_FILL_RATE_GATE = float(os.getenv("LIVE_STAGE_B_MIN_FILL_RATE_GATE", "0.70"))
LIVE_STAGE_B_MIN_SHARPE_GATE  = float(os.getenv("LIVE_STAGE_B_MIN_SHARPE_GATE", "0.40"))

LIVE_STAGE_C_MAX_SIZE_USDC    = float(os.getenv("LIVE_STAGE_C_MAX_SIZE_USDC",  "50.0"))
LIVE_STAGE_C_MAX_POSITIONS    = int(os.getenv("LIVE_STAGE_C_MAX_POSITIONS",    "10"))
LIVE_STAGE_C_MIN_TRADES_GATE  = int(os.getenv("LIVE_STAGE_C_MIN_TRADES_GATE",  "20"))
LIVE_STAGE_C_MAX_LOSS_GATE    = float(os.getenv("LIVE_STAGE_C_MAX_LOSS_GATE", "100.0"))
LIVE_STAGE_C_MIN_FILL_RATE_GATE = float(os.getenv("LIVE_STAGE_C_MIN_FILL_RATE_GATE", "0.75"))
LIVE_STAGE_C_MIN_SHARPE_GATE  = float(os.getenv("LIVE_STAGE_C_MIN_SHARPE_GATE", "0.60"))


def validate():
    if not PRIVATE_KEY:
        raise ValueError("PRIVATE_KEY not set in .env")
    if not WALLET_ADDRESS:
        raise ValueError("WALLET_ADDRESS not set in .env")

    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", WALLET_ADDRESS):
        raise ValueError(
            "WALLET_ADDRESS must be a valid 42-char 0x... EVM address"
        )

    if EXPECTED_POLYMARKET_WALLET and (
        WALLET_ADDRESS.lower() != EXPECTED_POLYMARKET_WALLET.lower()
    ):
        raise ValueError(
            f"WALLET_ADDRESS mismatch: got {WALLET_ADDRESS}, expected {EXPECTED_POLYMARKET_WALLET}"
        )
