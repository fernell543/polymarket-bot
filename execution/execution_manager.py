"""
Execution manager — adaptive order routing and pre-trade validation.
====================================================================
Wraps PolymarketClient with three layers of execution intelligence:

  1. Pre-trade edge check
     Before any order is placed, verify that expected net edge
     (fair_value - price - fee - slippage) clears EXEC_MIN_EDGE.
     Orders with insufficient edge are silently skipped.

  2. Adaptive maker-vs-taker selection
     Uses spread width, signal confidence, and urgency to choose
     between a limit order (maker, cheaper) and a market/FOK order
     (taker, faster fill).  Controlled by EXEC_TAKER_* thresholds.

  3. Order-timeout / partial-fill handling
     Tracks all pending limit order IDs and their placement time.
     Call cancel_timed_out_orders() periodically to cancel any
     order that has been open longer than EXEC_ORDER_TIMEOUT_SECS.
     Timed-out orders are cancelled and can be re-quoted next cycle.

Gated by QUANT_MODE_ENABLED:
  - When False: edge check always passes; "LIMIT" is always chosen;
    timeout tracking still runs (safe, no harm).
  - When True : full logic active.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import config
from client import PolymarketClient, PlacedOrder

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Edge check
# ---------------------------------------------------------------------------

@dataclass
class EdgeCheckResult:
    """Result of a pre-trade expected-edge calculation."""
    ok:            bool
    expected_edge: float    # net fractional edge (e.g. 0.03 = 3%)
    gross_edge:    float    # before fees / slippage
    reason:        str


def check_edge(
    price:      float,
    fair_value: float,
    fee:        float     = config.POLYMARKET_FEE,
    slippage:   float     = 0.002,
) -> EdgeCheckResult:
    """
    Compute expected net edge for a BUY at `price` vs `fair_value`.

    edge = (fair_value - price) - fee - slippage

    Returns ok=True only when QUANT_MODE_ENABLED=False **or**
    net_edge >= EXEC_MIN_EDGE.
    """
    gross = fair_value - price
    net   = gross - fee - slippage

    if not config.QUANT_MODE_ENABLED:
        # In non-quant mode, always pass — existing strategies handle their
        # own entry criteria.
        return EdgeCheckResult(ok=True, expected_edge=net, gross_edge=gross,
                               reason="quant mode disabled — edge check bypassed")

    if net < config.EXEC_MIN_EDGE:
        return EdgeCheckResult(
            ok=False,
            expected_edge=round(net, 6),
            gross_edge=round(gross, 6),
            reason=(
                f"edge too thin: net={net:.4f} < threshold={config.EXEC_MIN_EDGE:.4f} "
                f"(gross={gross:.4f} fee={fee:.4f} slip={slippage:.4f})"
            ),
        )

    return EdgeCheckResult(
        ok=True,
        expected_edge=round(net, 6),
        gross_edge=round(gross, 6),
        reason=f"edge ok: net={net:.4f}",
    )


# ---------------------------------------------------------------------------
# Maker / taker selection
# ---------------------------------------------------------------------------

def choose_order_type(
    spread:     float,
    confidence: float,
    urgency:    float = 0.5,
) -> str:
    """
    Return "LIMIT" (maker) or "MARKET" (taker/FOK).

    Decision logic:
      - confidence >= EXEC_TAKER_CONFIDENCE_THRESHOLD → MARKET
        (high conviction, want guaranteed fill)
      - urgency >= EXEC_TAKER_URGENCY_THRESHOLD → MARKET
        (time-sensitive, e.g. latency-arb near expiry)
      - spread <= EXEC_TAKER_SPREAD_THRESHOLD → LIMIT
        (market is tight, limit almost as fast but earns spread)
      - otherwise → LIMIT (default; be a maker when possible)

    When QUANT_MODE_ENABLED=False always returns "LIMIT" so existing
    strategies are unaffected.
    """
    if not config.QUANT_MODE_ENABLED:
        return "LIMIT"

    if confidence >= config.EXEC_TAKER_CONFIDENCE_THRESHOLD:
        return "MARKET"
    if urgency >= config.EXEC_TAKER_URGENCY_THRESHOLD:
        return "MARKET"
    if spread <= config.EXEC_TAKER_SPREAD_THRESHOLD:
        return "LIMIT"   # tight spread → maker is almost as fast, captures it

    return "LIMIT"


# ---------------------------------------------------------------------------
# Execution manager
# ---------------------------------------------------------------------------

class ExecutionManager:
    """
    Wraps PolymarketClient with adaptive routing, edge checks, and
    pending-order timeout management.

    Shared instance: create once in bot.py and pass to any strategy that
    wants the enhanced execution layer.
    """

    def __init__(self, client: PolymarketClient):
        self.client = client
        # order_id → monotonic timestamp of placement
        self._pending: dict[str, float] = {}
        # Fill-quality counters (reset never — session totals)
        self._total_placed:   int   = 0
        self._total_cancelled: int  = 0
        self._last_latency_ms: float = 0.0
        self._total_latency_ms: float = 0.0

    # ------------------------------------------------------------------
    # Main execute method
    # ------------------------------------------------------------------

    async def execute(
        self,
        token_id:    str,
        side:        str,           # "BUY" or "SELL"
        size_usdc:   float,
        fair_value:  float,         # expected settlement value (e.g. 0.98)
        confidence:  float = 1.0,   # signal confidence [0..1]
        urgency:     float = 0.5,   # 0=patient, 1=time-critical
        limit_price: Optional[float] = None,
        slippage:    float = 0.002,
    ) -> tuple[Optional[PlacedOrder], EdgeCheckResult]:
        """
        Execute an order with full pre-trade validation.

        Returns
        -------
        (placed_order, edge_check)
          placed_order is None if edge check fails or there is no liquidity.
        """
        bid, ask = await self.client.get_best_prices(token_id)
        price = ask if side == "BUY" else bid

        if price <= 0 or price >= 1:
            ec = EdgeCheckResult(
                ok=False, expected_edge=0.0, gross_edge=0.0,
                reason="no liquidity (best price out of range)",
            )
            return None, ec

        spread = ask - bid if ask > bid else 1.0

        # --- Pre-trade edge check -----------------------------------------
        ec = check_edge(price, fair_value, slippage=slippage)
        if not ec.ok:
            log.info(
                "ExecutionManager: skipping %s %s — %s",
                side, token_id[:8], ec.reason,
            )
            return None, ec

        # --- Adaptive order type ------------------------------------------
        order_type = choose_order_type(spread, confidence, urgency)

        # --- Place order (with latency tracking) --------------------------
        _t0 = time.monotonic()
        if order_type == "MARKET":
            placed = await self.client.place_market_order(
                token_id, side, size_usdc
            )
        else:
            lp = limit_price if limit_price is not None else price
            placed = await self.client.place_limit_order(
                token_id, side, lp, size_usdc
            )
        latency_ms = (time.monotonic() - _t0) * 1000.0
        self._last_latency_ms    = latency_ms
        self._total_latency_ms  += latency_ms
        if placed and placed.success:
            self._total_placed += 1

        # Track pending limit orders for timeout management
        if (
            placed
            and placed.success
            and order_type == "LIMIT"
            and placed.order_id not in ("DRY-RUN", "")
        ):
            self._pending[placed.order_id] = time.monotonic()

        return placed, ec

    # ------------------------------------------------------------------
    # Order-timeout / partial-fill handling
    # ------------------------------------------------------------------

    async def cancel_timed_out_orders(
        self,
        timeout_secs: Optional[float] = None,
    ) -> list[str]:
        """
        Cancel any tracked limit orders open longer than `timeout_secs`.

        Returns list of order IDs that were cancelled.

        Call this periodically (e.g. in the stats loop) to clean up stale
        orders that failed to fill.
        """
        if timeout_secs is None:
            timeout_secs = float(config.EXEC_ORDER_TIMEOUT_SECS)

        now = time.monotonic()
        to_cancel = [
            oid for oid, placed_at in list(self._pending.items())
            if now - placed_at > timeout_secs
        ]

        cancelled: list[str] = []
        for oid in to_cancel:
            log.info(
                "ExecutionManager: cancelling timed-out order %s "
                "(open >%.0fs)",
                oid, timeout_secs,
            )
            ok = await self.client.cancel_order(oid)
            if ok:
                self._pending.pop(oid, None)
                cancelled.append(oid)
                self._total_cancelled += 1

        return cancelled

    def mark_filled(self, order_id: str) -> None:
        """Remove an order from the pending tracker (confirmed filled)."""
        self._pending.pop(order_id, None)

    # ------------------------------------------------------------------
    # Fill-quality stats
    # ------------------------------------------------------------------

    def fill_quality_stats(self) -> dict:
        """
        Return a snapshot of session-level fill-quality metrics.

        Useful for the stats loop and fill_quality analytics module.
        """
        avg_lat = (
            self._total_latency_ms / self._total_placed
            if self._total_placed > 0 else 0.0
        )
        cancel_rate = (
            self._total_cancelled / self._total_placed
            if self._total_placed > 0 else 0.0
        )
        return {
            "total_placed":     self._total_placed,
            "total_cancelled":  self._total_cancelled,
            "cancel_rate":      round(cancel_rate, 4),
            "avg_latency_ms":   round(avg_lat, 2),
            "last_latency_ms":  round(self._last_latency_ms, 2),
        }

    # ------------------------------------------------------------------
    # Convenience: run timeout cleanup in a loop
    # ------------------------------------------------------------------

    async def run_timeout_loop(self, interval_secs: float = 30.0) -> None:
        """
        Periodically cancel timed-out orders.

        Run as an asyncio task:
            asyncio.create_task(exec_manager.run_timeout_loop())
        """
        while True:
            try:
                cancelled = await self.cancel_timed_out_orders()
                if cancelled:
                    log.info(
                        "ExecutionManager: cancelled %d timed-out orders",
                        len(cancelled),
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("ExecutionManager timeout loop error: %s", exc)
            await asyncio.sleep(interval_secs)
