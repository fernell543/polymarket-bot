"""
Position Sizing Engine
======================
Computes USDC order sizes based on:
  - Account balance × risk-budget fraction  (for directional strategies)
  - Expected edge in basis points (must exceed EDGE_FLOOR_BPS or size=0)
  - Signal confidence [0..1]  (below CONFIDENCE_FLOOR → size=0)
  - Volatility/regime multiplier (low/mid/high)
  - Bot health state guard (SAFE_MODE or worse → size=0)

No martingale logic.  Sizes scale monotonically with edge and confidence
and are hard-capped by SIZE_MIN_USDC / SIZE_MAX_USDC.

Protection rules
----------------
  1. edge_bps < EDGE_FLOOR_BPS     → size=0, reason="edge_below_floor"
  2. confidence < CONFIDENCE_FLOOR → size=0, reason="confidence_below_floor"
  3. health >= SAFE_MODE           → size=0, reason="safe_mode"

Core formula (when all guards pass)
------------------------------------
  edge_scalar = clamp(edge_bps / EDGE_REFERENCE_BPS, 0, 2.0)
  size        = base_usdc × edge_scalar × confidence × vol_mult
  size        = clamp(size, SIZE_MIN_USDC, SIZE_MAX_USDC)

Where ``base_usdc`` is either:
  - ``balance × RISK_BUDGET_PCT``  (compute(), for PriceArb / LatencyArb)
  - Caller-supplied base           (apply_to(), for MarketMaker)

Usage
-----
    from risk.sizing import PositionSizer

    sizer = PositionSizer()
    sizer.set_balance(5000.0)

    # Directional strategy:
    result = sizer.compute(edge_bps=120, confidence=0.75,
                           vol_regime="mid", label="LatencyArb")
    if result.size_usdc > 0:
        await place_order(result.size_usdc)

    # Market-maker (caller supplies base from capital-fraction formula):
    result = sizer.apply_to(base_usdc=50.0, edge_bps=100, confidence=1.0,
                            vol_regime="low", label="MarketMaker")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import config
from health_state import HealthLevel

log = logging.getLogger(__name__)

# Hard cap on the edge scalar so one great opportunity can't exceed 2× base.
_EDGE_SCALAR_MAX = 2.0

# Map signal-engine regime names → vol-regime keys used by this module.
_REGIME_MAP: dict[str, str] = {
    "trending": "high",   # trending = more volatile → reduce
    "ranging":  "low",    # ranging  = calmer        → slight increase
    "unknown":  "mid",
    "disabled": "mid",
}


@dataclass
class SizingResult:
    """Outcome of a single sizing decision, with all inputs for structured logging."""
    size_usdc:    float   # 0.0 when any guard triggers
    reason:       str     # "ok" | "edge_below_floor" | "confidence_below_floor" | "safe_mode"
    label:        str     # caller identifier for log messages
    edge_bps:     float
    confidence:   float
    vol_regime:   str
    vol_mult:     float
    edge_scalar:  float
    base_usdc:    float


def signal_regime_to_vol(regime: str) -> str:
    """Convert signal-engine regime string to vol-regime key (low/mid/high)."""
    return _REGIME_MAP.get(regime, "mid")


class PositionSizer:
    """
    Stateful position sizer; one shared instance wired into all strategies.

    The orchestrator (bot.py) calls ``set_balance()`` whenever a fresh account
    balance is available (same call site as ``MarketMakerStrategy.set_balance``).

    Strategies call ``set_sizer()`` (injected by bot.py at startup) then invoke
    ``compute()`` or ``apply_to()`` on every trade decision.
    """

    def __init__(self) -> None:
        self._balance: float = config.MM_STARTING_BALANCE

    # ------------------------------------------------------------------
    # Balance tracking
    # ------------------------------------------------------------------

    def set_balance(self, balance: float) -> None:
        """Update the tracked account balance (USDC)."""
        if balance > 0:
            self._balance = balance

    @property
    def balance(self) -> float:
        return self._balance

    # ------------------------------------------------------------------
    # Public sizing API
    # ------------------------------------------------------------------

    def compute(
        self,
        edge_bps:     float,
        confidence:   float = 1.0,
        vol_regime:   str   = "mid",
        health_level: Optional[HealthLevel] = None,
        label:        str   = "",
    ) -> SizingResult:
        """
        Size a position using ``balance × RISK_BUDGET_PCT`` as the base.

        Use for directional strategies (PriceArb, LatencyArb) where the
        natural risk unit is a fixed fraction of total equity per trade.
        """
        base = self._balance * config.RISK_BUDGET_PCT
        return self._scale_and_guard(base, edge_bps, confidence,
                                     vol_regime, health_level, label)

    def apply_to(
        self,
        base_usdc:    float,
        edge_bps:     float,
        confidence:   float = 1.0,
        vol_regime:   str   = "mid",
        health_level: Optional[HealthLevel] = None,
        label:        str   = "",
    ) -> SizingResult:
        """
        Apply sizing guards and multipliers to a caller-supplied base size.

        Use for MarketMaker, which derives its own base from the
        capital-fraction formula (``balance × MM_CAPITAL_PCT / slots``).
        Guards still apply; multipliers scale the provided base.
        """
        return self._scale_and_guard(base_usdc, edge_bps, confidence,
                                     vol_regime, health_level, label)

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def _scale_and_guard(
        self,
        base_usdc:    float,
        edge_bps:     float,
        confidence:   float,
        vol_regime:   str,
        health_level: Optional[HealthLevel],
        label:        str,
    ) -> SizingResult:

        def _zero(reason: str, vol_mult: float = 1.0,
                  edge_scalar: float = 1.0) -> SizingResult:
            result = SizingResult(
                size_usdc=0.0, reason=reason, label=label,
                edge_bps=edge_bps, confidence=confidence,
                vol_regime=vol_regime, vol_mult=vol_mult,
                edge_scalar=edge_scalar, base_usdc=base_usdc,
            )
            log.info(
                "Sizing[%s]: size=0 reason=%s "
                "edge_bps=%.1f conf=%.3f vol=%s base=%.2f",
                label, reason, edge_bps, confidence, vol_regime, base_usdc,
            )
            return result

        # Guard 1: health state — SAFE_MODE or worse blocks all new sizing
        if health_level is not None:
            if health_level.value >= HealthLevel.SAFE_MODE.value:
                return _zero("safe_mode")

        # Guard 2: edge floor
        if edge_bps < config.EDGE_FLOOR_BPS:
            return _zero("edge_below_floor")

        # Guard 3: confidence floor
        if confidence < config.CONFIDENCE_FLOOR:
            return _zero("confidence_below_floor")

        # Vol regime multiplier
        vol_mult = {
            "low":  config.VOL_MULT_LOW,
            "high": config.VOL_MULT_HIGH,
        }.get(vol_regime, config.VOL_MULT_MID)

        # Edge scalar: normalized to EDGE_REFERENCE_BPS, capped at 2×
        edge_scalar = min(_EDGE_SCALAR_MAX, edge_bps / config.EDGE_REFERENCE_BPS)

        # Core formula
        size = base_usdc * edge_scalar * confidence * vol_mult

        # Hard size caps
        size = max(config.SIZE_MIN_USDC, min(config.SIZE_MAX_USDC, size))
        size = round(size, 2)

        result = SizingResult(
            size_usdc=size, reason="ok", label=label,
            edge_bps=edge_bps, confidence=confidence,
            vol_regime=vol_regime, vol_mult=vol_mult,
            edge_scalar=edge_scalar, base_usdc=base_usdc,
        )
        log.debug(
            "Sizing[%s]: size=%.2f USDC "
            "(base=%.2f × edge_scalar=%.3f × conf=%.3f × vol_mult=%.2f) "
            "edge_bps=%.1f vol=%s",
            label, size,
            base_usdc, edge_scalar, confidence, vol_mult,
            edge_bps, vol_regime,
        )
        return result
