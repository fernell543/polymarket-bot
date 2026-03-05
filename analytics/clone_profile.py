"""
Polymarket Wallet Clone — Behavior Profiling (Deep Analysis)
=============================================================
Reads the extracted CSV produced by scripts/clone_extract.py and computes
a rich behavioral feature set characterising the target wallet's trading style.

Feature extraction:
  - Trade frequency (trades/day, active days, burst patterns)
  - Size distribution (min/p25/median/p75/max USDC per trade)
  - Entry price distribution (typical YES/NO entry price ranges)
  - YES vs NO preference (directional bias)
  - Market category preferences
  - Timing (days-before-market-end at entry)
  - Win/loss proxy (from position P&L if available)

Deep analysis (new):
  - Temporal patterns: time-of-day, day-of-week, dominant trading session
  - Market duration buckets: intraday / short / medium / long / very-long
  - Size-scaling analysis: does size scale with price distance from 0.5?
  - Streak analysis: win/loss streak detection + adaptation signal
  - Holding-period distribution: median/p90 estimated hold time
  - Archetype clustering: rules-based classification into 6 archetypes
  - Confidence-scored parameter bands: high / medium / speculative
  - Experiment matrix: 5 concrete param sets ranked by expected fidelity

Output:
  logs/clone_profile_<wallet>.json          — standard profile (backward-compat)
  logs/clone_profile_<wallet>_detailed.json — full deep analysis
  logs/clone_profile_<wallet>_report.md     — human-readable markdown report
  (printed to stdout as a concise summary table)

Usage:
  python -m analytics.clone_profile --wallet 0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd
  python -m analytics.clone_profile --wallet 0x... --csv logs/clone_source_0x....csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = q * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient."""
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den > 0 else 0.0


