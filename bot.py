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
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Optional

import config
from client import PolymarketClient
from feeds.price_feed import BTCPriceFeed
from strategies import PriceArbStrategy, LatencyArbStrategy, MarketMakerStrategy

# ---------------------------------------------------------------------------
# Logging — console + rotating file
# ---------------------------------------------------------------------------

os.makedirs("logs", exist_ok=True)

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_console = logging.StreamHandler()
_console.setFormatter(_fmt)

_file_handler = logging.handlers.RotatingFileHandler(
    "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console, _file_handler])
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

    Safe-mode rules (no new positions opened when any condition is true):
      - Market list shorter than config.MIN_MARKETS_THRESHOLD
      - BTC price feed has never delivered a price (has_price=False)
      - BTC price feed is stale (is_stale=True, age > 60 s)
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

        # Startup health checks — must pass before we spawn any trading tasks
        healthy = await self._health_check()
        if not healthy:
            if config.DRY_RUN:
                log.warning("Health check issues detected — continuing in DRY_RUN mode")
            else:
                await self.client.stop()
                raise RuntimeError(
                    "Startup health check failed in LIVE mode — aborting. "
                    "Check logs above for actionable errors."
                )

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
    # Startup health checks
    # ------------------------------------------------------------------

    async def _health_check(self) -> bool:
        """Validate critical dependencies before trading loops start.

        Checks:
          1. Env vars present (live mode only).
          2. CLOB API reachable (test fetch of 1 market).
          3. Auth client initialised (live mode only).

        Returns True if all checks pass (or only warnings in dry-run).
        Returns False if any fatal check fails.
        """
        ok = True
        log.info("=" * 60)
        log.info("  STARTUP HEALTH CHECKS")
        log.info("=" * 60)

        # 1. Environment variables (only fatal in live mode)
        if not config.DRY_RUN:
            if not config.PRIVATE_KEY:
                log.error("  [FAIL] PRIVATE_KEY not set — add it to .env")
                ok = False
            else:
                log.info("  [OK]   PRIVATE_KEY present")

            if not config.WALLET_ADDRESS:
                log.error("  [FAIL] WALLET_ADDRESS not set — add it to .env")
                ok = False
            else:
                log.info("  [OK]   WALLET_ADDRESS present")
        else:
            log.info("  [SKIP] Env-var check (DRY_RUN mode)")

        # 2. API connectivity
        try:
            test = await self.client.get_markets(active_only=True, limit=1)
            if test:
                log.info("  [OK]   CLOB API reachable (%d market returned)", len(test))
            else:
                log.warning("  [WARN] CLOB API reachable but returned 0 markets")
        except Exception as exc:
            log.error("  [FAIL] Cannot reach CLOB API: %s", exc)
            log.error("         Check CLOB_HOST in .env and network connectivity")
            ok = False

        # 3. Auth client (live mode only)
        if not config.DRY_RUN:
            if self.client._clob_client is None:
                log.error(
                    "  [FAIL] Auth client not initialised — "
                    "install py-clob-client and verify PRIVATE_KEY"
                )
                ok = False
            else:
                log.info("  [OK]   Auth client initialised")
        else:
            log.info("  [SKIP] Auth client check (DRY_RUN mode)")

        if ok:
            log.info("  All health checks passed — bot starting")
        else:
            log.error("  One or more health checks FAILED — see above")
        log.info("=" * 60)
        return ok

    # ------------------------------------------------------------------
    # Safe-mode guard
    # ------------------------------------------------------------------

    def _safe_to_trade(self) -> bool:
        """Return True only when data confidence is sufficient for new entries.

        False when:
          - Market list is smaller than MIN_MARKETS_THRESHOLD (API degraded).
          - (Price-feed staleness is checked separately per-strategy loop.)
        """
        return len(self._markets_cache) >= config.MIN_MARKETS_THRESHOLD

    # ------------------------------------------------------------------
    # Shared market cache
    # ------------------------------------------------------------------

    async def _refresh_markets(self):
        """Fetch fresh market list, preserving the existing cache on failure
        or when the new result looks suspiciously small."""
        try:
            markets = await self.client.get_markets(active_only=True)
        except Exception as exc:
            log.error("Failed to refresh markets — keeping existing cache (%d): %s",
                      len(self._markets_cache), exc)
            return

        if not markets:
            log.warning(
                "Market refresh returned 0 markets — keeping existing cache (%d)",
                len(self._markets_cache),
            )
            return

        if len(markets) < config.MIN_MARKETS_THRESHOLD and self._markets_cache:
            log.warning(
                "Market refresh returned only %d markets (threshold=%d) — "
                "keeping existing cache (%d) to avoid safe-mode false positive",
                len(markets), config.MIN_MARKETS_THRESHOLD, len(self._markets_cache),
            )
            return

        self._markets_cache.clear()
        self._markets_cache.extend(markets)
        log.info("Markets refreshed: %d active markets", len(markets))

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
                if not self._safe_to_trade():
                    log.warning(
                        "PriceArb: SAFE MODE — market list too small (%d < %d), "
                        "skipping cycle",
                        len(self._markets_cache), config.MIN_MARKETS_THRESHOLD,
                    )
                    await asyncio.sleep(10)
                    continue
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
        _feed_warned = False
        while self._running:
            try:
                # Safe-mode: market list too small
                if not self._safe_to_trade():
                    log.warning(
                        "LatencyArb: SAFE MODE — market list too small (%d < %d), "
                        "skipping cycle",
                        len(self._markets_cache), config.MIN_MARKETS_THRESHOLD,
                    )
                    await asyncio.sleep(5)
                    continue

                # Safe-mode: price feed never started
                if not self.price_feed.has_price:
                    if not _feed_warned:
                        log.warning(
                            "LatencyArb: SAFE MODE — BTC price feed has no data yet; "
                            "waiting for Binance/Coinbase connection"
                        )
                        _feed_warned = True
                    await asyncio.sleep(5)
                    continue

                # Safe-mode: price feed stale (likely disconnected)
                if self.price_feed.is_stale:
                    if not _feed_warned:
                        log.warning(
                            "LatencyArb: SAFE MODE — BTC price stale (%.0fs > 60s); "
                            "no new entries until feed recovers",
                            self.price_feed.age_secs,
                        )
                        _feed_warned = True
                    await asyncio.sleep(5)
                    continue

                _feed_warned = False
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
                # Safe-mode: market list too small
                if not self._safe_to_trade():
                    log.warning(
                        "MarketMaker: SAFE MODE — market list too small (%d < %d), "
                        "skipping rebalance",
                        len(self._markets_cache), config.MIN_MARKETS_THRESHOLD,
                    )
                    await asyncio.sleep(config.MM_REBALANCE_INTERVAL)
                    continue

                # Refresh balance so order sizing stays current
                if config.DRY_RUN:
                    self.market_maker.set_balance(config.MM_STARTING_BALANCE)
                else:
                    bal = await self.client.get_balance_usdc()
                    self.market_maker.set_balance(bal)

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
            with open("logs/perf.json", "w") as fh:
                json.dump(self.market_maker.stats(), fh)

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
