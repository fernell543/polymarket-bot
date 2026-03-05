"""
Polymarket Wallet Clone — Behavior Profiling
=============================================
Reads the extracted CSV produced by scripts/clone_extract.py and computes
behavioral features that characterise the target wallet's trading style.

Feature extraction:
  - Trade frequency (trades/day, active days, burst patterns)
  - Size distribution (min/p25/median/p75/max USDC per trade)
  - Entry price distribution (typical YES/NO entry price ranges)
  - YES vs NO preference (directional bias)
  - Market category preferences
  - Timing (days-before-market-end at entry)
  - Win/loss proxy (from position P&L if available)
  - Hold-time proxy (inferred from position end dates vs entry)

Output:
  logs/clone_profile_<wallet>.json  — full feature set + inferred params
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
    # Unix timestamp?
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
    return delta if delta >= 0 else None  # negative = already expired at entry


# ---------------------------------------------------------------------------
# Profile computation
# ---------------------------------------------------------------------------

def build_profile(rows: list[dict]) -> dict[str, Any]:
    """Compute all behavioral features from normalised CSV rows."""

    # Split by record type
    trades    = [r for r in rows if r["record_type"] == "trade"]
    positions = [r for r in rows if r["record_type"] == "position"]

    # -----------------------------------------------------------------------
    # A. Basic counts
    # -----------------------------------------------------------------------
    total_trades     = len(trades)
    total_positions  = len(positions)

    # -----------------------------------------------------------------------
    # B. Trade size distribution (USDC)
    # -----------------------------------------------------------------------
    trade_sizes = sorted([float(r["size_usdc"]) for r in trades if float(r.get("size_usdc") or 0) > 0])
    size_stats = {
        "count":   len(trade_sizes),
        "min":     trade_sizes[0]  if trade_sizes else 0.0,
        "p25":     _quantile(trade_sizes, 0.25),
        "median":  _quantile(trade_sizes, 0.50),
        "p75":     _quantile(trade_sizes, 0.75),
        "p90":     _quantile(trade_sizes, 0.90),
        "max":     trade_sizes[-1] if trade_sizes else 0.0,
        "mean":    _mean(trade_sizes),
        "std":     _std(trade_sizes),
        "total":   sum(trade_sizes),
    }

    # -----------------------------------------------------------------------
    # C. Entry price distribution (trades only, split by outcome)
    # -----------------------------------------------------------------------
    yes_prices = sorted([float(r["price"]) for r in trades
                         if r.get("outcome", "").lower() == "yes" and float(r.get("price") or 0) > 0])
    no_prices  = sorted([float(r["price"]) for r in trades
                         if r.get("outcome", "").lower() == "no"  and float(r.get("price") or 0) > 0])
    all_prices = sorted([float(r["price"]) for r in trades if float(r.get("price") or 0) > 0])

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

    # -----------------------------------------------------------------------
    # D. Outcome bias (YES vs NO preference)
    # -----------------------------------------------------------------------
    outcome_counts = Counter(r.get("outcome", "").lower() for r in trades)
    yes_count = outcome_counts.get("yes", 0)
    no_count  = outcome_counts.get("no", 0)
    total_directional = yes_count + no_count
    yes_bias = yes_count / total_directional if total_directional > 0 else 0.5

    # Buy vs Sell breakdown
    side_counts = Counter(r.get("side", "").upper() for r in trades)

    # -----------------------------------------------------------------------
    # E. Market category preferences
    # -----------------------------------------------------------------------
    cat_counts = Counter(r.get("category", "unknown").lower() for r in rows)
    top_categories = [{"category": cat, "count": cnt}
                      for cat, cnt in cat_counts.most_common(10)]

    # -----------------------------------------------------------------------
    # F. Trade frequency and timing
    # -----------------------------------------------------------------------
    trade_timestamps = [_parse_ts(r.get("timestamp", "")) for r in trades]
    trade_timestamps = [ts for ts in trade_timestamps if ts is not None]
    trade_timestamps.sort()

    if len(trade_timestamps) >= 2:
        date_span_days = (trade_timestamps[-1] - trade_timestamps[0]).total_seconds() / 86400.0
        trades_per_day = len(trade_timestamps) / max(date_span_days, 1.0)
        active_days    = len(set(ts.date() for ts in trade_timestamps))
        first_trade    = trade_timestamps[0].isoformat()
        last_trade     = trade_timestamps[-1].isoformat()
    else:
        date_span_days = 0.0
        trades_per_day = 0.0
        active_days    = len(trade_timestamps)
        first_trade    = trade_timestamps[0].isoformat() if trade_timestamps else ""
        last_trade     = first_trade

    # -----------------------------------------------------------------------
    # G. Days-to-expiry at entry (timing analysis)
    # -----------------------------------------------------------------------
    days_to_expiry = []
    for r in trades:
        ts = _parse_ts(r.get("timestamp", ""))
        dte = _days_until(ts, r.get("end_date", ""))
        if dte is not None:
            days_to_expiry.append(dte)

    days_to_expiry_sorted = sorted(days_to_expiry)
    dte_stats = {
        "count":  len(days_to_expiry_sorted),
        "min":    days_to_expiry_sorted[0]  if days_to_expiry_sorted else None,
        "p25":    _quantile(days_to_expiry_sorted, 0.25),
        "median": _quantile(days_to_expiry_sorted, 0.50),
        "p75":    _quantile(days_to_expiry_sorted, 0.75),
        "max":    days_to_expiry_sorted[-1] if days_to_expiry_sorted else None,
        "mean":   _mean(days_to_expiry_sorted),
    }

    # -----------------------------------------------------------------------
    # H. Win/loss proxy from position P&L data
    # -----------------------------------------------------------------------
    pnl_values = [float(r["pnl_usdc"]) for r in positions if r.get("pnl_usdc") and float(r.get("pnl_usdc") or 0) != 0.0]
    wins  = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p < 0]
    total_pnl = sum(pnl_values)

    win_rate_proxy = len(wins) / len(pnl_values) if pnl_values else None
    avg_win  = _mean(wins)  if wins   else 0.0
    avg_loss = _mean(losses) if losses else 0.0
    payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0.0 else None

    # -----------------------------------------------------------------------
    # I. Inferred trading rules / parameters (for clone strategy use)
    # -----------------------------------------------------------------------
    # Typical entry price range (where the wallet tends to enter YES positions)
    if yes_prices:
        clone_yes_min_price = max(0.01, _quantile(yes_prices, 0.10))
        clone_yes_max_price = min(0.99, _quantile(yes_prices, 0.90))
    else:
        clone_yes_min_price = 0.05
        clone_yes_max_price = 0.80

    if no_prices:
        clone_no_min_price = max(0.01, _quantile(no_prices, 0.10))
        clone_no_max_price = min(0.99, _quantile(no_prices, 0.90))
    else:
        clone_no_min_price = 0.05
        clone_no_max_price = 0.80

    # Size policy: use median + scale by confidence
    clone_base_size   = max(5.0, size_stats["median"])
    clone_min_size    = max(5.0, size_stats["p25"])
    clone_max_size    = min(500.0, size_stats["p90"] * 2.0)

    # Category filter: only trade in the top 3 category groups
    top_cats = [e["category"] for e in top_categories[:5] if e["category"] != "unknown"]

    # Timing: enter markets within the target's typical DTE window
    if dte_stats["count"] > 0:
        clone_min_dte = max(0.0, dte_stats["p25"] * 0.5)
        clone_max_dte = dte_stats["p75"] * 2.0
    else:
        clone_min_dte = 0.0
        clone_max_dte = 365.0

    # YES/NO directional bias
    bias_direction = "yes" if yes_bias >= 0.5 else "no"
    bias_strength  = abs(yes_bias - 0.5) * 2.0   # 0=neutral, 1=fully biased

    inferred_params = {
        "yes_entry_price_min":    round(clone_yes_min_price, 3),
        "yes_entry_price_max":    round(clone_yes_max_price, 3),
        "no_entry_price_min":     round(clone_no_min_price, 3),
        "no_entry_price_max":     round(clone_no_max_price, 3),
        "base_size_usdc":         round(clone_base_size, 2),
        "min_size_usdc":          round(clone_min_size, 2),
        "max_size_usdc":          round(clone_max_size, 2),
        "preferred_categories":   top_cats,
        "min_dte_days":           round(clone_min_dte, 1),
        "max_dte_days":           round(clone_max_dte, 1),
        "yes_bias":               round(yes_bias, 3),
        "bias_direction":         bias_direction,
        "bias_strength":          round(bias_strength, 3),
        "trades_per_day_target":  round(trades_per_day, 2),
        # Confidence floor: lower if wallet trades low-confidence markets (wide price spread)
        "confidence_floor":       0.30,
        "edge_floor_bps":         50.0,
    }

    return {
        "meta": {
            "total_trades":    total_trades,
            "total_positions": total_positions,
            "first_trade":     first_trade,
            "last_trade":      last_trade,
            "date_span_days":  round(date_span_days, 1),
            "active_days":     active_days,
        },
        "size_stats":         size_stats,
        "price_stats":        price_stats,
        "outcome_bias": {
            "yes_count":  yes_count,
            "no_count":   no_count,
            "yes_bias":   round(yes_bias, 3),
            "side_counts": dict(side_counts),
        },
        "top_categories":     top_categories,
        "frequency": {
            "trades_per_day": round(trades_per_day, 3),
            "active_days":    active_days,
            "date_span_days": round(date_span_days, 1),
        },
        "timing_dte":         dte_stats,
        "pnl_proxy": {
            "sample_size":    len(pnl_values),
            "total_pnl":      round(total_pnl, 2),
            "win_count":      len(wins),
            "loss_count":     len(losses),
            "win_rate":       round(win_rate_proxy, 3) if win_rate_proxy is not None else None,
            "avg_win":        round(avg_win, 2),
            "avg_loss":       round(avg_loss, 2),
            "payoff_ratio":   round(payoff_ratio, 3) if payoff_ratio is not None else None,
        },
        "inferred_params":    inferred_params,
    }


# ---------------------------------------------------------------------------
# Report printer
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

    print()
    print("=" * 62)
    print("  WALLET CLONE PROFILE")
    print(f"  Wallet: {wallet}")
    print("=" * 62)
    print(f"  Trades:         {meta['total_trades']:,}   (positions: {meta['total_positions']:,})")
    print(f"  Date range:     {meta['first_trade'][:10]}  →  {meta['last_trade'][:10]}  ({meta['date_span_days']:.0f} days)")
    print(f"  Active days:    {meta['active_days']}")
    print(f"  Freq:           {freq['trades_per_day']:.2f} trades/day")
    print()
    print("  — SIZE (USDC) —")
    print(f"    min={sizes['min']:.2f}  p25={sizes['p25']:.2f}  median={sizes['median']:.2f}  p75={sizes['p75']:.2f}  max={sizes['max']:.2f}")
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
        print(f"    min={dte['min']:.1f}  p25={dte['p25']:.1f}  median={dte['median']:.1f}  p75={dte['p75']:.1f}  max={dte['max']:.1f}")
    else:
        print("    (no DTE data — end_date not available)")
    print()
    print("  — WIN/LOSS PROXY (from position P&L) —")
    if pnl["sample_size"] > 0:
        print(f"    sample={pnl['sample_size']}  win_rate={pnl['win_rate']:.1%}  avg_win=+{pnl['avg_win']:.2f}  avg_loss={pnl['avg_loss']:.2f}")
        if pnl["payoff_ratio"]:
            print(f"    payoff_ratio={pnl['payoff_ratio']:.2f}  total_pnl={pnl['total_pnl']:+.2f}")
    else:
        print("    (no P&L data available)")
    print()
    print("  — TOP CATEGORIES —")
    for c in cats:
        print(f"    {c['category']:<30} {c['count']:>4} records")
    print()
    print("  — INFERRED CLONE PARAMETERS —")
    print(f"    yes_entry_price:  [{prm['yes_entry_price_min']:.3f}, {prm['yes_entry_price_max']:.3f}]")
    print(f"    no_entry_price:   [{prm['no_entry_price_min']:.3f}, {prm['no_entry_price_max']:.3f}]")
    print(f"    size_usdc:        base={prm['base_size_usdc']:.2f}  min={prm['min_size_usdc']:.2f}  max={prm['max_size_usdc']:.2f}")
    print(f"    preferred_cats:   {prm['preferred_categories']}")
    print(f"    dte_window:       [{prm['min_dte_days']:.1f}, {prm['max_dte_days']:.1f}] days")
    print(f"    bias_direction:   {prm['bias_direction']}  (strength={prm['bias_strength']:.2f})")
    print(f"    trades_per_day:   {prm['trades_per_day_target']:.2f}")
    print("=" * 62)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build behavioral profile from extracted wallet data")
    parser.add_argument("--wallet", required=True, help="Wallet address (used to locate CSV if --csv not given)")
    parser.add_argument("--csv", help="Explicit path to clone_source CSV")
    parser.add_argument("--out-dir", default="logs", help="Directory for output JSON (default: logs/)")
    args = parser.parse_args()

    wallet = args.wallet.lower()
    csv_path = args.csv or os.path.join(args.out_dir, f"clone_source_{wallet}.csv")

    if not os.path.exists(csv_path):
        print(f"ERROR: CSV not found at {csv_path}")
        print("Run scripts/clone_extract.py first.")
        sys.exit(1)

    # Load CSV
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
    profile["wallet"] = args.wallet
    profile["source_csv"] = csv_path

    # Save
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"clone_profile_{wallet}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, default=str)
    log.info("Profile saved -> %s", out_path)

    # Print report
    print_report(args.wallet, profile)

    print(f"Profile saved: {out_path}")
    print(f"Next: python -m analytics.clone_backtest --wallet {args.wallet}")
    print(f"Or run paper clone: $env:CLONE_WALLET='{args.wallet}'; python bot.py")


if __name__ == "__main__":
    main()
