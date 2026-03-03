"""
Kill-switch escalation tiers.
==============================
Monitors three independent signals and escalates through proportional
response tiers before the hard circuit breaker fires:

  Tier 0 — NORMAL   : no action; size_multiplier = 1.0
  Tier 1 — WARN     : log warning only; size_multiplier = 1.0
  Tier 2 — REDUCE   : 50% size reduction; size_multiplier = 0.5
  Tier 3 — NO_TRADE : block all new entries; size_multiplier = 0.0

Signals monitored (worst tier across all three applies):
┌──────────────────────────┬─────────────┬────────────┬────────────┐
│ Signal                   │  Tier 1     │  Tier 2    │  Tier 3    │
├──────────────────────────┼─────────────┼────────────┼────────────┤
│ API error rate (60s win) │  ≥ 20%      │  ≥ 40%     │  ≥ 60%     │
│ Price feed stale (secs)  │  ≥ 30s      │  ≥ 90s     │  ≥ 300s    │
│ Drawdown pace (pct/hr)   │  ≥ 20%      │  ≥ 50%     │  ≥ 80%     │
└──────────────────────────┴─────────────┴────────────┴────────────┘

All thresholds are env-configurable (see config.py KILL_SWITCH_* vars).

Integration:
    # In bot.py __init__:
    from risk.kill_switch import KillSwitch
    self.kill_switch = KillSwitch()

    # Feed it state (call these frequently from bot loops):
    self.kill_switch.record_api_call(success=True/False)
    self.kill_switch.update_stale_secs(self.price_feed.age_secs)
    self.kill_switch.update_drawdown_pace(
        daily_loss_usdc, config.RISK_DAILY_LOSS_LIMIT, elapsed_hours
    )

    # Strategies read:
    size = raw_size * self.kill_switch.size_multiplier

    # Tier 3 also escalates HealthState to SAFE_MODE so _safe_to_trade()
    # returns False automatically (call kill_switch.apply_to_health(health)).
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------

class Tier(IntEnum):
    NORMAL   = 0
    WARN     = 1
    REDUCE   = 2
    NO_TRADE = 3

    @property
    def size_multiplier(self) -> float:
        return [1.0, 1.0, 0.5, 0.0][self.value]

    @property
    def label(self) -> str:
        return ["NORMAL", "WARN", "REDUCE-50%", "NO-TRADE"][self.value]


# ---------------------------------------------------------------------------
# Internal state snapshot
# ---------------------------------------------------------------------------

@dataclass
class KillSwitchState:
    tier:             Tier
    api_error_rate:   float   # fraction of recent calls that failed
    stale_secs:       float   # age of price feed (seconds)
    dd_pace_pct:      float   # fraction of daily limit consumed per hour
    trigger:          str     # which signal set the current tier


# ---------------------------------------------------------------------------
# KillSwitch
# ---------------------------------------------------------------------------

class KillSwitch:
    """
    Stateful 3-tier kill switch.

    Instantiate once in bot.py and share the reference.
    All methods are safe to call from an async event loop.
    """

    # Rolling window for API error rate (seconds)
    _API_WINDOW_SECS: float = 60.0

    def __init__(self):
        # API call ring buffer: (monotonic_time, success: bool)
        self._api_calls: collections.deque[tuple[float, bool]] = collections.deque()

        # Stale-data age (updated externally)
        self._stale_secs: float = 0.0

        # Drawdown pace (fraction of daily limit per hour)
        self._dd_pace_pct: float = 0.0

        # Last computed tier (cached to avoid log spam on unchanged tier)
        self._last_tier: Tier = Tier.NORMAL

        # Monotonic time of last tier-change (for hysteresis)
        self._tier_since: float = time.monotonic()

    # ------------------------------------------------------------------
    # Feed methods (called by bot loops)
    # ------------------------------------------------------------------

    def record_api_call(self, success: bool) -> None:
        """Record one API call outcome (True = success, False = error)."""
        now = time.monotonic()
        self._api_calls.append((now, success))
        # Prune old entries outside the rolling window
        cutoff = now - self._API_WINDOW_SECS
        while self._api_calls and self._api_calls[0][0] < cutoff:
            self._api_calls.popleft()

    def update_stale_secs(self, stale_secs: float) -> None:
        """Update price-feed age (seconds since last valid price)."""
        self._stale_secs = max(0.0, stale_secs)

    def update_drawdown_pace(
        self,
        current_loss_usdc: float,
        daily_limit_usdc:  float,
        elapsed_hours:     float,
    ) -> None:
        """
        Update the drawdown pace signal.

        pace = (current_loss / daily_limit) / elapsed_hours
        e.g. lost 25% of limit in 0.5 hours → pace = 0.50 per hour → Tier 2
        """
        if daily_limit_usdc <= 0 or elapsed_hours <= 0:
            self._dd_pace_pct = 0.0
            return
        loss_pct = max(0.0, current_loss_usdc) / daily_limit_usdc
        self._dd_pace_pct = loss_pct / elapsed_hours

    # ------------------------------------------------------------------
    # Tier computation
    # ------------------------------------------------------------------

    def _api_error_rate(self) -> float:
        """Fraction of recent API calls that failed."""
        calls = list(self._api_calls)
        if not calls:
            return 0.0
        errors = sum(1 for _, ok in calls if not ok)
        return errors / len(calls)

    def _compute_tier(self) -> tuple[Tier, str]:
        """Return (Tier, trigger_description)."""
        api_rate   = self._api_error_rate()
        stale_secs = self._stale_secs
        dd_pace    = self._dd_pace_pct

        # Thresholds from config (with defaults if keys absent)
        t1_api   = getattr(config, "KS_TIER1_API_ERROR_RATE",   0.20)
        t2_api   = getattr(config, "KS_TIER2_API_ERROR_RATE",   0.40)
        t3_api   = getattr(config, "KS_TIER3_API_ERROR_RATE",   0.60)
        t1_stale = getattr(config, "KS_TIER1_STALE_SECS",       30.0)
        t2_stale = getattr(config, "KS_TIER2_STALE_SECS",       90.0)
        t3_stale = getattr(config, "KS_TIER3_STALE_SECS",      300.0)
        t1_dd    = getattr(config, "KS_TIER1_DD_PACE_PCT",       0.20)
        t2_dd    = getattr(config, "KS_TIER2_DD_PACE_PCT",       0.50)
        t3_dd    = getattr(config, "KS_TIER3_DD_PACE_PCT",       0.80)

        worst_tier   = Tier.NORMAL
        worst_reason = "all signals nominal"

        # API error rate
        if api_rate >= t3_api:
            worst_tier   = Tier.NO_TRADE
            worst_reason = f"API error rate {api_rate:.0%} ≥ T3({t3_api:.0%})"
        elif api_rate >= t2_api:
            if Tier.REDUCE > worst_tier:
                worst_tier   = Tier.REDUCE
                worst_reason = f"API error rate {api_rate:.0%} ≥ T2({t2_api:.0%})"
        elif api_rate >= t1_api:
            if Tier.WARN > worst_tier:
                worst_tier   = Tier.WARN
                worst_reason = f"API error rate {api_rate:.0%} ≥ T1({t1_api:.0%})"

        # Stale price feed
        if stale_secs >= t3_stale:
            if Tier.NO_TRADE > worst_tier:
                worst_tier   = Tier.NO_TRADE
                worst_reason = f"price feed stale {stale_secs:.0f}s ≥ T3({t3_stale:.0f}s)"
        elif stale_secs >= t2_stale:
            if Tier.REDUCE > worst_tier:
                worst_tier   = Tier.REDUCE
                worst_reason = f"price feed stale {stale_secs:.0f}s ≥ T2({t2_stale:.0f}s)"
        elif stale_secs >= t1_stale:
            if Tier.WARN > worst_tier:
                worst_tier   = Tier.WARN
                worst_reason = f"price feed stale {stale_secs:.0f}s ≥ T1({t1_stale:.0f}s)"

        # Drawdown pace
        if dd_pace >= t3_dd:
            if Tier.NO_TRADE > worst_tier:
                worst_tier   = Tier.NO_TRADE
                worst_reason = f"drawdown pace {dd_pace:.0%}/hr ≥ T3({t3_dd:.0%})"
        elif dd_pace >= t2_dd:
            if Tier.REDUCE > worst_tier:
                worst_tier   = Tier.REDUCE
                worst_reason = f"drawdown pace {dd_pace:.0%}/hr ≥ T2({t2_dd:.0%})"
        elif dd_pace >= t1_dd:
            if Tier.WARN > worst_tier:
                worst_tier   = Tier.WARN
                worst_reason = f"drawdown pace {dd_pace:.0%}/hr ≥ T1({t1_dd:.0%})"

        return worst_tier, worst_reason

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def tier(self) -> Tier:
        return self._compute_tier()[0]

    @property
    def size_multiplier(self) -> float:
        return self.tier.size_multiplier

    @property
    def state(self) -> KillSwitchState:
        t, reason = self._compute_tier()
        return KillSwitchState(
            tier=t,
            api_error_rate=round(self._api_error_rate(), 4),
            stale_secs=round(self._stale_secs, 1),
            dd_pace_pct=round(self._dd_pace_pct, 4),
            trigger=reason,
        )

    # ------------------------------------------------------------------
    # Health-state integration
    # ------------------------------------------------------------------

    def apply_to_health(self, health) -> None:
        """
        Escalate HealthState based on current kill-switch tier.

        Tier 3 → SAFE_MODE (no new entries).
        Tier 2 → DEGRADED (conservative sizing via size_multiplier).
        Tier 1 → no health change (just a warning in the log).

        Import HealthLevel lazily to avoid circular imports.
        """
        from health_state import HealthLevel  # local import

        t, reason = self._compute_tier()

        if t != self._last_tier:
            log.warning(
                "KillSwitch tier change: %s → %s | %s",
                self._last_tier.label, t.label, reason,
            )
            self._tier_since = time.monotonic()
            self._last_tier  = t

        if t == Tier.NO_TRADE:
            health.escalate(
                HealthLevel.SAFE_MODE,
                f"kill-switch Tier 3 NO-TRADE: {reason}",
            )
        elif t == Tier.REDUCE:
            health.escalate(
                HealthLevel.DEGRADED,
                f"kill-switch Tier 2 REDUCE-50%: {reason}",
            )
        elif t == Tier.WARN:
            # Log only — don't change health level for a warning tier
            age = time.monotonic() - self._tier_since
            if age < 5:   # only log once right after the transition
                log.warning("KillSwitch Tier 1 WARN: %s", reason)

    @property
    def summary(self) -> str:
        st = self.state
        return (
            f"tier={st.tier.label} size_mult={st.tier.size_multiplier:.1f} "
            f"api_err={st.api_error_rate:.1%} stale={st.stale_secs:.0f}s "
            f"dd_pace={st.dd_pace_pct:.1%}/hr trigger={st.trigger!r}"
        )
