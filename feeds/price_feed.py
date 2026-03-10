"""
Real-time crypto price feed via Binance WebSocket.

Maintains rolling best-price estimates for BTC, ETH, SOL, and XRP.
Falls back to Binance REST then Coinbase REST per symbol if the WebSocket drops.
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp
import websockets

import config

log = logging.getLogger(__name__)

# All supported symbols (must match _CRYPTO_KEYWORDS in latency_arb.py)
SYMBOLS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

# Binance combined stream — 4 assets, one WebSocket connection
BINANCE_MULTI_WS = config.BINANCE_WS_URL
BINANCE_REST_BASE = "https://api.binance.com/api/v3/ticker/price?symbol={}"
COINBASE_REST_BASE = "https://api.coinbase.com/v2/prices/{}-USD/spot"

_COINBASE_TICKER = {
    "btcusdt": "BTC",
    "ethusdt": "ETH",
    "solusdt": "SOL",
    "xrpusdt": "XRP",
}

# Price is considered stale (feed likely disconnected) after this many seconds
STALE_SECS = 60


class CryptoPriceFeed:
    """
    Maintains the latest spot prices for BTC, ETH, SOL, and XRP.

    Usage:
        feed = CryptoPriceFeed()
        asyncio.create_task(feed.run())
        ...
        btc_price = feed.get_price("btcusdt")
        if feed.is_fresh_for("ethusdt"):
            eth_price = feed.get_price("ethusdt")
    """

    def __init__(self):
        self._prices: dict[str, float] = {}
        self._last_updates: dict[str, float] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Per-symbol API (new)
    # ------------------------------------------------------------------

    def get_price(self, symbol: str) -> float:
        """Return the latest price for a symbol (0.0 if unavailable)."""
        return self._prices.get(symbol, 0.0)

    def age_secs_for(self, symbol: str) -> float:
        """Seconds since last update for a given symbol."""
        t = self._last_updates.get(symbol, 0.0)
        if t == 0.0:
            return float("inf")
        return time.monotonic() - t

    def is_fresh_for(self, symbol: str) -> bool:
        """True if price for symbol was updated in the last 5 seconds."""
        return self.age_secs_for(symbol) < 5

    def has_price_for(self, symbol: str) -> bool:
        """True if at least one valid price has been received for symbol."""
        return self._prices.get(symbol, 0.0) > 0

    # ------------------------------------------------------------------
    # Backward-compatible BTC properties (used by bot.py health checks)
    # ------------------------------------------------------------------

    @property
    def price(self) -> float:
        """BTC/USD price (backward compat)."""
        return self.get_price("btcusdt")

    @property
    def age_secs(self) -> float:
        """Seconds since last BTC update."""
        return self.age_secs_for("btcusdt")

    @property
    def has_price(self) -> bool:
        """True once any valid price has been received."""
        return any(v > 0 for v in self._prices.values())

    @property
    def is_fresh(self) -> bool:
        """True if BTC price was updated in the last 5 seconds."""
        return self.is_fresh_for("btcusdt")

    @property
    def is_stale(self) -> bool:
        """True if BTC feed connected before but hasn't updated in STALE_SECS."""
        return self.has_price_for("btcusdt") and self.age_secs > STALE_SECS

    def _update(self, symbol: str, price: float):
        self._prices[symbol] = price
        self._last_updates[symbol] = time.monotonic()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self):
        """Run the price feed loop indefinitely.
        Tries Binance WebSocket; falls back to REST polling on errors.
        """
        self._running = True
        while self._running:
            try:
                await self._binance_ws_loop()
            except Exception as exc:
                log.warning("Binance WS error: %s — retrying in 3s", exc)
                await asyncio.sleep(3)

    async def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Binance WebSocket (primary)
    # ------------------------------------------------------------------

    async def _binance_ws_loop(self):
        async with websockets.connect(BINANCE_MULTI_WS, ping_interval=20) as ws:
            log.info("Connected to Binance multi-asset trade stream (%s)", BINANCE_MULTI_WS)
            async for raw in ws:
                msg = json.loads(raw)
                # Combined stream wraps data: {"stream": "...", "data": {...}}
                # Single stream sends data directly without wrapping.
                data = msg.get("data", msg)
                if "p" in data and "s" in data:
                    symbol = data["s"].lower()
                    if symbol in _COINBASE_TICKER:
                        self._update(symbol, float(data["p"]))

    # ------------------------------------------------------------------
    # REST fallback (used on demand when WS is unavailable)
    # ------------------------------------------------------------------

    async def fetch_once(self) -> Optional[float]:
        """Fetch prices once from REST for all symbols. Returns BTC price."""
        async with aiohttp.ClientSession() as session:
            for symbol in SYMBOLS:
                await self._fetch_symbol_rest(session, symbol)
        return self.get_price("btcusdt") or None

    async def _fetch_symbol_rest(
        self, session: aiohttp.ClientSession, symbol: str
    ) -> None:
        """Fetch a single symbol from Binance REST, falling back to Coinbase."""
        try:
            url = BINANCE_REST_BASE.format(symbol.upper())
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                self._update(symbol, float(data["price"]))
                return
        except Exception as exc:
            log.debug("Binance REST for %s failed: %s", symbol, exc)

        coinbase_sym = _COINBASE_TICKER.get(symbol)
        if coinbase_sym:
            try:
                url = COINBASE_REST_BASE.format(coinbase_sym)
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    self._update(symbol, float(data["data"]["amount"]))
            except Exception as exc:
                log.debug("Coinbase REST for %s failed: %s", symbol, exc)


# Backward-compat alias so `from feeds.price_feed import BTCPriceFeed` still works
BTCPriceFeed = CryptoPriceFeed