def _parse_ts(ts: str) -> Optional[datetime]:
    """Try several timestamp formats."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, OSError):
        return None


def _days_until(entry_ts: Optional[datetime], end_date: str) -> Optional[float]:
    """Return days remaining until market end at entry time."""
    if not entry_ts or not end_date:
        return None
    end = _parse_ts(end_date)
    if end is None:
        return None
    delta = (end - entry_ts).total_seconds() / 86400.0
    return delta if delta >= 0 else None


def _coerce_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# New deep-analysis helpers
# ---------------------------------------------------------------------------

def _temporal_patterns(trades: list[dict]) -> dict:
    """Hour-of-day and day-of-week trade distribution and dominant session."""
    DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hour_counts: dict[int, int] = defaultdict(int)
    dow_counts:  dict[int, int] = defaultdict(int)
    valid_ts = 0

    for r in trades:
        ts = _parse_ts(r.get("timestamp", ""))
        if ts:
            hour_counts[ts.hour] += 1
            dow_counts[ts.weekday()] += 1
            valid_ts += 1

    if valid_ts == 0:
        return {"insufficient_data": True, "n_with_timestamp": 0}

    # 6-hour session buckets (UTC)
    session_labels = {
        0: "asia_00-06",
        1: "london_06-12",
        2: "us_market_12-18",
        3: "us_evening_18-24",
    }
    session_counts: dict[str, int] = defaultdict(int)
    for h, cnt in hour_counts.items():
        session_counts[session_labels[h // 6]] += cnt

    peak_hour = max(hour_counts, key=lambda h: hour_counts[h]) if hour_counts else 0
    peak_dow  = max(dow_counts,  key=lambda d: dow_counts[d])  if dow_counts  else 0
    dominant_session = max(session_counts, key=lambda s: session_counts[s]) if session_counts else "unknown"

    return {
        "n_with_timestamp":    valid_ts,
        "hour_distribution":   {str(h): hour_counts.get(h, 0) for h in range(24)},
        "dow_distribution":    {DOW_NAMES[d]: dow_counts.get(d, 0) for d in range(7)},
        "session_distribution": dict(session_counts),
        "peak_hour":           peak_hour,
        "peak_dow":            DOW_NAMES[peak_dow],
        "dominant_session":    dominant_session,
    }


def _market_duration_buckets(trades: list[dict]) -> dict:
    """Bucket trades by DTE at entry — proxy for preferred market duration."""
    buckets = {
        "intraday_lt1d":  0,
        "short_1_7d":     0,
        "medium_7_30d":   0,
        "long_30_90d":    0,
        "vlong_gt90d":    0,
        "unknown":        0,
    }
    for r in trades:
        ts = _parse_ts(r.get("timestamp", ""))
        dte = _days_until(ts, r.get("end_date", ""))
        if dte is None:
            buckets["unknown"] += 1
        elif dte < 1:
            buckets["intraday_lt1d"] += 1
        elif dte < 7:
            buckets["short_1_7d"] += 1
        elif dte < 30:
            buckets["medium_7_30d"] += 1
        elif dte < 90:
            buckets["long_30_90d"] += 1
        else:
            buckets["vlong_gt90d"] += 1

    total = sum(v for k, v in buckets.items() if k != "unknown")
    pcts = {k: round(v / total, 3) if total > 0 else 0.0
            for k, v in buckets.items() if k != "unknown"}
    dominant = max(pcts, key=lambda k: pcts[k]) if pcts else "unknown"
    duration_labels = {
        "intraday_lt1d": "intraday_scalper",
        "short_1_7d":    "short_term",
        "medium_7_30d":  "medium_term",
        "long_30_90d":   "long_term",
        "vlong_gt90d":   "very_long_term",
    }
    return {
        "counts":              buckets,
        "percentages":         pcts,
        "dominant_bucket":     dominant,
        "duration_preference": duration_labels.get(dominant, "mixed"),
    }


def _size_scaling_analysis(trades: list[dict]) -> dict:
    """
    Analyze relationship between trade size and price.

    A wallet that sizes larger at extreme prices (high conviction at 0.05 or 0.95)
    shows a different risk appetite than one that sizes uniformly or larger at mid prices.
    """
    pairs = []
    for r in trades:
        price    = _coerce_float(r.get("price"))
        size_usd = _coerce_float(r.get("size_usdc"))
        if 0.01 < price < 0.99 and size_usd > 0:
            pairs.append((price, size_usd))

    if len(pairs) < 5:
        return {"insufficient_data": True, "n_samples": len(pairs)}

    prices = [p[0] for p in pairs]
    sizes  = [p[1] for p in pairs]
    abs_prices = [abs(p - 0.5) for p in prices]   # distance from mid

    corr     = _pearson(prices, sizes)
    corr_abs = _pearson(abs_prices, sizes)

    def _regime(pair_list):
        if not pair_list:
            return {"count": 0, "avg_size": 0.0, "median_size": 0.0}
        s = sorted(p[1] for p in pair_list)
        return {
            "count":       len(s),
            "avg_size":    round(_mean(s), 2),
            "median_size": round(_quantile(s, 0.5), 2),
        }

    low_p  = [p for p in pairs if p[0] < 0.30]
    mid_p  = [p for p in pairs if 0.30 <= p[0] <= 0.70]
    high_p = [p for p in pairs if p[0] > 0.70]

    if corr > 0.2:
        direction = "larger_at_high_prices"
    elif corr < -0.2:
        direction = "larger_at_low_prices"
    elif corr_abs > 0.2:
        direction = "larger_at_extreme_prices"
    elif corr_abs < -0.2:
        direction = "larger_at_mid_prices"
    else:
        direction = "price_independent"

    return {
        "n_samples":                      len(pairs),
        "correlation_price_to_size":      round(corr, 4),
        "correlation_extremity_to_size":  round(corr_abs, 4),
        "size_scaling_direction":         direction,
        "regime_low_price_lt030":         _regime(low_p),
        "regime_mid_price_030_070":       _regime(mid_p),
        "regime_high_price_gt070":        _regime(high_p),
    }


def _streak_analysis(positions: list[dict]) -> dict:
    """
    Detect win/loss streak lengths from position P&L data.

    Also produces a rough 'adaptation signal' — did win rate improve over time?
    """
    pnl_seq = []
    for r in positions:
        ts  = _parse_ts(r.get("timestamp", ""))
        pnl = _coerce_float(r.get("pnl_usdc"))
        if ts and pnl != 0:
            pnl_seq.append((ts, pnl))
    pnl_seq.sort(key=lambda x: x[0])

    if len(pnl_seq) < 3:
        return {"insufficient_data": True, "n_pnl_records": len(pnl_seq)}

    outcomes = [1 if p > 0 else -1 for _, p in pnl_seq]

    win_runs:  list[int] = []
    loss_runs: list[int] = []
    cur_len  = 1
    cur_sign = outcomes[0]
    for o in outcomes[1:]:
        if o == cur_sign:
            cur_len += 1
        else:
            (win_runs if cur_sign == 1 else loss_runs).append(cur_len)
            cur_len  = 1
            cur_sign = o
    (win_runs if cur_sign == 1 else loss_runs).append(cur_len)

    # Rough adaptation signal: compare win rate first half vs second half
    mid = len(outcomes) // 2
    wr_first  = sum(1 for o in outcomes[:mid] if o == 1) / mid
    wr_second = sum(1 for o in outcomes[mid:] if o == 1) / max(1, len(outcomes) - mid)
    if wr_second > wr_first + 0.10:
        adapt = "improving"
    elif wr_second < wr_first - 0.10:
        adapt = "deteriorating"
    else:
        adapt = "stable"

    return {
        "n_pnl_records":    len(pnl_seq),
        "max_win_streak":   max(win_runs,  default=0),
        "max_loss_streak":  max(loss_runs, default=0),
        "avg_win_streak":   round(_mean([float(x) for x in win_runs]),  2),
        "avg_loss_streak":  round(_mean([float(x) for x in loss_runs]), 2),
        "win_rate_first_half":  round(wr_first,  3),
        "win_rate_second_half": round(wr_second, 3),
        "adaptation_signal": adapt,
    }


def _holding_period_distribution(trades: list[dict], positions: list[dict]) -> dict:
    """
    Infer holding period distribution.

    Method 1 (preferred): position open → close timestamps.
    Method 2 (fallback):  DTE at entry as upper bound.
    """
    # Method 1: position open → market end as proxy for hold (position held to resolution)
    hold_days = []
    for r in positions:
        ts_open  = _parse_ts(r.get("timestamp", ""))
        end_date = r.get("end_date", "")
        ts_end   = _parse_ts(end_date)
        if ts_open and ts_end:
            days = (ts_end - ts_open).total_seconds() / 86400.0
            if 0 < days < 3650:
                hold_days.append(days)

    if hold_days:
        hold_days.sort()
        return {
            "method":       "position_entry_to_end_date",
            "n_samples":    len(hold_days),
            "p25_days":     round(_quantile(hold_days, 0.25), 1),
            "median_days":  round(_quantile(hold_days, 0.50), 1),
            "p75_days":     round(_quantile(hold_days, 0.75), 1),
            "p90_days":     round(_quantile(hold_days, 0.90), 1),
            "max_days":     round(hold_days[-1], 1),
        }

    # Method 2: DTE at entry
    dtes = []
    for r in trades:
        ts  = _parse_ts(r.get("timestamp", ""))
        dte = _days_until(ts, r.get("end_date", ""))
        if dte is not None and dte >= 0:
            dtes.append(dte)

    if dtes:
        dtes.sort()
        return {
            "method":              "dte_at_entry_upper_bound",
            "n_samples":           len(dtes),
            "proxy_p25_days":      round(_quantile(dtes, 0.25), 1),
            "proxy_median_days":   round(_quantile(dtes, 0.50), 1),
            "proxy_p75_days":      round(_quantile(dtes, 0.75), 1),
            "proxy_p90_days":      round(_quantile(dtes, 0.90), 1),
            "note": "DTE at entry = upper bound; actual hold likely 20-80% of DTE",
        }

    return {"insufficient_data": True, "n_samples": 0}


def _archetype_clusters(
    trades: list[dict],
    positions: list[dict],
    profile_base: dict,
) -> dict:
    """
    Rules-based classification of trading behavior into 6 archetypes.

    Archetypes:
      event_scalper       — very short DTE, high frequency, small sizes
      longshot_speculator — extreme entry prices (<0.20 or >0.80), small sizes
      mean_reversion      — mid-price entries (0.30-0.70), neutral YES/NO bias
      market_making       — buys both YES+NO on same condition, high frequency
      fundamental_event   — long DTE, larger sizes, concentrated categories
      momentum_trend      — strong directional bias, medium DTE
    """
    dte_stats   = profile_base.get("timing_dte", {})
    size_stats  = profile_base.get("size_stats", {})
    freq        = profile_base.get("frequency", {})
    bias        = profile_base.get("outcome_bias", {})

    median_dte      = float(dte_stats.get("median") or 30.0)
    median_size     = float(size_stats.get("median") or 25.0)
    trades_per_day  = float(freq.get("trades_per_day") or 1.0)
    yes_bias        = float(bias.get("yes_bias") or 0.5)

    all_prices = [_coerce_float(r.get("price")) for r in trades]
    all_prices = [p for p in all_prices if 0.01 < p < 0.99]

    pct_extreme = (len([p for p in all_prices if p < 0.20 or p > 0.80]) /
                   len(all_prices)) if all_prices else 0.0
    pct_mid     = (len([p for p in all_prices if 0.30 <= p <= 0.70]) /
                   len(all_prices)) if all_prices else 0.0

    # Check MM-like: buy both YES+NO on same market
    market_outcomes: dict[str, set] = defaultdict(set)
    for r in trades:
        cid = r.get("condition_id", "")
        out = r.get("outcome", "").lower()
        if cid and out in ("yes", "no"):
            market_outcomes[cid].add(out)
    pct_both_sides = (len([v for v in market_outcomes.values() if len(v) == 2]) /
                      len(market_outcomes)) if market_outcomes else 0.0

    scores: dict[str, float] = {}

    # 1. Event scalper
    s = 0.0
    if median_dte < 3:    s += 0.50
    elif median_dte < 7:  s += 0.25
    if trades_per_day > 5: s += 0.30
    elif trades_per_day > 2: s += 0.15
    if median_size < 30:  s += 0.20
    scores["event_scalper"] = min(1.0, s)

    # 2. Longshot speculator
    s = 0.0
    if pct_extreme > 0.60: s += 0.90
    elif pct_extreme > 0.40: s += 0.60
    elif pct_extreme > 0.25: s += 0.30
    scores["longshot_speculator"] = min(1.0, s)

    # 3. Mean reversion
    s = 0.0
    if pct_mid > 0.60:                  s += 0.40
    elif pct_mid > 0.40:                s += 0.20
    if abs(yes_bias - 0.5) < 0.15:      s += 0.30   # balanced direction
    if 7 <= median_dte <= 60:            s += 0.30
    scores["mean_reversion"] = min(1.0, s)

    # 4. Market making
    s = 0.0
    if pct_both_sides > 0.40:  s += 0.90
    elif pct_both_sides > 0.20: s += 0.55
    elif pct_both_sides > 0.10: s += 0.25
    if trades_per_day > 3:      s += 0.10
    scores["market_making"] = min(1.0, s)

    # 5. Fundamental / event-driven
    s = 0.0
    if median_dte > 60:  s += 0.40
    elif median_dte > 30: s += 0.20
    if median_size > 75: s += 0.30
    elif median_size > 40: s += 0.15
    if trades_per_day < 1.5: s += 0.30
    scores["fundamental_event"] = min(1.0, s)

    # 6. Momentum / trend
    s = 0.0
    if abs(yes_bias - 0.5) > 0.35:  s += 0.40
    elif abs(yes_bias - 0.5) > 0.20: s += 0.20
    if pct_extreme > 0.30:           s += 0.30
    if 3 <= median_dte <= 30:        s += 0.30
    scores["momentum_trend"] = min(1.0, s)

    sorted_archetypes = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    result = []
    for name, score in sorted_archetypes:
        if score >= 0.65:
            conf = "high"
        elif score >= 0.35:
            conf = "medium"
        else:
            conf = "speculative"
        result.append({"name": name, "score": round(score, 3), "confidence": conf})

    primary   = sorted_archetypes[0][0] if sorted_archetypes else "unknown"
    secondary = sorted_archetypes[1][0] if len(sorted_archetypes) > 1 and sorted_archetypes[1][1] > 0.20 else None

    top2 = {k: v for k, v in sorted_archetypes[:2] if v > 0.10}
    total = sum(top2.values())
    weights = {k: round(v / total, 3) for k, v in top2.items()} if total > 0 else {}

    return {
        "archetypes":                    result,
        "primary":                       primary,
        "secondary":                     secondary,
        "allocation_weights":            weights,
        "pct_both_sides_same_market":    round(pct_both_sides, 3),
        "pct_extreme_price_trades":      round(pct_extreme, 3),
        "pct_mid_price_trades":          round(pct_mid, 3),
    }


def _parameter_bands_with_confidence(profile: dict, n_trades: int) -> dict:
    """
    Produce confidence-scored parameter bands for all key clone strategy parameters.

    Confidence levels:
      high       — n_samples >= 50 and parameter is directly observable
      medium     — n_samples >= 20 or weakly observable proxy
      speculative — n_samples < 20 or not directly observable from public data
    """
    ip = profile.get("inferred_params", {})
    ps = profile.get("price_stats", {})
    ss = profile.get("size_stats", {})
    dte = profile.get("timing_dte", {})

    yes_n = int(ps.get("yes", {}).get("count", 0))
    no_n  = int(ps.get("no",  {}).get("count", 0))
    sz_n  = int(ss.get("count", 0))
    dte_n = int(dte.get("count", 0))

    def _conf(n: int, observable: bool = True) -> str:
        if not observable:
            return "speculative"
        if n >= 50:
            return "high"
        if n >= 20:
            return "medium"
        return "speculative"

    bands: dict[str, Any] = {}

    bands["yes_entry_price_range"] = {
        "low":         round(float(ip.get("yes_entry_price_min", 0.05)), 3),
        "high":        round(float(ip.get("yes_entry_price_max", 0.85)), 3),
        "confidence":  _conf(yes_n),
        "n_samples":   yes_n,
        "rationale":   f"p10–p90 of {yes_n} YES entry prices",
    }

    bands["no_entry_price_range"] = {
        "low":         round(float(ip.get("no_entry_price_min", 0.05)), 3),
        "high":        round(float(ip.get("no_entry_price_max", 0.85)), 3),
        "confidence":  _conf(no_n),
        "n_samples":   no_n,
        "rationale":   f"p10–p90 of {no_n} NO entry prices",
    }

    bands["base_size_usdc"] = {
        "point_estimate": round(float(ip.get("base_size_usdc", 25.0)), 2),
        "range_low":      round(float(ip.get("min_size_usdc",  10.0)), 2),
        "range_high":     round(float(ip.get("max_size_usdc", 200.0)), 2),
        "confidence":     _conf(sz_n),
        "n_samples":      sz_n,
        "rationale":      f"Median={ip.get('base_size_usdc', 25):.1f}; range=p25–p90×2 from {sz_n} trades",
    }

    bands["dte_window_days"] = {
        "min_days":    round(float(ip.get("min_dte_days",   0.0)), 1),
        "max_days":    round(float(ip.get("max_dte_days", 365.0)), 1),
        "median_days": round(float(dte.get("median") or 0), 1),
        "confidence":  _conf(dte_n),
        "n_samples":   dte_n,
        "rationale":   f"DTE p25×0.5 to p75×2 from {dte_n} trades with end_date",
    }

    bands["trades_per_day"] = {
        "point_estimate": round(float(ip.get("trades_per_day_target", 1.0)), 2),
        "range_low":      round(float(ip.get("trades_per_day_target", 1.0)) * 0.5, 2),
        "range_high":     round(float(ip.get("trades_per_day_target", 1.0)) * 2.0, 2),
        "confidence":     _conf(n_trades),
        "n_samples":      n_trades,
        "rationale":      "Directly measured from trade history timestamps",
    }

    bands["yes_directional_bias"] = {
        "point_estimate": round(float(ip.get("yes_bias", 0.5)), 3),
        "bias_direction": ip.get("bias_direction", "yes"),
        "bias_strength":  round(float(ip.get("bias_strength", 0.0)), 3),
        "confidence":     _conf(n_trades),
        "n_samples":      n_trades,
        "rationale":      "Directly measured YES count vs total directional trades",
    }

    # These are not directly observable — always speculative
    bands["confidence_threshold"] = {
        "point_estimate": float(ip.get("confidence_floor", 0.30)),
        "range_low":      0.20,
        "range_high":     0.55,
        "confidence":     "speculative",
        "n_samples":      n_trades,
        "rationale":      "Not observable from public data; estimated from trade selectivity",
    }

    bands["edge_floor_bps"] = {
        "point_estimate": float(ip.get("edge_floor_bps", 50.0)),
        "range_low":      25.0,
        "range_high":     150.0,
        "confidence":     "speculative",
        "n_samples":      n_trades,
        "rationale":      "Not observable; estimated from typical spread vs frequency pattern",
    }

    bands["max_concurrent_positions"] = {
        "point_estimate": 3,
        "range_low":      1,
        "range_high":     10,
        "confidence":     "speculative",
        "n_samples":      n_trades,
        "rationale":      "Requires intraday position snapshot data (not available in public history)",
    }

    return bands


def _experiment_matrix(param_bands: dict, archetype: str, n_trades: int) -> list[dict]:
    """
    Generate 5 concrete clone parameter sets ranked by expected fidelity.

    Fidelity estimate = heuristic based on data quality (n_trades) and
    filter tightness.  Higher score = clone behavior more closely tracks target.
    """
    base_size  = float(param_bands.get("base_size_usdc", {}).get("point_estimate", 25.0))
    size_low   = float(param_bands.get("base_size_usdc", {}).get("range_low",  10.0))
    size_high  = float(param_bands.get("base_size_usdc", {}).get("range_high", 200.0))
    yes_lo     = float(param_bands.get("yes_entry_price_range", {}).get("low",  0.05))
    yes_hi     = float(param_bands.get("yes_entry_price_range", {}).get("high", 0.85))
    no_lo      = float(param_bands.get("no_entry_price_range",  {}).get("low",  0.05))
    no_hi      = float(param_bands.get("no_entry_price_range",  {}).get("high", 0.85))
    dte_min    = float(param_bands.get("dte_window_days", {}).get("min_days",    0.0))
    dte_max    = float(param_bands.get("dte_window_days", {}).get("max_days",  365.0))

    data_q = min(1.0, n_trades / 200.0)   # 0-1 data quality factor

    exps: list[dict] = []

    # ── 1. Conservative ──
    exps.append({
        "rank": 1,
        "name": "conservative_clone",
        "expected_fidelity": round(0.75 * data_q + 0.15, 3),
        "params": {
            "CLONE_SCORE_THRESHOLD":     0.55,
            "CLONE_AGGRESSIVENESS":      0.7,
            "CLONE_MAX_OPEN_POSITIONS":  2,
            "CLONE_YES_PRICE_MIN":       round(max(0.01, yes_lo * 1.10), 3),
            "CLONE_YES_PRICE_MAX":       round(min(0.99, yes_hi * 0.90), 3),
            "CLONE_NO_PRICE_MIN":        round(max(0.01, no_lo  * 1.10), 3),
            "CLONE_NO_PRICE_MAX":        round(min(0.99, no_hi  * 0.90), 3),
            "CLONE_BASE_SIZE":           round(base_size * 0.70, 2),
            "CLONE_MIN_SIZE":            round(size_low, 2),
            "CLONE_MAX_SIZE":            round(base_size * 1.20, 2),
        },
        "rationale": (
            "Tightest filter — only highest-confidence matches. "
            "Expect ~50–60% fewer trades vs baseline but highest behavioral accuracy."
        ),
    })

    # ── 2. Baseline ──
    exps.append({
        "rank": 2,
        "name": "baseline_clone",
        "expected_fidelity": round(0.65 * data_q + 0.15, 3),
        "params": {
            "CLONE_SCORE_THRESHOLD":     0.40,
            "CLONE_AGGRESSIVENESS":      1.0,
            "CLONE_MAX_OPEN_POSITIONS":  3,
            "CLONE_YES_PRICE_MIN":       round(yes_lo, 3),
            "CLONE_YES_PRICE_MAX":       round(yes_hi, 3),
            "CLONE_NO_PRICE_MIN":        round(no_lo,  3),
            "CLONE_NO_PRICE_MAX":        round(no_hi,  3),
            "CLONE_BASE_SIZE":           round(base_size, 2),
            "CLONE_MIN_SIZE":            round(size_low,  2),
            "CLONE_MAX_SIZE":            round(size_high, 2),
        },
        "rationale": (
            "Direct mapping of inferred profile parameters. "
            "Best overall balance of fidelity and trade volume."
        ),
    })

    # ── 3. Aggressive ──
    exps.append({
        "rank": 3,
        "name": "aggressive_clone",
        "expected_fidelity": round(0.50 * data_q + 0.10, 3),
        "params": {
            "CLONE_SCORE_THRESHOLD":     0.30,
            "CLONE_AGGRESSIVENESS":      1.30,
            "CLONE_MAX_OPEN_POSITIONS":  5,
            "CLONE_YES_PRICE_MIN":       round(max(0.01, yes_lo * 0.80), 3),
            "CLONE_YES_PRICE_MAX":       round(min(0.99, yes_hi * 1.10), 3),
            "CLONE_NO_PRICE_MIN":        round(max(0.01, no_lo  * 0.80), 3),
            "CLONE_NO_PRICE_MAX":        round(min(0.99, no_hi  * 1.10), 3),
            "CLONE_BASE_SIZE":           round(base_size * 1.30, 2),
            "CLONE_MIN_SIZE":            round(size_low,  2),
            "CLONE_MAX_SIZE":            round(size_high * 1.50, 2),
        },
        "rationale": (
            "Wider filters to capture more candidate trades. "
            "Higher noise but max market exposure."
        ),
    })

    # ── 4. Archetype-tuned ──
    archetype_overrides: dict[str, dict] = {
        "event_scalper":      {"CLONE_SCORE_THRESHOLD": 0.45, "CLONE_MAX_OPEN_POSITIONS": 5, "CLONE_AGGRESSIVENESS": 0.8},
        "fundamental_event":  {"CLONE_SCORE_THRESHOLD": 0.50, "CLONE_MAX_OPEN_POSITIONS": 2, "CLONE_AGGRESSIVENESS": 1.2},
        "mean_reversion":     {"CLONE_SCORE_THRESHOLD": 0.45, "CLONE_MAX_OPEN_POSITIONS": 4, "CLONE_AGGRESSIVENESS": 1.0},
        "market_making":      {"CLONE_SCORE_THRESHOLD": 0.35, "CLONE_MAX_OPEN_POSITIONS": 6, "CLONE_AGGRESSIVENESS": 0.6},
        "longshot_speculator":{"CLONE_SCORE_THRESHOLD": 0.40, "CLONE_MAX_OPEN_POSITIONS": 4, "CLONE_AGGRESSIVENESS": 0.9},
        "momentum_trend":     {"CLONE_SCORE_THRESHOLD": 0.45, "CLONE_MAX_OPEN_POSITIONS": 3, "CLONE_AGGRESSIVENESS": 1.1},
    }
    archetype_rationale: dict[str, str] = {
        "event_scalper":      "Tuned for short-DTE scalping: more concurrent positions, tighter sizes.",
        "fundamental_event":  "Tuned for fundamental research: fewer concurrent, larger sizes.",
        "mean_reversion":     "Tuned for mean-reversion: balanced params, mid-price focus.",
        "market_making":      "Tuned for MM-like: many small positions, low score threshold.",
        "longshot_speculator":"Tuned for longshot bets: moderate threshold, slightly smaller size.",
        "momentum_trend":     "Tuned for directional momentum: standard positions, slight size boost.",
    }
    base_params = exps[1]["params"].copy()   # start from baseline
    overrides = archetype_overrides.get(archetype, {"CLONE_SCORE_THRESHOLD": 0.42,
                                                      "CLONE_MAX_OPEN_POSITIONS": 3,
                                                      "CLONE_AGGRESSIVENESS": 1.0})
    base_params.update(overrides)
    exps.append({
        "rank": 4,
        "name": f"archetype_tuned_{archetype}",
        "expected_fidelity": round(0.70 * data_q + 0.15, 3),
        "params": base_params,
        "rationale": archetype_rationale.get(archetype, "Archetype-specific tuning."),
    })

    # ── 5. High-edge only ──
    exps.append({
        "rank": 5,
        "name": "high_edge_selective",
        "expected_fidelity": round(0.60 * data_q + 0.15, 3),
        "params": {
            "CLONE_SCORE_THRESHOLD":     0.60,
            "CLONE_AGGRESSIVENESS":      1.50,
            "CLONE_MAX_OPEN_POSITIONS":  2,
            "CLONE_YES_PRICE_MIN":       round(yes_lo, 3),
            "CLONE_YES_PRICE_MAX":       round(yes_hi, 3),
            "CLONE_NO_PRICE_MIN":        round(no_lo,  3),
            "CLONE_NO_PRICE_MAX":        round(no_hi,  3),
            "CLONE_BASE_SIZE":           round(base_size * 1.50, 2),
            "CLONE_MIN_SIZE":            round(size_low  * 1.50, 2),
            "CLONE_MAX_SIZE":            round(size_high, 2),
        },
        "rationale": (
            "Very selective (score≥0.60) with outsized position sizes. "
            "Tests whether highest-quality signals warrant extra capital."
        ),
    })

    return exps


# ---------------------------------------------------------------------------
# Core profile computation
# ---------------------------------------------------------------------------

def build_profile(rows: list[dict]) -> dict[str, Any]:
    """Compute full behavioral feature set from normalised CSV rows."""

    trades    = [r for r in rows if r["record_type"] == "trade"]
    positions = [r for r in rows if r["record_type"] == "position"]

    # ── A. Basic counts ──────────────────────────────────────────────────────
    total_trades    = len(trades)
    total_positions = len(positions)

    # ── B. Trade size distribution ───────────────────────────────────────────
    trade_sizes = sorted([_coerce_float(r.get("size_usdc")) for r in trades
                          if _coerce_float(r.get("size_usdc")) > 0])
    size_stats = {
        "count":  len(trade_sizes),
        "min":    trade_sizes[0]  if trade_sizes else 0.0,
        "p25":    _quantile(trade_sizes, 0.25),
        "median": _quantile(trade_sizes, 0.50),
        "p75":    _quantile(trade_sizes, 0.75),
        "p90":    _quantile(trade_sizes, 0.90),
        "max":    trade_sizes[-1] if trade_sizes else 0.0,
        "mean":   _mean(trade_sizes),
        "std":    _std(trade_sizes),
        "total":  sum(trade_sizes),
    }

    # ── C. Entry price distribution ──────────────────────────────────────────
    yes_prices = sorted([_coerce_float(r["price"]) for r in trades
                         if r.get("outcome", "").lower() == "yes" and _coerce_float(r.get("price")) > 0])
    no_prices  = sorted([_coerce_float(r["price"]) for r in trades
                         if r.get("outcome", "").lower() == "no"  and _coerce_float(r.get("price")) > 0])
    all_prices = sorted([_coerce_float(r["price"]) for r in trades if _coerce_float(r.get("price")) > 0])

    def _price_stats(prices: list[float]) -> dict:
        return {
            "count":  len(prices),
            "min":    prices[0]  if prices else 0.0,
            "p25":    _quantile(prices, 0.25),
            "median": _quantile(prices, 0.50),
            "p75":    _quantile(prices, 0.75),
            "max":    prices[-1] if prices else 0.0,
            "mean":   _mean(prices),
        }

    price_stats = {
        "yes": _price_stats(yes_prices),
        "no":  _price_stats(no_prices),
        "all": _price_stats(all_prices),
    }

    # ── D. Outcome bias ───────────────────────────────────────────────────────
    outcome_counts = Counter(r.get("outcome", "").lower() for r in trades)
    yes_count = outcome_counts.get("yes", 0)
    no_count  = outcome_counts.get("no",  0)
    total_dir = yes_count + no_count
    yes_bias  = yes_count / total_dir if total_dir > 0 else 0.5
    side_counts = Counter(r.get("side", "").upper() for r in trades)

    # ── E. Category preferences ───────────────────────────────────────────────
    cat_counts = Counter(r.get("category", "unknown").lower() for r in rows)
    top_categories = [{"category": cat, "count": cnt}
                      for cat, cnt in cat_counts.most_common(10)]

    # ── F. Trade frequency ────────────────────────────────────────────────────
    trade_ts = sorted(filter(None, (_parse_ts(r.get("timestamp", "")) for r in trades)))
    if len(trade_ts) >= 2:
        date_span_days = (trade_ts[-1] - trade_ts[0]).total_seconds() / 86400.0
        trades_per_day = len(trade_ts) / max(date_span_days, 1.0)
        active_days    = len(set(ts.date() for ts in trade_ts))
        first_trade    = trade_ts[0].isoformat()
        last_trade     = trade_ts[-1].isoformat()
    else:
        date_span_days = 0.0
        trades_per_day = 0.0
        active_days    = len(trade_ts)
        first_trade    = trade_ts[0].isoformat() if trade_ts else ""
        last_trade     = first_trade

    # ── G. Days-to-expiry at entry ────────────────────────────────────────────
    dtes = sorted(filter(None, (_days_until(_parse_ts(r.get("timestamp", "")), r.get("end_date", ""))
                                for r in trades)))
    dte_stats = {
        "count":  len(dtes),
        "min":    dtes[0]  if dtes else None,
        "p25":    _quantile(dtes, 0.25),
        "median": _quantile(dtes, 0.50),
        "p75":    _quantile(dtes, 0.75),
        "max":    dtes[-1] if dtes else None,
        "mean":   _mean(dtes),
    }

    # ── H. Win/loss proxy ─────────────────────────────────────────────────────
    pnl_vals = [_coerce_float(r["pnl_usdc"]) for r in positions
                if _coerce_float(r.get("pnl_usdc")) != 0.0]
    wins   = [p for p in pnl_vals if p > 0]
    losses = [p for p in pnl_vals if p < 0]
    total_pnl   = sum(pnl_vals)
    win_rate    = len(wins) / len(pnl_vals) if pnl_vals else None
    avg_win     = _mean(wins)
    avg_loss    = _mean(losses)
    payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0.0 else None

    # ── I. Inferred clone parameters (backward-compat) ───────────────────────
    clone_yes_min = max(0.01, _quantile(yes_prices, 0.10)) if yes_prices else 0.05
    clone_yes_max = min(0.99, _quantile(yes_prices, 0.90)) if yes_prices else 0.80
    clone_no_min  = max(0.01, _quantile(no_prices,  0.10)) if no_prices  else 0.05
    clone_no_max  = min(0.99, _quantile(no_prices,  0.90)) if no_prices  else 0.80

    clone_base_size = max(5.0,   _quantile(trade_sizes, 0.50)) if trade_sizes else 25.0
    clone_min_size  = max(5.0,   _quantile(trade_sizes, 0.25)) if trade_sizes else 10.0
    clone_max_size  = min(500.0, _quantile(trade_sizes, 0.90) * 2.0) if trade_sizes else 200.0

    top_cats = [e["category"] for e in top_categories[:5] if e["category"] != "unknown"]
    clone_min_dte = max(0.0, dte_stats["p25"] * 0.5) if dte_stats["count"] > 0 else 0.0
    clone_max_dte = dte_stats["p75"] * 2.0           if dte_stats["count"] > 0 else 365.0

    bias_direction = "yes" if yes_bias >= 0.5 else "no"
    bias_strength  = abs(yes_bias - 0.5) * 2.0

    inferred_params = {
        "yes_entry_price_min":   round(clone_yes_min, 3),
        "yes_entry_price_max":   round(clone_yes_max, 3),
        "no_entry_price_min":    round(clone_no_min, 3),
        "no_entry_price_max":    round(clone_no_max, 3),
        "base_size_usdc":        round(clone_base_size, 2),
        "min_size_usdc":         round(clone_min_size, 2),
        "max_size_usdc":         round(clone_max_size, 2),
        "preferred_categories":  top_cats,
        "min_dte_days":          round(clone_min_dte, 1),
        "max_dte_days":          round(clone_max_dte, 1),
        "yes_bias":              round(yes_bias, 3),
        "bias_direction":        bias_direction,
        "bias_strength":         round(bias_strength, 3),
        "trades_per_day_target": round(trades_per_day, 2),
        "confidence_floor":      0.30,
        "edge_floor_bps":        50.0,
    }

    # Assemble base profile (backward-compat keys)
    profile_base: dict[str, Any] = {
        "meta": {
            "total_trades":    total_trades,
            "total_positions": total_positions,
            "first_trade":     first_trade,
            "last_trade":      last_trade,
            "date_span_days":  round(date_span_days, 1),
            "active_days":     active_days,
        },
        "size_stats":    size_stats,
        "price_stats":   price_stats,
        "outcome_bias": {
            "yes_count":   yes_count,
            "no_count":    no_count,
            "yes_bias":    round(yes_bias, 3),
            "side_counts": dict(side_counts),
        },
        "top_categories":  top_categories,
        "frequency": {
            "trades_per_day": round(trades_per_day, 3),
            "active_days":    active_days,
            "date_span_days": round(date_span_days, 1),
        },
        "timing_dte":    dte_stats,
        "pnl_proxy": {
            "sample_size":  len(pnl_vals),
            "total_pnl":    round(total_pnl, 2),
            "win_count":    len(wins),
            "loss_count":   len(losses),
            "win_rate":     round(win_rate, 3) if win_rate is not None else None,
            "avg_win":      round(avg_win,  2),
            "avg_loss":     round(avg_loss, 2),
            "payoff_ratio": round(payoff_ratio, 3) if payoff_ratio is not None else None,
        },
        "inferred_params": inferred_params,
    }

    # ── J. Deep analysis (new sections) ──────────────────────────────────────
    log.info("Running deep analysis: temporal patterns …")
    profile_base["temporal_patterns"] = _temporal_patterns(trades)

    log.info("Running deep analysis: market duration buckets …")
    profile_base["market_duration_buckets"] = _market_duration_buckets(trades)

    log.info("Running deep analysis: size scaling …")
    profile_base["size_scaling"] = _size_scaling_analysis(trades)

    log.info("Running deep analysis: streak analysis …")
    profile_base["streak_analysis"] = _streak_analysis(positions)

    log.info("Running deep analysis: holding period …")
    profile_base["holding_period"] = _holding_period_distribution(trades, positions)

    log.info("Running deep analysis: archetype clustering …")
    profile_base["archetype_clusters"] = _archetype_clusters(trades, positions, profile_base)

    log.info("Running deep analysis: parameter bands …")
    profile_base["parameter_bands"] = _parameter_bands_with_confidence(profile_base, total_trades)

    log.info("Running deep analysis: experiment matrix …")
    primary_archetype = profile_base["archetype_clusters"].get("primary", "unknown")
    profile_base["experiment_matrix"] = _experiment_matrix(
        profile_base["parameter_bands"], primary_archetype, total_trades
    )

    return profile_base


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_detailed_json(wallet: str, profile: dict, out_dir: str) -> str:
    """Save the full deep profile as clone_profile_<wallet>_detailed.json."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"clone_profile_{wallet.lower()}_detailed.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, default=str)
    log.info("Detailed profile saved -> %s", path)
    return path


