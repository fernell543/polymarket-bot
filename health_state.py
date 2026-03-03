"""
Bot-wide health state.
======================
A simple state machine that aggregates all data-quality and risk signals
into one place.  Every strategy loop should consult HealthState before
opening a new position.

Health levels (in order of severity):

  NORMAL          — All systems healthy; full trading allowed.
  DEGRADED        — Minor issue (e.g. stale price, few markets);
                    existing positions may be maintained but sizing
                    should be conservative.
  SAFE_MODE       — No new entries; only maintenance (cancel/requote)
                    allowed.  Triggered by: stale price feed,
                    severely truncated market list, or explicit flag.
  CIRCUIT_BREAKER — Hard no-trade.  Triggered by risk-engine circuit
                    breaker (daily-loss or drawdown limits hit).

The orchestrator (bot.py) calls update() based on:
  - BTC price feed freshness / has_price
  - Market cache size vs MIN_MARKETS_THRESHOLD
  - Risk engine circuit breaker state
  - Explicit SAFE_MODE env flag

Strategies call ok_to_trade() or check .level directly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum, auto

log = logging.getLogger(__name__)


class HealthLevel(Enum):
    NORMAL          = 0
    DEGRADED        = 1
    SAFE_MODE       = 2
    CIRCUIT_BREAKER = 3

    def __le__(self, other: "HealthLevel") -> bool:
        return self.value <= other.value

    def __lt__(self, other: "HealthLevel") -> bool:
        return self.value < other.value


@dataclass
class HealthState:
    """
    Shared health-state object.

    Instantiated once in bot.py; passed by reference to strategies.

    Example
    -------
        if not health.ok_to_trade():
            log.warning("Health: %s", health.summary)
            continue
    """

    level:            HealthLevel = HealthLevel.NORMAL
    reason:           str         = "startup"
    updated:          float       = 0.0    # monotonic timestamp of last update
    size_multiplier:  float       = 1.0    # set by kill-switch (0.5 = Tier 2)

    def set(self, level: HealthLevel, reason: str) -> None:
        """Update health level; logs any transition."""
        if self.level != level:
            direction = "↑ improving" if level < self.level else "↓ degrading"
            log.warning(
                "HealthState %s: %s → %s | %s",
                direction, self.level.name, level.name, reason,
            )
        self.level  = level
        self.reason = reason
        self.updated = time.monotonic()

    def escalate(self, level: HealthLevel, reason: str) -> None:
        """Only set if `level` is worse than current level."""
        if level > self.level:
            self.set(level, reason)

    def recover(self, level: HealthLevel, reason: str) -> None:
        """Only set if `level` is better than current level."""
        if level < self.level:
            self.set(level, reason)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def ok_to_trade(self) -> bool:
        """True when new positions can be opened."""
        return self.level == HealthLevel.NORMAL

    def maintenance_allowed(self) -> bool:
        """
        True when cancel/requote operations are permitted.
        (CIRCUIT_BREAKER blocks even maintenance.)
        """
        return self.level != HealthLevel.CIRCUIT_BREAKER

    @property
    def summary(self) -> str:
        age = time.monotonic() - self.updated if self.updated > 0 else -1
        mult_str = f" size_mult={self.size_multiplier:.1f}" if self.size_multiplier != 1.0 else ""
        return (
            f"level={self.level.name} reason={self.reason!r} "
            f"updated={age:.0f}s ago{mult_str}"
        )
