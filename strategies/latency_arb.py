"""
Latency Arbitrage Strategy
==========================
Polymarket's 15-minute BTC price markets resolve based on the BTC/USD price
at a fixed point in time (e.g. "Will BTC be above $95,000 at 3:15 PM?").

This strategy:
  1. Discovers active BTC threshold markets about to resolve.
  2. Compares the LIVE spot price (from BTCPriceFeed) to the threshold.
  3. If the outcome looks certain (>= CERTAINTY_THRESHOLD probability) but
     the market price hasn't fully updated, enters a position.

The edge: Polymarket market makers are slow. With 30–60 seconds left,
the fair value of YES or NO is ~0.99, but the book may still show 0.70.
This is the exact strategy used by high-profit bots like "gabagool".

Detection heuristics
--------------------
  - Question contains phrases like "above $X" or "below $X" at a time.
  - We parse the threshold and resolution time from the question text.
  - We only enter with <= WINDOW_SECS seconds remaining.
  - We only enter if spot price distance from threshold >= CERTAINTY_GAP_PCT.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config
from client import PolymarketClient, Market
from feeds.price_feed import BTCPriceFeed

log = logging.getLogger(__name__)

# How far BTC must be from the threshold (as % of threshold) to consider
# the outcome "certain". E.g. 0.005 = 0.5% away = $475 on a $95k BTC.
CERTAINTY_GAP_PCT = 0.005

# Only act when this many seconds or fewer remain.
WINDOW_SECS = config.LATENCY_ARB_WINDOW_SECS

# Minimum token price discount vs fair value (1.0) to consider profitable.
MIN_DISCOUNT = config.LATENCY_ARB_MIN_DISCOUNT

# Patterns to parse BTC threshold markets
# Matches: "Will BTC be above $95,000 at 3:15 PM?" or "...below $90000..."
_THRESHOLD_RE = re.compile(
    r"(?P<direction>above|below)\s+\$(?P<threshold>[\d,]+)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"at\s+(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ampm>AM|PM)?",
    re.IGNORECASE,
)


@dataclass
class BTCMarket:
    market: Market
    yes_token_id: str
    no_token_id: str
    direction: str          # "above" | "below"
    threshold: float        # USD price threshold
    resolution_dt: Optional[datetime]   # resolved at this UTC time


@dataclass
class LatencyArbTrade:
    market: BTCMarket
    side: str               # "YES" | "NO"
    token_id: str
    entry_price: float
    size_usdc: float
    fair_value: float       # ~1.0 since outcome is near-certain
    expected_profit: float
    order_id: Optional[str] = None
    success: bool = False


class LatencyArbStrategy:
    """
    Monitors BTC threshold markets for latency arbitrage opportunities.
    """

    def __init__(self, client: PolymarketClient, price_feed: BTCPriceFeed):
        self.client = client
        self.price_feed = price_feed
        self._btc_markets: list[BTCMarket] = []
        self._trades: list[LatencyArbTrade] = []
        self._last_market_refresh = 0.0
        self._market_refresh_interval = 120.0   # refresh market list every 2 min

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def _refresh_btc_markets(self, all_markets: list[Market]):
        """Filter and parse BTC threshold markets from the full market list."""
        parsed = []
        for m in all_markets:
            btc_market = self._parse_btc_market(m)
            if btc_market:
                parsed.append(btc_market)

        self._btc_markets = parsed
        log.info("LatencyArb: found %d BTC threshold markets", len(parsed))

    def _parse_btc_market(self, market: Market) -> Optional[BTCMarket]:
        """
        Try to parse a market's question into a BTCMarket.
        Returns None if the market isn't a BTC threshold market.
        """
        q = market.question.lower()
        # Must be about BTC/Bitcoin
        if "btc" not in q and "bitcoin" not in q:
            return None

        threshold_match = _THRESHOLD_RE.search(market.question)
        if not threshold_match:
            return None

        direction = threshold_match.group("direction").lower()
        threshold_str = threshold_match.group("threshold").replace(",", "")
        try:
            threshold = float(threshold_str)
        except ValueError:
            return None

        # Token IDs
        tokens = {t["outcome"].upper(): t["token_id"] for t in market.tokens}
        yes_id = tokens.get("YES")
        no_id = tokens.get("NO")
        if not yes_id or not no_id:
            return None

        # Resolution time — try end_date_iso first, then parse question
        resolution_dt = self._parse_resolution_time(market)

        return BTCMarket(
            market=market,
            yes_token_id=yes_id,
            no_token_id=no_id,
            direction=direction,
            threshold=threshold,
            resolution_dt=resolution_dt,
        )

    def _parse_resolution_time(self, market: Market) -> Optional[datetime]:
        """Parse resolution datetime from market metadata or question."""
        # Try ISO date from market data
        try:
            if market.end_date_iso:
                dt = datetime.fromisoformat(
                    market.end_date_iso.replace("Z", "+00:00")
                )
                return dt
        except (ValueError, AttributeError):
            pass

        # Fallback: parse time from question text (assumes today/UTC)
        time_match = _TIME_RE.search(market.question)
        if not time_match:
            return None

        now = datetime.now(timezone.utc)
        h = int(time_match.group("h"))
        m = int(time_match.group("m"))
        ampm = (time_match.group("ampm") or "").upper()

        if ampm == "PM" and h != 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0

        try:
            return now.replace(hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Opportunity detection
    # ------------------------------------------------------------------

    async def scan_once(
        self, all_markets: list[Market]
    ) -> list[LatencyArbTrade]:
        """
        Scan all BTC markets for latency arb opportunities.

        Returns a list of potential trades (not yet executed).
        """
        # Refresh market list periodically
        now = time.monotonic()
        if now - self._last_market_refresh > self._market_refresh_interval:
            await self._refresh_btc_markets(all_markets)
            self._last_market_refresh = now

        spot = self.price_feed.price
        if not spot or not self.price_feed.is_fresh:
            log.warning("LatencyArb: BTC price unavailable or stale")
            return []

        opportunities = []
        for btc_market in self._btc_markets:
            trade = await self._evaluate(btc_market, spot)
            if trade:
                opportunities.append(trade)

        return opportunities

    async def _evaluate(
        self, btc_market: BTCMarket, spot: float
    ) -> Optional[LatencyArbTrade]:
        """
        Evaluate a single BTC market.
        Returns a LatencyArbTrade if profitable, else None.
        """
        resolution_dt = btc_market.resolution_dt
        if resolution_dt is None:
            return None

        # Time remaining
        now_utc = datetime.now(timezone.utc)
        secs_remaining = (resolution_dt - now_utc).total_seconds()

        if secs_remaining < 0 or secs_remaining > WINDOW_SECS:
            return None

        threshold = btc_market.threshold
        direction = btc_market.direction
        gap_pct = abs(spot - threshold) / threshold

        if gap_pct < CERTAINTY_GAP_PCT:
            return None     # too close to threshold, outcome uncertain

        # Determine which side is winning
        if direction == "above":
            winning_side = "YES" if spot > threshold else "NO"
        else:
            winning_side = "YES" if spot < threshold else "NO"

        # Get the current price of the winning token
        token_id = (
            btc_market.yes_token_id
            if winning_side == "YES"
            else btc_market.no_token_id
        )
        _, ask = await self.client.get_best_prices(token_id)

        # Fair value is ~1.0 since this is near-certain
        fair_value = 1.0 - config.POLYMARKET_FEE

        discount = fair_value - ask
        if discount < MIN_DISCOUNT:
            return None

        size_usdc = min(config.MAX_POSITION_USDC, 200)
        expected_profit = discount * (size_usdc / ask)

        log.info(
            "LatencyArb opportunity | %s | BTC=%.2f threshold=%.2f (%s) | "
            "%.0fs left | %s ask=%.4f fair=%.4f discount=%.4f | "
            "expected profit: %.2f USDC",
            market_label(btc_market.market),
            spot, threshold, direction,
            secs_remaining,
            winning_side, ask, fair_value, discount,
            expected_profit,
        )

        return LatencyArbTrade(
            market=btc_market,
            side=winning_side,
            token_id=token_id,
            entry_price=ask,
            size_usdc=size_usdc,
            fair_value=fair_value,
            expected_profit=expected_profit,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, trade: LatencyArbTrade) -> LatencyArbTrade:
        """Execute a latency arb trade (market buy of the winning token)."""
        log.info(
            "LatencyArb EXECUTE | %s | BUY %s %.2f USDC @ %.4f",
            market_label(trade.market.market),
            trade.side, trade.size_usdc, trade.entry_price,
        )

        result = await self.client.place_market_order(
            trade.token_id, "BUY", trade.size_usdc
        )
        trade.order_id = result.order_id
        trade.success = result.success

        if result.success:
            log.info("LatencyArb: order placed %s", result.order_id)
        else:
            log.warning("LatencyArb: order failed — %s", result.error)

        self._trades.append(trade)
        return trade

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, markets_ref: list, poll_interval: float = 5.0):
        """
        Continuously scan for latency arb opportunities.

        `markets_ref` is a shared mutable list updated by the orchestrator.
        """
        log.info("LatencyArbStrategy started (poll=%.0fs)", poll_interval)
        while True:
            try:
                trades = await self.scan_once(markets_ref)
                for trade in trades:
                    await self.execute(trade)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("LatencyArb loop error: %s", exc, exc_info=True)
            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        total = len(self._trades)
        success = sum(1 for t in self._trades if t.success)
        expected = sum(t.expected_profit for t in self._trades if t.success)
        return {
            "total_attempts": total,
            "success": success,
            "expected_profit_usdc": round(expected, 2),
        }


def market_label(market: Market) -> str:
    q = market.question
    return q[:60] + "…" if len(q) > 60 else q
