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
MM_ORDER_SIZE_USDC = float(os.getenv("MM_ORDER_SIZE_USDC", "100"))

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


def validate():
    if not PRIVATE_KEY:
        raise ValueError("PRIVATE_KEY not set in .env")
    if not WALLET_ADDRESS:
        raise ValueError("WALLET_ADDRESS not set in .env")
