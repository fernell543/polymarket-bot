"""
Polymarket Bot — Main Orchestrator
====================================
Runs three strategies concurrently:
  1. PriceArbStrategy   — arbitrage YES+NO < $1.00
  2. LatencyArbStrategy — bet on near-certain BTC market outcomes
  3. MarketMakerStrategy — provide liquidity and earn rewards

Usage:
  cp .env.example .env        # fill in PRIVATE_KEY, WALLET_ADDRESS
  pip install -r requirements.txt
  python bot.py               # starts in DRY_RUN=1 mode by default
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Optional

import config
from client import PolymarketClient
from feeds.price_feed import BTCPriceFeed
from strategies import PriceArbStrategy, LatencyArbStrategy, MarketMakerStrategy

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Bot:
    """
    Coordinates the three strategies via asyncio tasks.

    Shared state:
      - `markets_cache`: refreshed every MARKETS_REFRESH_SECS and passed
        by reference to strategies that need the full market list.
      - `price_feed`: single BTCPriceFeed instance shared between strategies.
    """

    MARKETS_REFRESH_SECS = 120
    STATS_INTERVAL_SECS  = 60

    def __init__(self):
        self.client       = PolymarketClient()
        self.price_feed   = BTCPriceFeed()

        self.price_arb    = PriceArbStrategy(self.client)
        self.latency_arb  = LatencyArbStrategy(self.client, self.price_feed)
        self.market_maker = MarketMakerStrategy(self.client)

        self._markets_cache: list = []
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._start_time = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        if config.DRY_RUN:
            log.warning("=" * 60)
            log.warning("  DRY RUN MODE — no real orders will be placed")
            log.warning("  Set DRY_RUN=0 in .env to enable live trading")
            log.warning("=" * 60)
        else:
            config.validate()
            log.info("LIVE TRADING MODE — wallet %s", config.WALLET_ADDRESS)

        await self.client.start()
        self._running = True
        self._start_time = time.monotonic()

        # Initial market list fetch
        await self._refresh_markets()

        # Spawn all async tasks
        self._tasks = [
            asyncio.create_task(self._market_refresh_loop(), name="market-refresh"),
            asyncio.create_task(self.price_feed.run(),       name="price-feed"),
            asyncio.create_task(self._price_arb_loop(),      name="price-arb"),
            asyncio.create_task(self._latency_arb_loop(),    name="latency-arb"),
            asyncio.create_task(self._market_maker_loop(),   name="market-maker"),
            asyncio.create_task(self._stats_loop(),          name="stats"),
        ]

        log.info("Bot started with %d tasks", len(self._tasks))

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.stop()
        log.info("Bot stopped. Final stats:\n%s", self._format_stats())

    # ------------------------------------------------------------------
    # Shared market cache
    # ------------------------------------------------------------------

    async def _refresh_markets(self):
        try:
            markets = await self.client.get_markets(active_only=True)
            self._markets_cache.clear()
            self._markets_cache.extend(markets)
            log.info("Markets refreshed: %d active markets", len(markets))
        except Exception as exc:
            log.error("Failed to refresh markets: %s", exc)

    async def _market_refresh_loop(self):
        while self._running:
            await asyncio.sleep(self.MARKETS_REFRESH_SECS)
            await self._refresh_markets()

    # ------------------------------------------------------------------
    # Strategy loops (thin wrappers so we can share markets_cache)
    # ------------------------------------------------------------------

    async def _price_arb_loop(self):
        """
        Price arb runs its own internal poll, but we drive it here
        so it's cancellable from the orchestrator.
        """
        log.info("PriceArb loop starting")
        while self._running:
            try:
                opportunities = await self.price_arb.scan_once()
                for opp in opportunities:
                    await self.price_arb.execute(opp)
                    break   # execute at most one per cycle (capital management)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("PriceArb error: %s", exc, exc_info=True)
            await asyncio.sleep(10)

    async def _latency_arb_loop(self):
        log.info("LatencyArb loop starting")
        while self._running:
            try:
                trades = await self.latency_arb.scan_once(self._markets_cache)
                for trade in trades:
                    await self.latency_arb.execute(trade)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("LatencyArb error: %s", exc, exc_info=True)
            await asyncio.sleep(5)

    async def _market_maker_loop(self):
        log.info("MarketMaker loop starting")
        while self._running:
            try:
                targets = await self.market_maker.select_target_markets(
                    self._markets_cache
                )
                tasks = [
                    self.market_maker._quote_market(market, yes_id, no_id)
                    for market, yes_id, no_id in targets
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                log.info("MarketMaker: cancelling orders on shutdown")
                await self.client.cancel_all_orders()
                break
            except Exception as exc:
                log.error("MarketMaker error: %s", exc, exc_info=True)
            await asyncio.sleep(config.MM_REBALANCE_INTERVAL)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def _stats_loop(self):
        while self._running:
            await asyncio.sleep(self.STATS_INTERVAL_SECS)
            log.info("STATS\n%s", self._format_stats())

    def _format_stats(self) -> str:
        uptime = int(time.monotonic() - self._start_time)
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        lines = [
            f"  Uptime:        {h:02d}:{m:02d}:{s:02d}",
            f"  Markets:       {len(self._markets_cache)} active",
            f"  BTC price:     ${self.price_feed.price:,.2f} "
            f"(age {self.price_feed.age_secs:.1f}s)",
            f"  PriceArb:      {self.price_arb.stats()}",
            f"  LatencyArb:    {self.latency_arb.stats()}",
            f"  MarketMaker:   {self.market_maker.stats()}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    bot = Bot()

    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        log.info("Received %s — shutting down…", sig.name)
        loop.create_task(bot.stop())

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown, sig)

    await bot.start()

    # Wait until all tasks are done (i.e. shutdown was called)
    try:
        await asyncio.gather(*bot._tasks, return_exceptions=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
