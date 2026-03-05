"""
Risk engine for Polymarket bot.
================================
Provides safety-first risk management across all strategies.

Features (all active when QUANT_MODE_ENABLED=1):
  1. Vol-targeted position sizing     — scales order size by realized / target vol.
  2. Per-market exposure cap          — max USDC deployed in any single market.
  3. Portfolio exposure cap           — max total USDC deployed across all markets.
  4. Daily loss limit circuit breaker — hard no-trade when daily P&L < -DAILY_LOSS_LIMIT.
  5. Max drawdown circuit breaker     — hard no-trade when drawdown > MAX_DRAWDOWN.
  6. Consecutive-loss cooldown        — temporary pause after N consecutive losses.

When QUANT_MODE_ENABLED=0 only the hard per-order size cap (MAX_POSITION_USDC)
is applied so the engine integrates safely into existing code paths.

Usage:
    from risk import RiskEngine
    engine = RiskEngine()

    size = engine.size_position(base_usdc=100, market_id=mkt_id,
                                signal_confidence=0.6)
    if not engine.can_trade(market_id):
        return

    # … place order of `size` USDC …
    engine.add_exposure(market_id, size)
    # … later, on trade close:
    engine.record_trade(pnl=+2.5, market_id=mkt_id, size_usdc=size)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State types
# ---------------------------------------------------------------------------

class CircuitBreakerState(Enum):
    CLOSED  = auto()   # normal — trading allowed
    COOLING = auto()   # temporary pause after consecutive losses
    OPEN    = auto()   # hard no-trade (daily loss or drawdown limit hit)


@dataclass
class RiskState:
    """Snapshot of the risk engine state (read-only view)."""
    circuit_breaker:    CircuitBreakerState
    daily_pnl:          float
    peak_pnl:           float
    drawdown:           float
    consecutive_losses: int
    cooldown_until:     float   # monotonic timestamp; 0 = not cooling
    portfolio_exposure: float   # total USDC notional deployed
    position_count:     int     # number of markets with active exposure

    @property
    def is_tradeable(self) -> bool:
        return self.circuit_breaker == CircuitBreakerState.CLOSED

    @property
    def summary(self) -> str:
        cb = self.circuit_breaker.name
        return (
            f"CB={cb} daily_pnl={self.daily_pnl:+.2f} "
            f"drawdown={self.drawdown:.2f} "
            f"consec_loss={self.consecutive_losses} "
            f"exposure={self.portfolio_exposure:.2f} "
            f"positions={self.position_count}"
        )


# ---------------------------------------------------------------------------
# Risk engine
# ---------------------------------------------------------------------------

class RiskEngine:
    """
    Stateful, thread-safe-ish risk manager.

    Designed as a single shared instance (instantiated in bot.py and passed
    to strategies that need it).
    """

    def __init__(self):
        self._daily_pnl:           float = 0.0
        self._peak_pnl:            float = 0.0
        self._drawdown:            float = 0.0
        self._consecutive_losses:  int   = 0
        self._cooldown_until:      float = 0.0
        self._circuit_breaker:     CircuitBreakerState = CircuitBreakerState.CLOSED
        self._day_start:           float = time.time()

        # Per-market: market_id → USDC deployed (gross notional)
        self._market_exposure: dict[str, float] = {}

        # Per-market: rolling price returns for vol estimation
        self._vol_history: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def size_position(
        self,
        base_usdc:         float,
        market_id:         str   = "",
        signal_confidence: float = 1.0,
        realized_vol:      Optional[float] = None,
    ) -> float:
        """
        Return the recommended USDC size for a new order.

        When QUANT_MODE_ENABLED:
          size = base_usdc × vol_scalar × signal_confidence

          vol_scalar = target_vol / realized_vol  (clamped to [0.2, 3.0])

        Always capped by MAX_POSITION_USDC and per-market / portfolio limits.
        """
        if not config.QUANT_MODE_ENABLED:
            return min(base_usdc, config.MAX_POSITION_USDC)

        # --- Vol-targeted scalar -------------------------------------------
        if realized_vol and realized_vol > 1e-6:
            vol_scalar = config.RISK_TARGET_VOL / realized_vol
            vol_scalar = max(0.2, min(3.0, vol_scalar))
        else:
            vol_scalar = 1.0

        size = base_usdc * vol_scalar * max(0.0, min(1.0, signal_confidence))

        # --- Per-market exposure cap ----------------------------------------
        if market_id:
            current_mkt = self._market_exposure.get(market_id, 0.0)
            remaining_mkt = max(0.0, config.RISK_MAX_MARKET_EXPOSURE - current_mkt)
            size = min(size, remaining_mkt)

        # --- Portfolio exposure cap -----------------------------------------
        total_exp = sum(self._market_exposure.values())
        remaining_port = max(0.0, config.RISK_MAX_PORTFOLIO_EXPOSURE - total_exp)
        size = min(size, remaining_port)

        # --- Hard per-order ceiling -----------------------------------------
        size = min(size, config.MAX_POSITION_USDC)

        return round(max(0.0, size), 2)

    # ------------------------------------------------------------------
    # Circuit-breaker guard
    # ------------------------------------------------------------------

    def can_trade(self, market_id: str = "") -> bool:
        """
        Return True if new positions are permitted right now.

        Internally refreshes state (daily reset, limit checks) before deciding.
        """
        self._refresh_state()

        if self._circuit_breaker == CircuitBreakerState.OPEN:
            log.warning(
                "RiskEngine: CIRCUIT BREAKER OPEN — "
                "daily_pnl=%.2f drawdown=%.2f — no new trades",
                self._daily_pnl, self._drawdown,
            )
            return False

        if self._circuit_breaker == CircuitBreakerState.COOLING:
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                log.info(
                    "RiskEngine: cooldown active (%.0fs remaining, %d consec losses)",
                    remaining, self._consecutive_losses,
                )
                return False
            # Cooldown expired — return to CLOSED
            log.info("RiskEngine: cooldown expired — resuming normal trading")
            self._circuit_breaker = CircuitBreakerState.CLOSED
            self._consecutive_losses = 0

        return True

    def _refresh_state(self) -> None:
        """
        Check all hard limits and update circuit breaker if needed.

        HARD GUARDRAILS: daily loss cap and drawdown cap are ALWAYS checked
        regardless of QUANT_MODE_ENABLED, LIVE_DEPLOY_MODE, or any optimizer
        override.  These limits are non-negotiable and cannot be bypassed.
        Kill-switch Tier 3 (NO_TRADE) is also always enforced via
        apply_to_health() in bot._kill_switch_loop().
        """
        now_ts = time.time()
        # Daily reset at midnight UTC
        if now_ts - self._day_start >= 86_400:
            log.info(
                "RiskEngine: new trading day — resetting daily P&L "
                "(previous: %+.2f)",
                self._daily_pnl,
            )
            self._daily_pnl = 0.0
            self._day_start = now_ts
            # Daily-loss-triggered breaker resets each day;
            # drawdown-triggered breaker does NOT auto-reset.
            if self._circuit_breaker == CircuitBreakerState.OPEN:
                if abs(self._daily_pnl) < config.RISK_DAILY_LOSS_LIMIT:
                    self._circuit_breaker = CircuitBreakerState.CLOSED
                    log.info("RiskEngine: daily circuit breaker auto-reset")

        # ---------------------------------------------------------------
        # HARD GUARDRAILS — always enforced, cannot be disabled by config.
        # These run regardless of QUANT_MODE_ENABLED or deployment stage.
        # ---------------------------------------------------------------

        # Hard daily loss limit
        if (
            self._daily_pnl < -config.RISK_DAILY_LOSS_LIMIT
            and self._circuit_breaker != CircuitBreakerState.OPEN
        ):
            log.error(
                "RiskEngine: [HARD GUARDRAIL] DAILY LOSS LIMIT HIT "
                "(%.2f < -%.2f) — CIRCUIT BREAKER OPEN",
                self._daily_pnl, config.RISK_DAILY_LOSS_LIMIT,
            )
            self._circuit_breaker = CircuitBreakerState.OPEN

        # Hard max drawdown
        if (
            self._drawdown > config.RISK_MAX_DRAWDOWN
            and self._circuit_breaker != CircuitBreakerState.OPEN
        ):
            log.error(
                "RiskEngine: [HARD GUARDRAIL] MAX DRAWDOWN HIT "
                "(%.2f > %.2f) — CIRCUIT BREAKER OPEN",
                self._drawdown, config.RISK_MAX_DRAWDOWN,
            )
            self._circuit_breaker = CircuitBreakerState.OPEN

        if not config.QUANT_MODE_ENABLED:
            return  # soft limits below only active in quant mode

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------

    def record_trade(
        self,
        pnl:       float,
        market_id: str   = "",
        size_usdc: float = 0.0,
    ) -> None:
        """
        Record a completed trade outcome.

        Updates daily P&L, drawdown, and consecutive-loss counter.
        Call this after each trade settles (or at end-of-cycle in dry-run).
        """
        self._daily_pnl += pnl

        # Update peak and drawdown
        if self._daily_pnl > self._peak_pnl:
            self._peak_pnl = self._daily_pnl
        current_dd = self._peak_pnl - self._daily_pnl
        if current_dd > self._drawdown:
            self._drawdown = current_dd

        # Consecutive-loss tracking → cooldown
        if pnl < 0:
            self._consecutive_losses += 1
            if (
                config.QUANT_MODE_ENABLED
                and self._consecutive_losses >= config.RISK_MAX_CONSECUTIVE_LOSSES
                and self._circuit_breaker == CircuitBreakerState.CLOSED
            ):
                self._circuit_breaker = CircuitBreakerState.COOLING
                self._cooldown_until = (
                    time.monotonic() + config.RISK_COOLDOWN_SECS
                )
                log.warning(
                    "RiskEngine: %d consecutive losses — "
                    "entering cooldown for %ds",
                    self._consecutive_losses, config.RISK_COOLDOWN_SECS,
                )
        else:
            self._consecutive_losses = 0

        # Reduce market exposure on close
        if market_id and size_usdc > 0:
            self.remove_exposure(market_id, size_usdc)

        log.debug(
            "RiskEngine.record_trade: pnl=%+.2f daily=%+.2f "
            "drawdown=%.2f consec_loss=%d CB=%s",
            pnl, self._daily_pnl, self._drawdown,
            self._consecutive_losses, self._circuit_breaker.name,
        )

    # ------------------------------------------------------------------
    # Exposure tracking
    # ------------------------------------------------------------------

    def add_exposure(self, market_id: str, size_usdc: float) -> None:
        """Record that capital has been deployed into a market."""
        self._market_exposure[market_id] = (
            self._market_exposure.get(market_id, 0.0) + size_usdc
        )

    def remove_exposure(self, market_id: str, size_usdc: float) -> None:
        """Record that capital has been freed from a market."""
        current = self._market_exposure.get(market_id, 0.0)
        self._market_exposure[market_id] = max(0.0, current - size_usdc)

    # ------------------------------------------------------------------
    # Realized volatility
    # ------------------------------------------------------------------

    def update_vol(self, market_id: str, price_return: float) -> None:
        """Push a return sample for rolling vol estimation."""
        hist = self._vol_history.setdefault(market_id, [])
        hist.append(price_return)
        if len(hist) > 50:
            hist.pop(0)

    def realized_vol(self, market_id: str) -> Optional[float]:
        """
        Return the rolling annualised-ish std of returns for a market.
        Returns None if fewer than 5 samples exist.
        """
        hist = self._vol_history.get(market_id, [])
        if len(hist) < 5:
            return None
        mean = sum(hist) / len(hist)
        var = sum((r - mean) ** 2 for r in hist) / (len(hist) - 1)
        return math.sqrt(var)

    # ------------------------------------------------------------------
    # State snapshot
    # ------------------------------------------------------------------

    @property
    def guardrails_summary(self) -> str:
        """
        One-line summary of hard guardrail status.

        These limits are always active regardless of mode or optimizer.
        Suitable for logging in health checks and dashboard display.
        """
        return (
            f"[HARD GUARDRAILS] "
            f"daily_loss_limit=${config.RISK_DAILY_LOSS_LIMIT:.0f}  "
            f"max_drawdown=${config.RISK_MAX_DRAWDOWN:.0f}  "
            f"current_daily_pnl={self._daily_pnl:+.2f}  "
            f"current_drawdown={self._drawdown:.2f}  "
            f"CB={self._circuit_breaker.name}"
        )

    @property
    def state(self) -> RiskState:
        return RiskState(
            circuit_breaker=self._circuit_breaker,
            daily_pnl=round(self._daily_pnl, 4),
            peak_pnl=round(self._peak_pnl, 4),
            drawdown=round(self._drawdown, 4),
            consecutive_losses=self._consecutive_losses,
            cooldown_until=self._cooldown_until,
            portfolio_exposure=round(sum(self._market_exposure.values()), 4),
            position_count=sum(
                1 for v in self._market_exposure.values() if v > 0
            ),
        )
