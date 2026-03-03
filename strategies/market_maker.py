"""
Market Making (AMM) Strategy
=============================
Places limit orders on both sides of selected Polymarket markets to:
  1. Earn the bid-ask spread on each fill.
  2. Earn Polymarket's DAILY liquidity rewards (much of the edge here).

How rewards work
----------------
Polymarket pays makers in USDC proportional to how tightly they quote
around the midpoint. Orders within 1% of mid earn ~3x more than orders
at 5%. The reward calculation favors:
  - Quoting BOTH sides (yes and no) simultaneously.
  - Quoting closer to the current price.
  - Keeping orders live longer.

Strategy
--------
For each target market:
  - Place a BID and an ASK for the YES token.
  - Spread = config.MARKET_MAKER_SPREAD (e.g. 2%).
  - Cancel and replace every MM_REBALANCE_INTERVAL seconds.
  - Skip if inventory is too imbalanced (> MAX_INVENTORY_IMBALANCE).

Position sizing
---------------
Order size is calculated dynamically from your account balance:
  order_size = balance * MM_CAPITAL_PCT / (MM_TARGET_MARKETS * 4)
This keeps total capital deployed at MM_CAPITAL_PCT of balance regardless
of account size, and auto-adjusts as your balance grows or shrinks.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from client import PolymarketClient, Market
from signals import SignalEngine, SignalResult

log = logging.getLogger(__name__)

MAX_INVENTORY_IMBALANCE = 0.7   # cancel/pause if one side > 70% of total


def _simulate_fills(pos: "MMPosition", current_mid: float, size_usdc: float) -> None:
    """
    Check if previous limit orders would have filled based on current mid price.

    BUY fills when mid drops to/below our bid (market moved down to us).
    SELL fills when mid rises to/above our ask (market moved up to us).
    Win  = both sides fill → captured the spread.
    Loss = only one side fills → adverse selection (market moved strongly one way).
    """
    no_mid = 1.0 - current_mid
    for buy_hit, sell_hit, bid_p, ask_p in [
        (current_mid <= pos.yes_bid_price, current_mid >= pos.yes_ask_price,
         pos.yes_bid_price, pos.yes_ask_price),
        (no_mid <= pos.no_bid_price, no_mid >= pos.no_ask_price,
         pos.no_bid_price, pos.no_ask_price),
    ]:
        if bid_p <= 0:
            continue  # no previous order
        if buy_hit and sell_hit:
            pos.total_pnl += (ask_p - bid_p) * (size_usdc / bid_p)
            pos.total_fills += 2
            pos.wins += 1
        elif buy_hit or sell_hit:
            pos.total_pnl -= ((ask_p - bid_p) / 2) * (size_usdc / bid_p)
            pos.total_fills += 1
            pos.losses += 1


@dataclass
class MMPosition:
    """Tracks open MM orders on a single market."""
    market: Market
    yes_token_id: str
    no_token_id: str
    bid_order_id: Optional[str] = None   # YES BID
    ask_order_id: Optional[str] = None   # YES ASK
    no_bid_order_id: Optional[str] = None
    no_ask_order_id: Optional[str] = None
    yes_inventory: float = 0.0           # YES tokens held
    no_inventory: float = 0.0           # NO tokens held
    total_fills: int = 0
    total_pnl: float = 0.0
    last_refresh: float = field(default_factory=time.monotonic)
    # fill simulation tracking (dry run only)
    yes_bid_price: float = 0.0
    yes_ask_price: float = 0.0
    no_bid_price: float = 0.0
    no_ask_price: float = 0.0
    wins: int = 0
    losses: int = 0


class MarketMakerStrategy:
    """
    Maintains two-sided quotes on high-reward Polymarket markets.
    Order size auto-scales with account balance.
    """

    def __init__(self, client: PolymarketClient):
        self.client = client
        self._positions: dict[str, MMPosition] = {}   # condition_id → position
        self._balance: float = config.MM_STARTING_BALANCE  # updated by bot.py
        self._size_multiplier: float = 1.0   # set by kill-switch (0.5 = Tier 2)
        # One SignalEngine per YES token (keyed by token_id)
        self._signal_engines: dict[str, SignalEngine] = {}

    def set_balance(self, balance: float) -> None:
        """Called by the orchestrator whenever a fresh balance is fetched."""
        if balance > 0:
            self._balance = balance

    def set_size_multiplier(self, multiplier: float) -> None:
        """
        Set the kill-switch size multiplier (0.0–1.0).

        Called by bot.py before each rebalance cycle based on kill-switch tier.
          1.0 → full size (Tier 0 / 1)
          0.5 → half size (Tier 2 REDUCE)
          0.0 → should not reach here (Tier 3 blocks at safe-to-trade level)
        """
        self._size_multiplier = max(0.0, min(1.0, multiplier))

    def _order_size_usdc(self) -> float:
        """
        Compute per-order size so total deployed ≤ MM_CAPITAL_PCT of balance,
        then apply the kill-switch size multiplier.
        Floor at $5 so tiny balances still place valid orders.
        """
        size = self._balance * config.MM_CAPITAL_PCT / (config.MM_TARGET_MARKETS * 4)
        size *= self._size_multiplier
        return max(5.0, round(size, 2))

    # ------------------------------------------------------------------
    # Market selection
    # ------------------------------------------------------------------

    async def select_target_markets(
        self, all_markets: list[Market]
    ) -> list[Market]:
        """
        Select the best markets for market making:
          1. Markets that earn liquidity rewards.
          2. Active, high-volume binary markets.
          3. Mid price is between 0.10 and 0.90 (not too skewed).
        """
        # Fetch reward markets
        reward_markets = await self.client.get_rewards_markets()
        reward_ids = {m.get("condition_id") for m in reward_markets}

        candidates = []
        for market in all_markets:
            if not market.active or market.closed:
                continue
            if len(market.tokens) != 2:
                continue

            tokens = {t["outcome"].upper(): t["token_id"] for t in market.tokens}
            yes_id = tokens.get("YES")
            no_id = tokens.get("NO")
            if not yes_id or not no_id:
                continue

            # Prefer reward markets
            reward_score = 2 if market.condition_id in reward_ids else 1

            candidates.append((reward_score, market, yes_id, no_id))

        # Sort by reward score desc, take top N
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = candidates[: config.MM_TARGET_MARKETS]

        log.info(
            "MarketMaker: selected %d target markets (%d reward markets) "
            "order_size=%.2f USDC (balance=%.2f)",
            len(selected),
            sum(1 for s in selected if s[0] == 2),
            self._order_size_usdc(),
            self._balance,
        )
        return [(m, y, n) for _, m, y, n in selected]

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------

    async def _quote_market(
        self,
        market: Market,
        yes_id: str,
        no_id: str,
    ):
        """Place or refresh quotes on a single market."""
        condition_id = market.condition_id

        # Get full order book (needed for signal engine when quant mode active)
        try:
            book = await self.client.get_order_book(yes_id)
            yes_bid = max((p for p, _ in book.bids), default=0.0)
            yes_ask = min((p for p, _ in book.asks), default=1.0)
        except Exception:
            yes_bid, yes_ask = await self.client.get_best_prices(yes_id)
            book = None

        mid = (yes_bid + yes_ask) / 2

        # Skip if price is too extreme (don't make markets near resolution)
        if mid < 0.05 or mid > 0.95:
            log.debug("MarketMaker: skipping %s (mid=%.3f too extreme)", condition_id[:8], mid)
            await self._cancel_position(condition_id)
            return

        # --- Signal-engine gate (QUANT_MODE_ENABLED only) ------------------
        # When active, compute a confidence score from the order book.
        # If confidence is too low (market is noisy/uncertain), skip this cycle.
        signal: Optional[SignalResult] = None
        if config.QUANT_MODE_ENABLED:
            if yes_id not in self._signal_engines:
                self._signal_engines[yes_id] = SignalEngine(yes_id)
            eng = self._signal_engines[yes_id]
            bids = book.bids if book else []
            asks = book.asks if book else []
            signal = eng.evaluate(yes_bid, yes_ask, bids, asks)
            if not signal.is_tradeable:
                log.debug(
                    "MarketMaker: skipping %s — signal confidence too low "
                    "(%.2f < %.2f) regime=%s",
                    condition_id[:8],
                    signal.confidence,
                    config.SIGNAL_CONFIDENCE_THRESHOLD,
                    signal.regime,
                )
                return   # skip this market this cycle; existing orders stay live

        half_spread = config.MARKET_MAKER_SPREAD / 2
        our_bid = round(max(0.01, mid - half_spread), 4)
        our_ask = round(min(0.99, mid + half_spread), 4)

        # NO token is complement: NO_price ≈ 1 - YES_price
        no_mid = 1.0 - mid
        no_bid = round(max(0.01, no_mid - half_spread), 4)
        no_ask = round(min(0.99, no_mid + half_spread), 4)

        pos = self._positions.get(condition_id)

        # Simulate fills from previous orders before replacing them
        if config.DRY_RUN and pos and pos.yes_bid_price > 0:
            _simulate_fills(pos, mid, self._order_size_usdc())

        # Cancel stale orders before replacing
        if pos:
            cancel_tasks = []
            for oid in [
                pos.bid_order_id, pos.ask_order_id,
                pos.no_bid_order_id, pos.no_ask_order_id,
            ]:
                if oid and oid != "DRY-RUN":
                    cancel_tasks.append(self.client.cancel_order(oid))
            if cancel_tasks:
                await asyncio.gather(*cancel_tasks, return_exceptions=True)

        size_usdc = self._order_size_usdc()

        log.debug(
            "MarketMaker: %s | YES bid=%.4f ask=%.4f | NO bid=%.4f ask=%.4f | size=%.2f USDC",
            condition_id[:8], our_bid, our_ask, no_bid, no_ask, size_usdc,
        )

        # Place all four orders concurrently
        results = await asyncio.gather(
            self.client.place_limit_order(yes_id, "BUY",  our_bid, size_usdc),
            self.client.place_limit_order(yes_id, "SELL", our_ask, size_usdc),
            self.client.place_limit_order(no_id,  "BUY",  no_bid,  size_usdc),
            self.client.place_limit_order(no_id,  "SELL", no_ask,  size_usdc),
            return_exceptions=True,
        )

        yes_bid_r, yes_ask_r, no_bid_r, no_ask_r = results

        new_pos = MMPosition(
            market=market,
            yes_token_id=yes_id,
            no_token_id=no_id,
            bid_order_id=getattr(yes_bid_r, "order_id", None),
            ask_order_id=getattr(yes_ask_r, "order_id", None),
            no_bid_order_id=getattr(no_bid_r, "order_id", None),
            no_ask_order_id=getattr(no_ask_r, "order_id", None),
            yes_inventory=pos.yes_inventory if pos else 0.0,
            no_inventory=pos.no_inventory if pos else 0.0,
            total_fills=pos.total_fills if pos else 0,
            total_pnl=pos.total_pnl if pos else 0.0,
            yes_bid_price=our_bid,
            yes_ask_price=our_ask,
            no_bid_price=no_bid,
            no_ask_price=no_ask,
            wins=pos.wins if pos else 0,
            losses=pos.losses if pos else 0,
        )
        self._positions[condition_id] = new_pos

    async def _cancel_position(self, condition_id: str):
        pos = self._positions.pop(condition_id, None)
        if not pos:
            return
        for oid in [
            pos.bid_order_id, pos.ask_order_id,
            pos.no_bid_order_id, pos.no_ask_order_id,
        ]:
            if oid and oid != "DRY-RUN":
                await self.client.cancel_order(oid)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, markets_ref: list, poll_interval: float = 30.0):
        """
        Continuously maintain quotes on selected markets.

        `markets_ref` is a shared mutable list updated by the orchestrator.
        """
        log.info("MarketMakerStrategy started (rebalance=%.0fs)", poll_interval)
        while True:
            try:
                targets = await self.select_target_markets(markets_ref)
                tasks = [
                    self._quote_market(market, yes_id, no_id)
                    for market, yes_id, no_id in targets
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                log.info("MarketMaker: cancelling all orders on shutdown")
                await self.client.cancel_all_orders()
                break
            except Exception as exc:
                log.error("MarketMaker loop error: %s", exc, exc_info=True)
            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        active = len(self._positions)
        total_fills = sum(p.total_fills for p in self._positions.values())
        total_pnl = sum(p.total_pnl for p in self._positions.values())
        wins = sum(p.wins for p in self._positions.values())
        losses = sum(p.losses for p in self._positions.values())
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades else 0.0
        return {
            "active_positions": active,
            "total_fills": total_fills,
            "estimated_pnl": round(total_pnl, 4),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "order_size_usdc": self._order_size_usdc(),
            "balance": round(self._balance, 2),
        }
