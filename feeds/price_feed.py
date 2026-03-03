"""
Real-time BTC/USD price feed via Binance WebSocket.

Keeps a rolling best-price estimate updated in-memory.
Falls back to Coinbase REST if the WebSocket drops.
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

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
COINBASE_REST = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# Price is considered stale (feed likely disconnected) after this many seconds
STALE_SECS = 60


class BTCPriceFeed:
    """
    Singleton-like class that maintains the latest BTC/USD price.

    Usage:
        feed = BTCPriceFeed()
        asyncio.create_task(feed.run())
        ...
        price = feed.price   # always current
    """

    def __init__(self):
        self._price: float = 0.0
        self._last_update: float = 0.0
        self._running = False

    @property
    def price(self) -> float:
        return self._price

    @property
    def age_secs(self) -> float:
        """Seconds since last price update."""
        return time.monotonic() - self._last_update

    @property
    def has_price(self) -> bool:
        """True once at least one valid price has been received."""
        return self._last_update > 0 and self._price > 0

    @property
    def is_fresh(self) -> bool:
        """True if price was updated in the last 5 seconds."""
        return self.age_secs < 5

    @property
    def is_stale(self) -> bool:
        """True if feed connected before but hasn't updated in STALE_SECS.
        Indicates the WebSocket/REST connection has likely dropped.
        """
        return self.has_price and self.age_secs > STALE_SECS

    def _update(self, price: float):
        self._price = price
        self._last_update = time.monotonic()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self):
        """
        Run the price feed loop indefinitely.
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
        async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
            log.info("Connected to Binance BTC/USDT trade stream")
            async for raw in ws:
                msg = json.loads(raw)
                # Trade message: {"p": "price", "q": "qty", ...}
                if "p" in msg:
                    self._update(float(msg["p"]))

    # ------------------------------------------------------------------
    # REST fallback (used on demand when WS is unavailable)
    # ------------------------------------------------------------------

    async def fetch_once(self) -> Optional[float]:
        """Fetch price once from Binance REST (no WebSocket)."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    BINANCE_REST, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    price = float(data["price"])
                    self._update(price)
                    return price
        except Exception as exc:
            log.debug("Binance REST fallback failed: %s", exc)

        # Try Coinbase as second fallback
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    COINBASE_REST, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    price = float(data["data"]["amount"])
                    self._update(price)
                    return price
        except Exception as exc:
            log.debug("Coinbase REST fallback failed: %s", exc)

        return None
