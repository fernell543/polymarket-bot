"""
Live execution realism layer.
==============================
Models frictions that paper/backtest modes miss, making live edge estimates
conservative and realistic before capital is deployed.

Friction components:
  1. Slippage model       — function of spread width and order size vs depth
  2. Partial-fill model   — large orders vs thin depth → fractional fill
  3. Queue-position model — limit orders wait behind existing queue
  4. Cancel/replace cost  — round-trip latency budget for stale quote cleanup
  5. Stale-book detection — refuse to trade on outdated best-price data

All estimates are intentionally conservative: when in doubt, assume worse
real-world outcomes than backtest implies.

Usage:
    rl = LiveRealism()
    slip    = rl.slippage_estimate(spread=0.04, size_usdc=50, depth_usdc=300)
    fill    = rl.partial_fill_fraction(size_usdc=50, depth_usdc=300)
    re_edge = rl.realized_edge(theoretical_edge=0.05, slippage=slip,
                                partial_fill_frac=fill)
    chase_ok = rl.chase_allowed(signal_price=0.40, current_price=0.43,
                                 spread=0.04, urgency=0.5)
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass
from typing import Optional

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Realism estimates
# ---------------------------------------------------------------------------

@dataclass
class RealismEstimate:
    """Full live-friction breakdown for one prospective trade."""
    slippage:           float   # fractional slippage (e.g. 0.012 = 1.2%)
    partial_fill_frac:  float   # expected immediate fill fraction [0..1]
    realized_edge:      float   # theoretical_edge net of frictions
    chase_allowed:      bool    # whether price move since signal is acceptable
    book_fresh:         bool    # whether the book timestamp is fresh enough
    reason:             str     # human-readable verdict


@dataclass
class SpreadSnapshot:
    spread: float
    ts: float  # monotonic time of measurement


class LiveRealism:
    """
    Stateful realism estimator.

    Maintains a short rolling history of spread snapshots per token to
    detect sudden spread widening (instability signal).

    Instantiate once in ExecutionManager and call per-trade.
    """

    def __init__(self) -> None:
        # token_id → deque of (spread, monotonic_ts)
        self._spread_history: dict[str, collections.deque[SpreadSnapshot]] = {}
        # token_id → monotonic time of last price-book update
        self._book_ts: dict[str, float] = {}

    # ------------------------------------------------------------------
    # External feed
    # ------------------------------------------------------------------

    def record_book_update(self, token_id: str) -> None:
        """Call whenever get_best_prices() returns a fresh result."""
        self._book_ts[token_id] = time.monotonic()

    def record_spread(self, token_id: str, spread: float) -> None:
        """Call whenever a spread is observed (even in scan loops)."""
        q = self._spread_history.setdefault(token_id, collections.deque(maxlen=30))
        q.append(SpreadSnapshot(spread=spread, ts=time.monotonic()))

    # ------------------------------------------------------------------
    # Slippage model
    # ------------------------------------------------------------------

    def slippage_estimate(
        self,
        spread:     float,
        size_usdc:  float,
        depth_usdc: float = 500.0,
    ) -> float:
        """
        Estimate expected slippage for a market order of `size_usdc`.

        Formula (conservative):
            slip = SLIP_BASE
                   + spread * SLIP_SPREAD_FACTOR
                   + (size_usdc / 100) * SLIP_IMPACT_FACTOR

        The spread component captures half-spread cost (we cross it on entry).
        The impact component captures market-impact for larger orders.

        Returns fractional slippage (e.g. 0.012 = 1.2%).
        """
        base    = config.LIVE_SLIP_BASE
        spread_component = spread * config.LIVE_SLIP_SPREAD_FACTOR
        impact_component = (size_usdc / 100.0) * config.LIVE_SLIP_IMPACT_FACTOR

        # Thin-depth penalty: if size > depth, additional adverse selection
        if depth_usdc > 0 and size_usdc > depth_usdc * config.LIVE_DEPTH_FILL_FRACTION:
            overflow = (size_usdc - depth_usdc * config.LIVE_DEPTH_FILL_FRACTION) / depth_usdc
            thin_penalty = overflow * spread * 0.5  # extra half-spread per overflow unit
        else:
            thin_penalty = 0.0

        slip = base + spread_component + impact_component + thin_penalty
        return round(min(slip, spread), 6)   # cap at full spread

    # ------------------------------------------------------------------
    # Partial-fill model
    # ------------------------------------------------------------------

    def partial_fill_fraction(
        self,
        size_usdc:  float,
        depth_usdc: float = 500.0,
    ) -> float:
        """
        Estimate what fraction of `size_usdc` fills immediately.

        Assumption: LIVE_DEPTH_FILL_FRACTION × depth_usdc is available at the
        best price level.  Orders larger than that fill partially; the remainder
        sits in queue where it faces adverse selection or timeout.

        Returns a value in [0, 1].  1.0 means full immediate fill.
        """
        if size_usdc <= 0:
            return 1.0
        available = depth_usdc * config.LIVE_DEPTH_FILL_FRACTION
        frac = min(1.0, available / size_usdc)
        return round(frac, 4)

    # ------------------------------------------------------------------
    # Realized edge
    # ------------------------------------------------------------------

    def realized_edge(
        self,
        theoretical_edge: float,
        slippage:         float,
        partial_fill_frac: float,
    ) -> float:
        """
        Adjust theoretical edge for live frictions.

        Penalties:
          1. Direct slippage cost (subtracted in full).
          2. Partial-fill penalty: unfilled portion earns no edge but
             still incurs adverse-selection cost (0.3× slippage of notional
             exposure while waiting in queue).

        A negative result means the trade destroys edge after costs.
        """
        # Filled portion earns edge - slippage
        filled_edge = theoretical_edge - slippage

        # Unfilled portion: loses a fraction of the waiting cost
        unfilled_frac = 1.0 - partial_fill_frac
        queue_adverse_selection_cost = unfilled_frac * slippage * 0.3

        net = filled_edge - queue_adverse_selection_cost
        return round(net, 6)

    # ------------------------------------------------------------------
    # Spread stability check
    # ------------------------------------------------------------------

    def spread_is_stable(self, token_id: str, current_spread: float) -> bool:
        """
        Return True if the spread has not widened by more than
        LIVE_SPREAD_MAX_MOVE_PCT in the last LIVE_SPREAD_STABILITY_SECS.

        A sudden spread widening indicates a market-maker pullback —
        risky to enter (likely to fill at a worse price or not at all).
        """
        q = self._spread_history.get(token_id)
        if not q:
            return True  # no history → assume stable (first look)

        cutoff = time.monotonic() - config.LIVE_SPREAD_STABILITY_SECS
        recent = [s for s in q if s.ts >= cutoff]
        if not recent:
            return True

        min_spread = min(s.spread for s in recent)
        if min_spread <= 0:
            return True

        widening = (current_spread - min_spread) / min_spread
        stable = widening <= config.LIVE_SPREAD_MAX_MOVE_PCT
        if not stable:
            log.debug(
                "LiveRealism: spread unstable token=%s  current=%.4f  "
                "min_recent=%.4f  widen=%.1f%%",
                token_id[:8], current_spread, min_spread, widening * 100,
            )
        return stable

    # ------------------------------------------------------------------
    # Avoid-chase logic
    # ------------------------------------------------------------------

    def chase_allowed(
        self,
        signal_price:  float,
        current_price: float,
        spread:        float,
        urgency:       float = 0.5,
    ) -> bool:
        """
        Return True if it is acceptable to trade at `current_price` given
        that the signal was generated at `signal_price`.

        If the market has moved more than LIVE_CHASE_MAX_MOVE_SPREADS × spread
        away from signal_price, refuse the trade unless urgency exceeds
        LIVE_CHASE_URGENCY_OVERRIDE.

        This prevents filling late on a momentum move after the edge has
        already been consumed by earlier participants.
        """
        if spread <= 0:
            return True  # can't measure — allow

        price_move = abs(current_price - signal_price)
        move_in_spreads = price_move / spread

        if move_in_spreads <= config.LIVE_CHASE_MAX_MOVE_SPREADS:
            return True

        # Price has moved significantly — only allow if very urgent
        if urgency >= config.LIVE_CHASE_URGENCY_OVERRIDE:
            log.debug(
                "LiveRealism: allowing chase (urgency=%.2f >= %.2f)  "
                "move=%.2f spreads",
                urgency, config.LIVE_CHASE_URGENCY_OVERRIDE, move_in_spreads,
            )
            return True

        log.debug(
            "LiveRealism: blocking chase  move=%.2f spreads > %.2f threshold  "
            "urgency=%.2f < %.2f override",
            move_in_spreads, config.LIVE_CHASE_MAX_MOVE_SPREADS,
            urgency, config.LIVE_CHASE_URGENCY_OVERRIDE,
        )
        return False

    # ------------------------------------------------------------------
    # Book freshness
    # ------------------------------------------------------------------

    def book_is_fresh(self, token_id: str) -> bool:
        """
        Return True if the order book for `token_id` was updated within
        LIVE_BOOK_STALE_SECS seconds.

        Call record_book_update() each time get_best_prices() succeeds.
        """
        last_update = self._book_ts.get(token_id)
        if last_update is None:
            return False   # never seen — treat as stale
        age = time.monotonic() - last_update
        return age <= config.LIVE_BOOK_STALE_SECS

    # ------------------------------------------------------------------
    # Full composite check
    # ------------------------------------------------------------------

    def evaluate(
        self,
        token_id:         str,
        signal_price:     float,
        current_price:    float,
        spread:           float,
        size_usdc:        float,
        depth_usdc:       float,
        theoretical_edge: float,
        urgency:          float = 0.5,
    ) -> RealismEstimate:
        """
        Run all realism checks and return a composite estimate.

        This is the single call that ExecutionManager and WalletClone
        use before deciding whether to place an order.
        """
        slip       = self.slippage_estimate(spread, size_usdc, depth_usdc)
        fill_frac  = self.partial_fill_fraction(size_usdc, depth_usdc)
        re_edge    = self.realized_edge(theoretical_edge, slip, fill_frac)
        chase_ok   = self.chase_allowed(signal_price, current_price, spread, urgency)
        fresh      = self.book_is_fresh(token_id)
        spread_ok  = self.spread_is_stable(token_id, spread)

        all_ok = chase_ok and fresh and spread_ok and re_edge > 0
        reasons = []
        if not fresh:
            reasons.append(f"stale book (>{config.LIVE_BOOK_STALE_SECS}s)")
        if not chase_ok:
            reasons.append("price chased beyond threshold")
        if not spread_ok:
            reasons.append("spread unstable")
        if re_edge <= 0:
            reasons.append(f"realized edge negative ({re_edge:.4f})")

        reason = "ok" if all_ok else "; ".join(reasons)

        return RealismEstimate(
            slippage=slip,
            partial_fill_frac=fill_frac,
            realized_edge=re_edge,
            chase_allowed=chase_ok,
            book_fresh=fresh,
            reason=reason,
        )
