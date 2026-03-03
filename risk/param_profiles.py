"""
Regime-adaptive parameter profiles.
=====================================
Defines three trading profiles — conservative, balanced, aggressive —
each specifying overrides for key strategy parameters.  The active
profile is chosen by:

  1. PARAM_PROFILE config ("conservative" | "balanced" | "aggressive" | "auto")
  2. In "auto" mode: the current market regime selects the profile:
       unknown   → conservative
       ranging   → balanced
       trending  → aggressive

Hard caps from the global risk engine are ALWAYS enforced regardless of
the active profile.

Usage (inside a strategy):
    from risk.param_profiles import get_active_profile
    p = get_active_profile(regime="ranging")
    threshold = p["signal_confidence_threshold"]
    min_edge  = p["exec_min_edge"]

Usage (runtime override — useful for run_paper_super.ps1):
    PARAM_PROFILE=balanced python bot.py

Profiles are deliberately conservative by default:
  - All spread gates are non-zero (avoid razor-thin-spread markets)
  - Conservative profile is a safe fallback when regime is ambiguous
"""

from __future__ import annotations

import logging

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------

PROFILES: dict[str, dict] = {
    "conservative": {
        # Signal filtering — stricter confidence + edge requirements
        "signal_confidence_threshold":   0.55,
        "exec_min_edge":                 0.018,
        # Execution — prefer limit orders, never chase market
        "exec_taker_confidence_threshold": 0.90,
        "exec_taker_urgency_threshold":    0.95,
        # Market making — wider spread, lower capital deployment
        "market_maker_spread":           0.025,
        "mm_capital_pct_scale":          0.70,   # multiply MM_CAPITAL_PCT by this
        # Risk — tighter vol target, longer cooldown
        "risk_target_vol_scale":         0.70,   # multiply RISK_TARGET_VOL by this
        "risk_cooldown_scale":           1.50,   # multiply RISK_COOLDOWN_SECS by this
        # Spread gate: skip markets tighter than this (thin spread = harder to earn)
        "mm_spread_gate":                0.010,
        # Label
        "profile_name": "conservative",
    },

    "balanced": {
        "signal_confidence_threshold":   0.42,
        "exec_min_edge":                 0.012,
        "exec_taker_confidence_threshold": 0.80,
        "exec_taker_urgency_threshold":    0.90,
        "market_maker_spread":           0.020,
        "mm_capital_pct_scale":          1.00,   # no change to MM_CAPITAL_PCT
        "risk_target_vol_scale":         1.00,
        "risk_cooldown_scale":           1.00,
        "mm_spread_gate":                0.005,
        "profile_name": "balanced",
    },

    "aggressive": {
        "signal_confidence_threshold":   0.32,
        "exec_min_edge":                 0.008,
        "exec_taker_confidence_threshold": 0.70,
        "exec_taker_urgency_threshold":    0.85,
        "market_maker_spread":           0.015,
        "mm_capital_pct_scale":          1.30,   # 30% more capital per order
        "risk_target_vol_scale":         1.30,
        "risk_cooldown_scale":           0.75,   # shorter cooldown
        "mm_spread_gate":                0.002,
        "profile_name": "aggressive",
    },
}

# Regime → profile mapping for "auto" mode
_REGIME_TO_PROFILE: dict[str, str] = {
    "unknown":  "conservative",
    "ranging":  "balanced",
    "trending": "aggressive",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_profile(regime: str = "unknown") -> dict:
    """
    Return the parameter profile dict appropriate for the given regime.

    If PARAM_PROFILE is set to a specific profile name, that overrides
    regime-based selection.  In "auto" mode, the regime drives the choice.

    All hard-cap config values (RISK_MAX_MARKET_EXPOSURE, MAX_POSITION_USDC,
    etc.) are NOT included — they are always enforced by the risk engine.
    """
    forced = getattr(config, "PARAM_PROFILE", "auto").lower()

    if forced in PROFILES:
        profile_name = forced
    else:
        # "auto" mode: choose by regime
        profile_name = _REGIME_TO_PROFILE.get(regime, "balanced")

    profile = dict(PROFILES[profile_name])   # shallow copy

    log.debug(
        "ParamProfile: regime=%s forced=%s → profile=%s",
        regime, forced, profile_name,
    )
    return profile


def describe_profiles() -> str:
    """Return a human-readable table of all profiles (for --help / README)."""
    lines = ["Param Profiles:", ""]
    for name, p in PROFILES.items():
        lines.append(f"  [{name.upper()}]")
        for k, v in p.items():
            if k == "profile_name":
                continue
            lines.append(f"    {k:<40s} = {v}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI (for inspection)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(describe_profiles())
    print()
    for regime in ("unknown", "ranging", "trending"):
        p = get_active_profile(regime)
        print(f"  regime={regime:10s} → profile={p['profile_name']}")
