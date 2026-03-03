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
from analytics import QuantTradeLogger
from client import PolymarketClient
from execution import ExecutionManager
from feeds.price_feed import BTCPriceFeed
from health_state import HealthState, HealthLevel
from risk import RiskEngine, CircuitBreakerState, PositionSizer
from risk.kill_switch import KillSwitch
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

        # --- Quant infrastructure (safe when QUANT_MODE_ENABLED=0) --------
        self.health        = HealthState()
        self.risk_engine   = RiskEngine()
        self.exec_manager  = ExecutionManager(self.client)
        self.trade_logger  = QuantTradeLogger()
        self.kill_switch   = KillSwitch()
        self.sizer         = PositionSizer()
        self._session_start: float = time.time()

        # Wire risk-based sizer + health state into all strategies
        for _strat in (self.price_arb, self.latency_arb, self.market_maker):
            _strat.set_sizer(self.sizer)
            _strat.set_health(self.health)

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
            asyncio.create_task(self._kill_switch_loop(),    name="kill-switch"),
            asyncio.create_task(
                self.exec_manager.run_timeout_loop(interval_secs=30),
                name="order-timeout",
            ),
        ]

        if config.QUANT_MODE_ENABLED:
            log.info("QUANT MODE ENABLED — signal engine, risk engine, adaptive execution active")
        else:
            log.info("Quant mode disabled (QUANT_MODE_ENABLED=0) — running classic strategies")

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

        # 2. API connectivity — single-page probe only (avoids 500k-market pagination)
        try:
            probe = await self.client._get_json(
                "/markets", {"active": "true", "closed": "false", "limit": "10"}
            )
            n = len(probe.get("data", []))
            if n > 0:
                log.info("  [OK]   CLOB API reachable (%d markets on probe page)", n)
            else:
                log.warning("  [WARN] CLOB API reachable but probe returned 0 markets")
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
    # Health state management
    # ------------------------------------------------------------------

    def _update_health_state(self) -> None:
        """
        Recompute and apply the global HealthState from current data quality
        signals and the risk engine's circuit-breaker.

        Called after every market refresh and inside strategy loops.
        """
        # Risk engine circuit breaker → highest severity
        cb = self.risk_engine.state.circuit_breaker
        if cb == CircuitBreakerState.OPEN:
            self.health.set(
                HealthLevel.CIRCUIT_BREAKER,
                f"risk circuit breaker OPEN — "
                f"daily_pnl={self.risk_engine.state.daily_pnl:+.2f} "
                f"drawdown={self.risk_engine.state.drawdown:.2f}",
            )
            return

        # Market list too small → SAFE_MODE
        if len(self._markets_cache) < config.MIN_MARKETS_THRESHOLD:
            self.health.set(
                HealthLevel.SAFE_MODE,
                f"market list too small: {len(self._markets_cache)} < "
                f"{config.MIN_MARKETS_THRESHOLD}",
            )
            return

        # BTC price feed completely absent → SAFE_MODE
        if not self.price_feed.has_price:
            self.health.set(
                HealthLevel.SAFE_MODE,
                "BTC price feed has no data yet",
            )
            return

        # BTC price feed stale → SAFE_MODE (latency_arb) / DEGRADED (others)
        if self.price_feed.is_stale:
            self.health.set(
                HealthLevel.SAFE_MODE,
                f"BTC price stale ({self.price_feed.age_secs:.0f}s > 60s)",
            )
            return

        # Cooldown: temporary pause, not a hard stop
        if cb == CircuitBreakerState.COOLING:
            self.health.set(
                HealthLevel.SAFE_MODE,
                f"risk cooldown active ({self.risk_engine.state.consecutive_losses} "
                f"consecutive losses)",
            )
            return

        # All clear
        self.health.set(HealthLevel.NORMAL, "all systems operational")

    # ------------------------------------------------------------------
    # Safe-mode guard
    # ------------------------------------------------------------------

    def _safe_to_trade(self) -> bool:
        """Return True only when data quality and risk state allow new entries.

        Checks:
          - Market list >= MIN_MARKETS_THRESHOLD.
          - HealthState == NORMAL (price feed fresh, no circuit breaker).
        """
        self._update_health_state()
        return self.health.ok_to_trade()

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

    async def _kill_switch_loop(self):
        """
        Update kill-switch signals every 10s and apply to health + market maker.

        Feeds:
          - Price-feed staleness (age_secs)
          - Drawdown pace (daily loss vs limit over session elapsed hours)
          - API error rate is fed by client calls (not tracked here centrally yet,
            but the kill switch handles missing data gracefully with 0% rate)
        """
        while self._running:
            try:
                # Stale data
                self.kill_switch.update_stale_secs(
                    self.price_feed.age_secs if self.price_feed.has_price else 0.0
                )

                # Drawdown pace
                elapsed_hours = (time.time() - self._session_start) / 3600.0
                risk_st = self.risk_engine.state
                current_loss = max(0.0, -risk_st.daily_pnl)   # loss is positive
                self.kill_switch.update_drawdown_pace(
                    current_loss,
                    config.RISK_DAILY_LOSS_LIMIT,
                    elapsed_hours,
                )

                # Apply tier to health and market-maker size
                self.kill_switch.apply_to_health(self.health)
                self.market_maker.set_size_multiplier(
                    self.kill_switch.size_multiplier
                )
                self.health.size_multiplier = self.kill_switch.size_multiplier

                ks_tier = self.kill_switch.tier
                if ks_tier.value >= 2:
                    log.warning("KillSwitch: %s", self.kill_switch.summary)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("KillSwitch loop error: %s", exc)
            await asyncio.sleep(10)

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
                    log.warning("PriceArb: %s", self.health.summary)
                    await asyncio.sleep(10)
                    continue
                if not self.risk_engine.can_trade():
                    log.warning("PriceArb: risk engine blocked — %s",
                                self.risk_engine.state.summary)
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
        while self._running:
            try:
                # Unified safe-mode check (covers market list + price feed + CB)
                if not self._safe_to_trade():
                    log.warning("LatencyArb: %s", self.health.summary)
                    await asyncio.sleep(5)
                    continue

                if not self.risk_engine.can_trade():
                    log.warning("LatencyArb: risk engine blocked — %s",
                                self.risk_engine.state.summary)
                    await asyncio.sleep(5)
                    continue

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
                # Unified safe-mode check
                if not self._safe_to_trade():
                    log.warning("MarketMaker: %s", self.health.summary)
                    await asyncio.sleep(config.MM_REBALANCE_INTERVAL)
                    continue

                # Risk engine check (MM is maintenance-allowed even in cooldown)
                if not self.health.maintenance_allowed():
                    log.warning("MarketMaker: circuit breaker OPEN — skipping rebalance")
                    await asyncio.sleep(config.MM_REBALANCE_INTERVAL)
                    continue

                # Refresh balance so order sizing stays current
                if config.DRY_RUN:
                    bal = config.MM_STARTING_BALANCE
                else:
                    bal = await self.client.get_balance_usdc()
                self.market_maker.set_balance(bal)
                self.sizer.set_balance(bal)   # keep sizer in sync with live balance

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
            risk_st = self.risk_engine.state
            ks_st = self.kill_switch.state
            fq_st = self.exec_manager.fill_quality_stats()
            perf = {
                **self.market_maker.stats(),
                "health": self.health.level.name,
                "health_reason": self.health.reason,
                "health_size_multiplier": self.health.size_multiplier,
                "risk_circuit_breaker": risk_st.circuit_breaker.name,
                "risk_daily_pnl": risk_st.daily_pnl,
                "risk_drawdown": risk_st.drawdown,
                "risk_portfolio_exposure": risk_st.portfolio_exposure,
                "kill_switch_tier": ks_st.tier.label,
                "kill_switch_trigger": ks_st.trigger,
                "fill_quality": fq_st,
                "quant_mode": config.QUANT_MODE_ENABLED,
            }
            with open("logs/perf.json", "w") as fh:
                json.dump(perf, fh, indent=2)

    def _format_stats(self) -> str:
        uptime = int(time.monotonic() - self._start_time)
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        risk_st = self.risk_engine.state
        ks_st = self.kill_switch.state
        fq_st = self.exec_manager.fill_quality_stats()
        lines = [
            f"  Uptime:        {h:02d}:{m:02d}:{s:02d}",
            f"  Health:        {self.health.summary}",
            f"  KillSwitch:    {self.kill_switch.summary}",
            f"  Markets:       {len(self._markets_cache)} active",
            f"  BTC price:     ${self.price_feed.price:,.2f} "
            f"(age {self.price_feed.age_secs:.1f}s)",
            f"  Risk:          {risk_st.summary}",
            f"  FillQuality:   placed={fq_st['total_placed']} "
            f"cancelled={fq_st['total_cancelled']} "
            f"cancel_rate={fq_st['cancel_rate']:.1%} "
            f"avg_lat={fq_st['avg_latency_ms']:.0f}ms",
            f"  PriceArb:      {self.price_arb.stats()}",
            f"  LatencyArb:    {self.latency_arb.stats()}",
            f"  MarketMaker:   {self.market_maker.stats()}",
        ]
        if config.QUANT_MODE_ENABLED:
            lines.append(f"  Quant mode:    ACTIVE  profile={config.PARAM_PROFILE}")
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
