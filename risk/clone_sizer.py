"""
Clone Dynamic Sizing Model
===========================
Profile-matched or adaptive per-trade sizing for clone/HF strategies.

Replaces fixed-base sizing with target-wallet-behavior-aware computation,
using inferred profile fields: size quantiles, turnover targets, confidence,
market regime, and live liquidity/depth signals.

Two modes (CLONE_SIZE_MODE env var):
  profile  — maps edge_proxy + confidence into the target wallet's observed
             size distribution (min_size_usdc … max_size_usdc, anchored at
             base_size_usdc).  Set CLONE_SIZE_MATCH_TARGET=1 (default).
  adaptive — edge-scalar × confidence × liquidity multiplier, anchored at
             profile.base_size_usdc.  Behaves like PositionSizer but
             clone-specific.

Global hard caps are always applied last regardless of mode.

Config knobs (env-overridable):
  CLONE_SIZE_MODE=profile|adaptive   sizing algorithm (default: profile)
  CLONE_SIZE_MIN_USDC=2.0            absolute floor after all logic
  CLONE_SIZE_MAX_USDC=100.0          absolute ceiling after all logic
  CLONE_SIZE_MATCH_TARGET=1          honour profile min/max bounds (default 1)
  CLONE_SIZE_LIQUIDITY_MULT=1.0      extra depth-scaling factor
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.wallet_clone import CloneProfile
    from health_state import HealthLevel as _HL

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (re-read at import time so tests can patch os.environ before import)
# ---------------------------------------------------------------------------

def _flt(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


CLONE_SIZE_MODE           = os.getenv("CLONE_SIZE_MODE",           "profile")
CLONE_SIZE_MIN_USDC       = _flt("CLONE_SIZE_MIN_USDC",  2.0)
CLONE_SIZE_MAX_USDC       = _flt("CLONE_SIZE_MAX_USDC", 100.0)
CLONE_SIZE_MATCH_TARGET   = os.getenv("CLONE_SIZE_MATCH_TARGET",   "1") == "1"
CLONE_SIZE_LIQUIDITY_MULT = _flt("CLONE_SIZE_LIQUIDITY_MULT", 1.0)
_CLONE_HF_MIN_DEPTH       = _flt("CLONE_MIN_DEPTH_USDC", 100.0)

# Edge floor: below this we output size=0 (not worth entering).
_EDGE_FLOOR_BPS    = 30.0   # 0.30 % — conservative clone-specific floor
# Normalisation: edge at this BPS → scalar=1.0 (for adaptive mode).
_EDGE_REF_BPS      = 100.0  # 1 %
# Max useful edge_proxy range for profile-mode normalisation.
_EDGE_PROXY_CEIL   = 0.15   # 15 % = saturates to full size


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CloneSizeResult:
    """Full sizing trace — log every field for audit / debugging."""
    size_usdc:   float
    mode:        str
    reason:      str
    inputs:      dict[str, Any] = field(default_factory=dict)
    blocked:     bool           = False

    def log_decision(self, label: str = "CloneSizer") -> None:
        log.info(
            "%s: %s  →  %.2f USDC  [blocked=%s]",
            label, self.reason, self.size_usdc, self.blocked,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_clone_size(
    profile:      "Optional[CloneProfile]",
    edge_proxy:   float,            # e.g. 1 - combined_ask - 2*fee
    confidence:   float,            # 0..1
    depth_usdc:   float,            # per-leg book depth (USDC)
    spread:       float,            # bid-ask spread (fraction, e.g. 0.02)
    health_level: Any,              # HealthLevel enum value
    global_max:   float,            # staircase/hard cap from caller
) -> CloneSizeResult:
    """
    Compute per-trade USDC size for a clone / HF position.

    Decision trace is stored in CloneSizeResult.inputs and emitted as
    a structured INFO log so every sizing decision is fully auditable.

    Parameters
    ----------
    profile     : CloneProfile loaded from target wallet JSON (may be None).
    edge_proxy  : Theoretical edge fraction (e.g. 0.05 = 5 %).
    confidence  : Signal / score confidence [0..1].
    depth_usdc  : Available liquidity depth on the leg being sized.
    spread      : Current bid-ask spread as a fraction.
    health_level: HealthLevel enum — blocks on SAFE_MODE / CIRCUIT_BREAKER.
    global_max  : Hard cap from staircase / live mode (non-overridable).

    Returns
    -------
    CloneSizeResult  (size_usdc=0 if blocked)
    """
    # Lazy import to avoid circular deps at module load time
    try:
        from health_state import HealthLevel
        _safe  = HealthLevel.SAFE_MODE
        _cbk   = HealthLevel.CIRCUIT_BREAKER
        is_bad = health_level in (_safe, _cbk)
    except Exception:
        is_bad = False

    # --- Health gate ---
    if is_bad:
        return CloneSizeResult(
            size_usdc=0.0,
            mode=CLONE_SIZE_MODE,
            reason=f"blocked: health={health_level}",
            inputs={"health": str(health_level)},
            blocked=True,
        )

    # --- Edge gate ---
    edge_bps = edge_proxy * 10_000.0
    if edge_bps < _EDGE_FLOOR_BPS:
        return CloneSizeResult(
            size_usdc=0.0,
            mode=CLONE_SIZE_MODE,
            reason=f"blocked: edge_bps={edge_bps:.1f} < floor={_EDGE_FLOOR_BPS}",
            inputs={"edge_bps": edge_bps, "floor": _EDGE_FLOOR_BPS},
            blocked=True,
        )

    # --- Confidence gate ---
    conf_floor = profile.confidence_floor if profile else 0.20
    if confidence < conf_floor:
        return CloneSizeResult(
            size_usdc=0.0,
            mode=CLONE_SIZE_MODE,
            reason=f"blocked: confidence={confidence:.3f} < floor={conf_floor:.3f}",
            inputs={"confidence": confidence, "conf_floor": conf_floor},
            blocked=True,
        )

    # --- Liquidity multiplier ---
    # Scales linearly from 0 → 1 between 0 and 5× min depth.
    raw_liq  = depth_usdc / max(_CLONE_HF_MIN_DEPTH, 1.0)
    liq_mult = min(1.0, raw_liq) * CLONE_SIZE_LIQUIDITY_MULT

    # --- Spread penalty ---
    # If spread > 5%, each extra percent halves remaining size budget.
    if spread > 0.05:
        spread_mult = max(0.30, 1.0 - (spread - 0.05) * 2.0)
    else:
        spread_mult = 1.0

    # --- Core sizing ---
    mode = CLONE_SIZE_MODE
    if mode == "profile" and CLONE_SIZE_MATCH_TARGET:
        size = _profile_mode(profile, edge_proxy, confidence, liq_mult)
    else:
        size = _adaptive_mode(profile, edge_bps, confidence, liq_mult)
        mode = "adaptive"

    size *= spread_mult

    # --- Apply profile bounds (if matching target) ---
    if CLONE_SIZE_MATCH_TARGET and profile:
        size = max(profile.min_size_usdc, min(profile.max_size_usdc, size))

    # --- Apply env-level bounds ---
    size = max(CLONE_SIZE_MIN_USDC, min(CLONE_SIZE_MAX_USDC, size))

    # --- Hard global cap (staircase / live mode — NEVER overridden) ---
    size = min(size, global_max)

    inputs: dict[str, Any] = {
        "mode":           mode,
        "edge_proxy":     round(edge_proxy, 6),
        "edge_bps":       round(edge_bps, 2),
        "confidence":     round(confidence, 4),
        "depth_usdc":     round(depth_usdc, 2),
        "liq_mult":       round(liq_mult, 4),
        "spread":         round(spread, 5),
        "spread_mult":    round(spread_mult, 4),
        "profile_base":   round(profile.base_size_usdc, 2) if profile else None,
        "profile_min":    round(profile.min_size_usdc, 2)  if profile else None,
        "profile_max":    round(profile.max_size_usdc, 2)  if profile else None,
        "global_max":     round(global_max, 2),
        "size_min_usdc":  CLONE_SIZE_MIN_USDC,
        "size_max_usdc":  CLONE_SIZE_MAX_USDC,
    }

    reason = (
        f"mode={mode}  edge={edge_bps:.0f}bps  conf={confidence:.2f}  "
        f"depth={depth_usdc:.0f}  liq={liq_mult:.2f}  spread={spread:.3f}  "
        f"spread_mult={spread_mult:.2f}  global_max={global_max:.2f}"
        f"  →  {size:.2f} USDC"
    )

    result = CloneSizeResult(size_usdc=size, mode=mode, reason=reason, inputs=inputs)
    result.log_decision()
    return result


# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------

def _profile_mode(
    profile: "Optional[CloneProfile]",
    edge_proxy: float,
    confidence: float,
    liq_mult: float,
) -> float:
    """
    Map (edge_proxy, confidence) → size within the profile's observed range.

    Logic:
      edge_norm  = clamp(edge_proxy / EDGE_PROXY_CEIL, 0, 1)
      size_base  = min_size + (max_size - min_size) * edge_norm
      size       = size_base × confidence × liq_mult

    At median edge_proxy ≈ 7.5 % and confidence ≈ 0.70 this produces
    roughly profile.base_size_usdc, reproducing the target's typical trade.
    """
    if profile is None:
        return CLONE_SIZE_MIN_USDC

    edge_norm  = min(1.0, max(0.0, edge_proxy / _EDGE_PROXY_CEIL))
    span       = profile.max_size_usdc - profile.min_size_usdc
    size_base  = profile.min_size_usdc + span * edge_norm
    # Confidence scales ±50 % around size_base (clamped to [0.5, 1.5]).
    conf_mult  = max(0.50, min(1.50, confidence))
    return size_base * conf_mult * liq_mult


def _adaptive_mode(
    profile: "Optional[CloneProfile]",
    edge_bps: float,
    confidence: float,
    liq_mult: float,
) -> float:
    """
    Adaptive sizing anchored at profile.base_size_usdc.
    Matches PositionSizer formula tuned for clone context.

      edge_scalar = clamp(edge_bps / EDGE_REF_BPS, 0, 2.0)
      size = base × edge_scalar × confidence × liq_mult
    """
    base        = profile.base_size_usdc if profile else CLONE_SIZE_MIN_USDC
    edge_scalar = min(2.0, max(0.0, edge_bps / _EDGE_REF_BPS))
    return base * edge_scalar * confidence * liq_mult