def write_markdown_report(wallet: str, profile: dict, out_dir: str) -> str:
    """Write human-readable markdown analysis report."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"clone_profile_{wallet.lower()}_report.md")

    meta        = profile.get("meta", {})
    ss          = profile.get("size_stats", {})
    bias        = profile.get("outcome_bias", {})
    freq        = profile.get("frequency", {})
    pnl         = profile.get("pnl_proxy", {})
    archetypes  = profile.get("archetype_clusters", {})
    param_bands = profile.get("parameter_bands", {})
    experiment  = profile.get("experiment_matrix", [])
    temporal    = profile.get("temporal_patterns", {})
    dur_buckets = profile.get("market_duration_buckets", {})
    streak      = profile.get("streak_analysis", {})
    hold        = profile.get("holding_period", {})
    size_sc     = profile.get("size_scaling", {})

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        "# Wallet Clone Deep Analysis Report",
        "",
        f"**Target Wallet**: `{wallet}`  ",
        f"**Generated**: {now_str}  ",
        f"**Data range**: {meta.get('first_trade', 'N/A')[:10]} → {meta.get('last_trade', 'N/A')[:10]}"
        f" ({meta.get('date_span_days', 0):.0f} days)",
        "",
        "---",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total trades | {meta.get('total_trades', 0):,} |",
        f"| Total positions | {meta.get('total_positions', 0):,} |",
        f"| Active trading days | {meta.get('active_days', 0)} |",
        f"| Trades per day | {freq.get('trades_per_day', 0):.2f} |",
        f"| Total trade volume | ${ss.get('total', 0):,.2f} USDC |",
        f"| YES bias | {bias.get('yes_bias', 0.5):.1%} ({bias.get('yes_count', 0)} YES / {bias.get('no_count', 0)} NO) |",
        "",
        "---",
        "",
        "## Strategy Archetype Clustering",
        "",
    ]

    if archetypes and not archetypes.get("insufficient_data"):
        primary   = archetypes.get("primary", "unknown")
        secondary = archetypes.get("secondary") or "N/A"
        weights   = archetypes.get("allocation_weights", {})
        lines += [
            f"**Primary archetype**: `{primary}`  ",
            f"**Secondary archetype**: `{secondary}`  ",
            f"**Estimated allocation**: "
            + ", ".join(f"`{k}` {v:.0%}" for k, v in weights.items()),
            "",
            "| Archetype | Score | Confidence |",
            "|-----------|-------|------------|",
        ]
        for a in archetypes.get("archetypes", []):
            conf_icon = {"high": "✅", "medium": "🟡", "speculative": "⚠️"}.get(a["confidence"], "")
            lines.append(f"| {a['name']} | {a['score']:.3f} | {conf_icon} {a['confidence']} |")
        lines += [
            "",
            f"_Supporting metrics_: "
            f"both-sides-same-market={archetypes.get('pct_both_sides_same_market', 0):.1%}, "
            f"extreme-price-trades={archetypes.get('pct_extreme_price_trades', 0):.1%}, "
            f"mid-price-trades={archetypes.get('pct_mid_price_trades', 0):.1%}",
            "",
        ]

    lines += [
        "---",
        "",
        "## Size & Entry Analysis",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| P25 trade size | ${ss.get('p25', 0):.2f} USDC |",
        f"| Median trade size | ${ss.get('median', 0):.2f} USDC |",
        f"| P75 trade size | ${ss.get('p75', 0):.2f} USDC |",
        f"| P90 trade size | ${ss.get('p90', 0):.2f} USDC |",
        f"| Max trade size | ${ss.get('max', 0):.2f} USDC |",
    ]

    if not size_sc.get("insufficient_data"):
        lines += [
            f"| Size scaling direction | {size_sc.get('size_scaling_direction', 'N/A')} |",
            f"| Price-to-size correlation | {size_sc.get('correlation_price_to_size', 0):.3f} |",
            f"| Extremity-to-size correlation | {size_sc.get('correlation_extremity_to_size', 0):.3f} |",
        ]
        low_r = size_sc.get("regime_low_price_lt030", {})
        mid_r = size_sc.get("regime_mid_price_030_070", {})
        hi_r  = size_sc.get("regime_high_price_gt070", {})
        lines += [
            "",
            "**Size by price regime:**",
            "",
            "| Price Range | N trades | Median size |",
            "|-------------|----------|-------------|",
            f"| Low (<0.30) | {low_r.get('count', 0)} | ${low_r.get('median_size', 0):.2f} |",
            f"| Mid (0.30–0.70) | {mid_r.get('count', 0)} | ${mid_r.get('median_size', 0):.2f} |",
            f"| High (>0.70) | {hi_r.get('count', 0)} | ${hi_r.get('median_size', 0):.2f} |",
        ]
    lines.append("")

    lines += [
        "---",
        "",
        "## Temporal Patterns",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Dominant trading session (UTC) | {temporal.get('dominant_session', 'N/A')} |",
        f"| Peak hour (UTC) | {temporal.get('peak_hour', 'N/A')}:00 |",
        f"| Peak day of week | {temporal.get('peak_dow', 'N/A')} |",
        "",
    ]

    sessions = temporal.get("session_distribution", {})
    if sessions:
        total_sess = sum(sessions.values()) or 1
        lines += [
            "**Session breakdown:**",
            "",
            "| Session | Trades | % |",
            "|---------|--------|---|",
        ]
        for sname, cnt in sorted(sessions.items(), key=lambda x: -x[1]):
            lines.append(f"| {sname} | {cnt} | {cnt/total_sess:.1%} |")
        lines.append("")

    lines += [
        "---",
        "",
        "## Market Duration Preference",
        "",
        f"**Dominant bucket**: `{dur_buckets.get('dominant_bucket', 'N/A')}`  ",
        f"**Duration preference label**: `{dur_buckets.get('duration_preference', 'N/A')}`",
        "",
    ]

    pcts = dur_buckets.get("percentages", {})
    counts = dur_buckets.get("counts", {})
    if pcts:
        lines += [
            "| Bucket | Trades | % |",
            "|--------|--------|---|",
        ]
        for bucket in ["intraday_lt1d", "short_1_7d", "medium_7_30d", "long_30_90d", "vlong_gt90d"]:
            cnt = counts.get(bucket, 0)
            pct = pcts.get(bucket, 0)
            lines.append(f"| {bucket} | {cnt} | {pct:.1%} |")
        lines.append(f"| unknown | {counts.get('unknown', 0)} | — |")
        lines.append("")

    lines += [
        "---",
        "",
        "## Holding Period Distribution",
        "",
    ]

    if hold.get("insufficient_data"):
        lines.append("_Insufficient data to estimate holding period._")
    elif hold.get("method") == "position_entry_to_end_date":
        lines += [
            f"_Method_: position entry → market end date (n={hold.get('n_samples', 0)})",
            "",
            "| Percentile | Days |",
            "|-----------|------|",
            f"| P25 | {hold.get('p25_days', 'N/A')} |",
            f"| Median | {hold.get('median_days', 'N/A')} |",
            f"| P75 | {hold.get('p75_days', 'N/A')} |",
            f"| P90 | {hold.get('p90_days', 'N/A')} |",
        ]
    else:
        lines += [
            f"_Method_: DTE at entry (upper bound proxy, n={hold.get('n_samples', 0)})  ",
            f"_{hold.get('note', '')}_",
            "",
            "| Percentile | Days (proxy) |",
            "|-----------|-------------|",
            f"| P25 | {hold.get('proxy_p25_days', 'N/A')} |",
            f"| Median | {hold.get('proxy_median_days', 'N/A')} |",
            f"| P75 | {hold.get('proxy_p75_days', 'N/A')} |",
            f"| P90 | {hold.get('proxy_p90_days', 'N/A')} |",
        ]
    lines.append("")

    lines += [
        "---",
        "",
        "## Win/Loss Behavior",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Win rate (proxy, from position P&L) | {pnl.get('win_rate') or 'N/A'} |",
        f"| Average win | ${pnl.get('avg_win', 0):+.2f} |",
        f"| Average loss | ${pnl.get('avg_loss', 0):+.2f} |",
        f"| Payoff ratio | {pnl.get('payoff_ratio') or 'N/A'} |",
        f"| Total observed P&L | ${pnl.get('total_pnl', 0):+.2f} |",
    ]

    if not streak.get("insufficient_data"):
        lines += [
            f"| Max win streak | {streak.get('max_win_streak', 0)} |",
            f"| Max loss streak | {streak.get('max_loss_streak', 0)} |",
            f"| Avg win streak | {streak.get('avg_win_streak', 0):.1f} |",
            f"| Avg loss streak | {streak.get('avg_loss_streak', 0):.1f} |",
            f"| Adaptation signal | `{streak.get('adaptation_signal', 'N/A')}` "
            f"(win rate: first half {streak.get('win_rate_first_half', 0):.1%} → "
            f"second half {streak.get('win_rate_second_half', 0):.1%}) |",
        ]
    lines.append("")

    lines += [
        "---",
        "",
        "## Confidence-Scored Parameter Bands",
        "",
        "> ✅ high (n≥50, directly observable)  "
        "🟡 medium (n≥20 or weak proxy)  "
        "⚠️ speculative (n<20 or not directly observable)",
        "",
        "| Parameter | Value / Range | Confidence | n |",
        "|-----------|---------------|------------|---|",
    ]

    for param, band in param_bands.items():
        conf = band.get("confidence", "?")
        n    = band.get("n_samples", 0)
        icon = {"high": "✅", "medium": "🟡", "speculative": "⚠️"}.get(conf, "")

        if "low" in band and "high" in band and "point_estimate" not in band:
            val_str = f"{band['low']} – {band['high']}"
        elif "min_days" in band:
            val_str = f"{band['min_days']} – {band['max_days']} days (median {band.get('median_days', '?')})"
        elif "point_estimate" in band:
            lo   = band.get("range_low",  "")
            hi   = band.get("range_high", "")
            val_str = f"{band['point_estimate']} (range: {lo}–{hi})"
        else:
            val_str = str(band.get("point_estimate", "N/A"))

        lines.append(f"| {param} | {val_str} | {icon} {conf} | {n} |")

    lines += [
        "",
        "---",
        "",
        "## Recommended Paper Experiment Matrix",
        "",
        "_Ranked by expected fidelity (higher = clone behavior more closely tracks target)._",
        "",
    ]

    for exp in experiment:
        fid_pct = f"{exp.get('expected_fidelity', 0):.0%}"
        lines += [
            f"### Experiment {exp['rank']}: `{exp['name']}`",
            f"**Expected fidelity**: {fid_pct}  ",
            f"**Rationale**: {exp.get('rationale', '')}",
            "",
            "```powershell",
            "# Set these env vars before running run_clone_paper.ps1",
        ]
        for k, v in exp.get("params", {}).items():
            lines.append(f"$env:{k} = '{v}'")
        lines += [
            f"$env:CLONE_WALLET = '{wallet}'",
            "$env:CLONE_ENABLED = '1'",
            ".\\run_clone_paper.ps1",
            "```",
            "",
        ]

    lines += [
        "---",
        "",
        "## Next Commands",
        "",
        "```powershell",
        "# 1. Run baseline paper clone",
        f"$env:CLONE_WALLET = '{wallet}'",
        "$env:CLONE_ENABLED = '1'",
        ".\\run_clone_paper.ps1",
        "",
        "# 2. Re-extract with longer history (increase --limit)",
        f".\\run_clone_extract.ps1 -Wallet {wallet} -Limit 10000",
        "",
        "# 3. Re-run profiling after re-extraction",
        f".\\run_clone_profile.ps1 -Wallet {wallet}",
        "",
        "# 4. Run backtest comparison",
        f"python -m analytics.clone_backtest --wallet {wallet}",
        "```",
        "",
        "---",
        "*Report generated by `analytics/clone_profile.py` — Polymarket Bot*",
    ]

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log.info("Markdown report saved -> %s", path)
    return path


# ---------------------------------------------------------------------------
# Console summary printer (unchanged from v1, extended with archetypes)
# ---------------------------------------------------------------------------

def print_report(wallet: str, profile: dict) -> None:
    meta  = profile["meta"]
    freq  = profile["frequency"]
    sizes = profile["size_stats"]
    bias  = profile["outcome_bias"]
    pnl   = profile["pnl_proxy"]
    dte   = profile["timing_dte"]
    cats  = profile["top_categories"][:5]
    prm   = profile["inferred_params"]
    arch  = profile.get("archetype_clusters", {})
    bands = profile.get("parameter_bands", {})

    print()
    print("=" * 66)
    print("  WALLET CLONE DEEP PROFILE")
    print(f"  Wallet: {wallet}")
    print("=" * 66)
    print(f"  Trades:         {meta['total_trades']:,}   (positions: {meta['total_positions']:,})")
    print(f"  Date range:     {meta['first_trade'][:10]}  ->  {meta['last_trade'][:10]}  ({meta['date_span_days']:.0f} days)")
    print(f"  Active days:    {meta['active_days']}")
    print(f"  Freq:           {freq['trades_per_day']:.2f} trades/day")
    print()
    print("  — SIZE (USDC) —")
    print(f"    min={sizes['min']:.2f}  p25={sizes['p25']:.2f}  median={sizes['median']:.2f}  "
          f"p75={sizes['p75']:.2f}  max={sizes['max']:.2f}")
    print(f"    mean={sizes['mean']:.2f}  total_volume={sizes['total']:.2f}")
    print()
    print("  — DIRECTIONAL BIAS —")
    print(f"    YES: {bias['yes_count']}  NO: {bias['no_count']}  yes_bias={bias['yes_bias']:.1%}")
    print(f"    side_counts: {bias['side_counts']}")
    print()
    print("  — ENTRY PRICE RANGES —")
    ps = profile["price_stats"]
    print(f"    YES: p25={ps['yes']['p25']:.3f}  median={ps['yes']['median']:.3f}  p75={ps['yes']['p75']:.3f}")
    print(f"    NO:  p25={ps['no']['p25']:.3f}  median={ps['no']['median']:.3f}  p75={ps['no']['p75']:.3f}")
    print()
    print("  — TIMING (days before market end at entry) —")
    if dte["count"] > 0:
        print(f"    min={dte['min']:.1f}  p25={dte['p25']:.1f}  median={dte['median']:.1f}  "
              f"p75={dte['p75']:.1f}  max={dte['max']:.1f}")
    else:
        print("    (no DTE data — end_date not available)")
    print()
    print("  — WIN/LOSS PROXY —")
    if pnl["sample_size"] > 0:
        print(f"    sample={pnl['sample_size']}  win_rate={pnl['win_rate']:.1%}  "
              f"avg_win=+{pnl['avg_win']:.2f}  avg_loss={pnl['avg_loss']:.2f}")
        if pnl["payoff_ratio"]:
            print(f"    payoff_ratio={pnl['payoff_ratio']:.2f}  total_pnl={pnl['total_pnl']:+.2f}")
    else:
        print("    (no P&L data available)")
    print()
    print("  — TOP CATEGORIES —")
    for c in cats:
        print(f"    {c['category']:<30} {c['count']:>4} records")
    print()
    print("  — STRATEGY ARCHETYPE CLUSTERS —")
    if arch and not arch.get("insufficient_data"):
        print(f"    Primary:   {arch.get('primary', 'unknown')}  "
              f"(secondary: {arch.get('secondary', 'N/A')})")
        for a in arch.get("archetypes", [])[:3]:
            print(f"      {a['name']:<25} score={a['score']:.3f}  confidence={a['confidence']}")
    else:
        print("    (insufficient data for archetype clustering)")
    print()
    print("  — CONFIDENCE-SCORED PARAMETER BANDS (key params) —")
    for pname in ["yes_entry_price_range", "no_entry_price_range", "base_size_usdc",
                  "dte_window_days", "trades_per_day"]:
        band = bands.get(pname, {})
        if not band:
            continue
        conf = band.get("confidence", "?")
        n    = band.get("n_samples", 0)
        if "low" in band and "high" in band and "point_estimate" not in band:
            val = f"[{band['low']}, {band['high']}]"
        elif "min_days" in band:
            val = f"[{band['min_days']}, {band['max_days']}] days"
        elif "point_estimate" in band:
            val = f"{band['point_estimate']} ({band.get('range_low','')}–{band.get('range_high','')})"
        else:
            val = str(band.get("point_estimate", "?"))
        print(f"    {pname:<30}  {val:<25}  conf={conf}  n={n}")
    print()
    print("  — INFERRED CLONE PARAMETERS (for wallet_clone.py) —")
    print(f"    yes_entry_price:  [{prm['yes_entry_price_min']:.3f}, {prm['yes_entry_price_max']:.3f}]")
    print(f"    no_entry_price:   [{prm['no_entry_price_min']:.3f}, {prm['no_entry_price_max']:.3f}]")
    print(f"    size_usdc:        base={prm['base_size_usdc']:.2f}  "
          f"min={prm['min_size_usdc']:.2f}  max={prm['max_size_usdc']:.2f}")
    print(f"    preferred_cats:   {prm['preferred_categories']}")
    print(f"    dte_window:       [{prm['min_dte_days']:.1f}, {prm['max_dte_days']:.1f}] days")
    print(f"    bias_direction:   {prm['bias_direction']}  (strength={prm['bias_strength']:.2f})")
    print(f"    trades_per_day:   {prm['trades_per_day_target']:.2f}")
    print("=" * 66)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build deep behavioral profile from extracted wallet data"
    )
    parser.add_argument("--wallet", required=True,
                        help="Wallet address (used to locate CSV if --csv not given)")
    parser.add_argument("--csv",     help="Explicit path to clone_source CSV")
    parser.add_argument("--out-dir", default="logs",
                        help="Directory for output JSON/report (default: logs/)")
    args = parser.parse_args()

    wallet    = args.wallet.lower()
    csv_path  = args.csv or os.path.join(args.out_dir, f"clone_source_{wallet}.csv")

    if not os.path.exists(csv_path):
        print(f"ERROR: CSV not found at {csv_path}")
        print("Run scripts/clone_extract.py first.")
        sys.exit(1)

    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
    log.info("Loaded %d rows from %s", len(rows), csv_path)

    if not rows:
        print("ERROR: CSV is empty — extraction may have found no data.")
        sys.exit(1)

    # Build profile
    profile = build_profile(rows)
    profile["wallet"]     = args.wallet
    profile["source_csv"] = csv_path

    os.makedirs(args.out_dir, exist_ok=True)

    # Save standard profile (backward-compat)
    std_path = os.path.join(args.out_dir, f"clone_profile_{wallet}.json")
    with open(std_path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, default=str)
    log.info("Standard profile saved -> %s", std_path)

    # Save detailed JSON
    det_path = write_detailed_json(args.wallet, profile, args.out_dir)

    # Save markdown report
    md_path  = write_markdown_report(args.wallet, profile, args.out_dir)

    # Print console summary
    print_report(args.wallet, profile)

    print(f"Standard profile: {std_path}")
    print(f"Detailed profile: {det_path}")
    print(f"Markdown report:  {md_path}")
    print(f"Next: python -m analytics.clone_backtest --wallet {args.wallet}")
    print(f"Or run paper clone: $env:CLONE_WALLET='{args.wallet}'; python bot.py")


if __name__ == "__main__":
    main()
