"""
Polymarket Wallet Clone — Replay / Backtest Evaluator
======================================================
Replays historical extracted trade data to evaluate how closely the clone
strategy's inferred rules would have matched the target wallet's actual behavior.

Methodology:
  - For each historical trade by the target wallet, apply the clone scoring
    function to determine if the clone would have triggered.
  - Compare entry prices, sizes, and estimated outcomes.
  - Compute comparison metrics: match rate, size deviation, P&L proxy.

Output:
  logs/clone_backtest_<wallet>.json   — full per-trade comparison
  Printed summary table to stdout.

Usage:
  python -m analytics.clone_backtest --wallet 0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd
  python -m analytics.clone_backtest --wallet 0x... --threshold 0.35
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
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
# Scoring helpers (mirrors wallet_clone.py logic, offline)
# ---------------------------------------------------------------------------

def _price_score(price: float, lo: float, hi: float) -> float:
    if lo >= hi:
        return 0.5
    if price < lo or price > hi:
        dist = min(abs(price - lo), abs(price - hi))
        return max(0.0, 0.5 - dist * 2.0)
    mid = (lo + hi) / 2.0
    dist_from_mid = abs(price - mid) / ((hi - lo) / 2.0)
    return 1.0 - 0.3 * dist_from_mid


def _timing_score(entry_ts: Optional[datetime], end_date: str, min_dte: float, max_dte: float) -> tuple[float, Optional[float]]:
    if not entry_ts or not end_date:
        return 0.5, None
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                end_dt = datetime.strptime(end_date, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return 0.5, None
        dte = (end_dt - entry_ts).total_seconds() / 86400.0
        if dte < 0:
            return 0.0, dte
        if min_dte >= max_dte:
            return 0.5, dte
        if dte < min_dte or dte > max_dte:
            dist = min(abs(dte - min_dte), abs(dte - max_dte))
            return max(0.0, 0.5 - dist / max(max_dte - min_dte, 1.0)), dte
        mid = (min_dte + max_dte) / 2.0
        dist = abs(dte - mid) / ((max_dte - min_dte) / 2.0)
        return 1.0 - 0.3 * dist, dte
    except Exception:
        return 0.5, None


def _category_score(question: str, preferred_categories: list[str]) -> float:
    if not preferred_categories:
        return 0.5
    q_lower = question.lower()
    for cat in preferred_categories:
        if cat and cat in q_lower:
            return 1.0
    return 0.1


def _bias_score(outcome: str, bias_direction: str, bias_strength: float) -> float:
    match = (outcome.lower() == bias_direction.lower())
    if match:
        return 0.5 + 0.5 * bias_strength
    return 0.5 - 0.5 * bias_strength


def _parse_ts(ts: str) -> Optional[datetime]:
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


def _coerce_float(v: Any) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Per-trade scoring
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    row_idx: int
    timestamp: str
    condition_id: str
    question: str
    outcome: str
    actual_price: float
    actual_size_usdc: float
    actual_pnl: float

    # Clone decision
    clone_score: float
    clone_would_trade: bool
    clone_size_usdc: float
    clone_price_score: float
    clone_timing_score: float
    clone_category_score: float
    clone_bias_score: float
    dte_days: Optional[float]

    # Outcome comparison (PnL proxy)
    pnl_proxy: float          # estimate based on outcome field vs position value
    size_delta_pct: float     # (clone_size - actual_size) / actual_size


def score_trade(row: dict, profile: dict, threshold: float, aggressiveness: float) -> TradeResult:
    """Apply clone scoring logic to a single historical trade row."""
    p = profile.get("inferred_params", {})

    price      = _coerce_float(row.get("price"))
    size_usdc  = _coerce_float(row.get("size_usdc"))
    outcome    = str(row.get("outcome", "")).strip()
    question   = str(row.get("question", ""))
    timestamp  = str(row.get("timestamp", ""))
    end_date   = str(row.get("end_date", ""))
    actual_pnl = _coerce_float(row.get("pnl_usdc"))
    condition_id = str(row.get("condition_id", ""))

    # Price score
    if outcome.lower() == "yes":
        lo = float(p.get("yes_entry_price_min", 0.05))
        hi = float(p.get("yes_entry_price_max", 0.85))
    else:
        lo = float(p.get("no_entry_price_min", 0.05))
        hi = float(p.get("no_entry_price_max", 0.85))
    price_s = _price_score(price, lo, hi) if price > 0 else 0.5

    # Timing score
    entry_ts = _parse_ts(timestamp)
    timing_s, dte = _timing_score(
        entry_ts, end_date,
        float(p.get("min_dte_days", 0.0)),
        float(p.get("max_dte_days", 365.0)),
    )

    # Category score
    preferred_cats = list(p.get("preferred_categories", []))
    cat_s = _category_score(question, preferred_cats)

    # Bias score
    bias_direction = str(p.get("bias_direction", "yes"))
    bias_strength  = float(p.get("bias_strength", 0.0))
    bias_s = _bias_score(outcome, bias_direction, bias_strength)

    # Composite
    score = cat_s * 0.35 + price_s * 0.35 + timing_s * 0.20 + bias_s * 0.10
    would_trade = score >= threshold

    # Clone size estimate
    base_size = float(p.get("base_size_usdc", 25.0)) * aggressiveness
    clone_size = max(float(p.get("min_size_usdc", 10.0)),
                     min(float(p.get("max_size_usdc", 200.0)), base_size))

    # Size delta vs actual
    size_delta_pct = (clone_size - size_usdc) / size_usdc if size_usdc > 0 else 0.0

    # PnL proxy: if the trade had a recorded PnL, use it directly.
    # Otherwise approximate based on price and outcome.
    pnl_proxy = actual_pnl  # already in USD from position data

    return TradeResult(
        row_idx=0,
        timestamp=timestamp,
        condition_id=condition_id,
        question=question[:60],
        outcome=outcome,
        actual_price=price,
        actual_size_usdc=size_usdc,
        actual_pnl=actual_pnl,
        clone_score=round(score, 4),
        clone_would_trade=would_trade,
        clone_size_usdc=round(clone_size, 2),
        clone_price_score=round(price_s, 4),
        clone_timing_score=round(timing_s, 4),
        clone_category_score=round(cat_s, 4),
        clone_bias_score=round(bias_s, 4),
        dte_days=round(dte, 2) if dte is not None else None,
        pnl_proxy=round(pnl_proxy, 4),
        size_delta_pct=round(size_delta_pct, 4),
    )


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def build_summary(results: list[TradeResult], threshold: float) -> dict:
    total = len(results)
    matched = [r for r in results if r.clone_would_trade]
    not_matched = [r for r in results if not r.clone_would_trade]

    match_rate = len(matched) / total if total > 0 else 0.0

    actual_sizes  = [r.actual_size_usdc for r in results if r.actual_size_usdc > 0]
    clone_sizes   = [r.clone_size_usdc  for r in matched  if r.clone_size_usdc  > 0]
    size_deltas   = [r.size_delta_pct   for r in matched]
    scores_all    = [r.clone_score      for r in results]
    scores_match  = [r.clone_score      for r in matched]

    pnl_all     = [r.pnl_proxy for r in results     if r.pnl_proxy != 0]
    pnl_matched = [r.pnl_proxy for r in matched     if r.pnl_proxy != 0]

    # Turnover
    actual_turnover = sum(actual_sizes)
    clone_turnover  = sum(clone_sizes)

    # Hit rate proxy (% of matched trades with positive pnl_proxy)
    pos_pnl_matched = [p for p in pnl_matched if p > 0]
    neg_pnl_matched = [p for p in pnl_matched if p < 0]
    hit_rate_proxy  = len(pos_pnl_matched) / len(pnl_matched) if pnl_matched else None

    # Hold time proxy: average DTE at entry for matched trades
    dtes = [r.dte_days for r in matched if r.dte_days is not None]

    return {
        "threshold":            threshold,
        "total_records":        total,
        "matched_count":        len(matched),
        "not_matched_count":    len(not_matched),
        "match_rate":           round(match_rate, 4),
        "score_stats": {
            "all_mean":         round(_mean(scores_all),  4),
            "all_std":          round(_std(scores_all),   4),
            "matched_mean":     round(_mean(scores_match),4),
        },
        "size_comparison": {
            "actual_median":    round(sorted(actual_sizes)[len(actual_sizes)//2], 2) if actual_sizes else 0,
            "clone_median":     round(sorted(clone_sizes)[len(clone_sizes)//2],   2) if clone_sizes  else 0,
            "actual_total_vol": round(actual_turnover, 2),
            "clone_total_vol":  round(clone_turnover,  2),
            "avg_size_delta":   round(_mean(size_deltas), 4),
        },
        "pnl_proxy": {
            "target_total_pnl":  round(sum(pnl_all),      2),
            "clone_total_pnl":   round(sum(pnl_matched),  2),
            "target_hit_rate":   None,   # not enough data to compute without resolution
            "clone_hit_rate":    round(hit_rate_proxy, 4) if hit_rate_proxy is not None else None,
            "clone_avg_win":     round(_mean(pos_pnl_matched), 4) if pos_pnl_matched else 0,
            "clone_avg_loss":    round(_mean(neg_pnl_matched), 4) if neg_pnl_matched else 0,
        },
        "timing_proxy": {
            "avg_dte_days_matched": round(_mean(dtes), 2) if dtes else None,
            "dte_sample_size":      len(dtes),
        },
    }


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(wallet: str, summary: dict, results: list[TradeResult]) -> None:
    s = summary
    sz = s["size_comparison"]
    pnl = s["pnl_proxy"]
    dte = s["timing_proxy"]
    scores = s["score_stats"]

    print()
    print("=" * 62)
    print("  WALLET CLONE — BACKTEST REPORT")
    print(f"  Target wallet: {wallet}")
    print(f"  Score threshold: {s['threshold']:.2f}")
    print("=" * 62)
    print(f"  Historical records:  {s['total_records']:,}")
    print(f"  Clone would trade:   {s['matched_count']:,} ({s['match_rate']:.1%} match rate)")
    print(f"  Clone would skip:    {s['not_matched_count']:,}")
    print()
    print("  — SCORE DISTRIBUTION —")
    print(f"    All records:  mean={scores['all_mean']:.3f}  std={scores['all_std']:.3f}")
    print(f"    Matched only: mean={scores['matched_mean']:.3f}")
    print()
    print("  — SIZE COMPARISON —")
    print(f"    Target median size: ${sz['actual_median']:.2f} USDC")
    print(f"    Clone  median size: ${sz['clone_median']:.2f} USDC")
    print(f"    Target total vol:   ${sz['actual_total_vol']:,.2f} USDC")
    print(f"    Clone  total vol:   ${sz['clone_total_vol']:,.2f} USDC  (matched trades only)")
    print(f"    Avg size delta:     {sz['avg_size_delta']:+.1%}")
    print()
    print("  — P&L PROXY (positions w/ recorded P&L) —")
    print(f"    Target total P&L:   ${pnl['target_total_pnl']:+,.2f}")
    print(f"    Clone total P&L:    ${pnl['clone_total_pnl']:+,.2f}  (matched trades)")
    if pnl["clone_hit_rate"] is not None:
        print(f"    Clone hit rate:     {pnl['clone_hit_rate']:.1%}")
        print(f"    Clone avg win:      ${pnl['clone_avg_win']:+.4f}")
        print(f"    Clone avg loss:     ${pnl['clone_avg_loss']:+.4f}")
    print()
    print("  — TIMING —")
    if dte["avg_dte_days_matched"] is not None:
        print(f"    Avg DTE at entry (matched): {dte['avg_dte_days_matched']:.1f} days  (n={dte['dte_sample_size']})")
    else:
        print("    (no DTE data available)")
    print()

    # Top 5 matched trades by score
    top = sorted([r for r in results if r.clone_would_trade], key=lambda r: r.clone_score, reverse=True)[:5]
    if top:
        print("  — TOP 5 HIGHEST-SCORING MATCHES —")
        for r in top:
            print(f"    [{r.clone_score:.2f}] {r.outcome:3s} @ {r.actual_price:.3f}  "
                  f"${r.actual_size_usdc:.0f}→clone${r.clone_size_usdc:.0f}  "
                  f"{r.question[:40]}")
    print("=" * 62)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Replay clone strategy logic against historical wallet data")
    parser.add_argument("--wallet", required=True, help="Wallet address (0x...)")
    parser.add_argument("--csv", help="Explicit path to clone_source CSV")
    parser.add_argument("--profile", help="Explicit path to clone_profile JSON")
    parser.add_argument("--out-dir", default="logs", help="Output directory (default: logs/)")
    parser.add_argument("--threshold", type=float, default=0.40, help="Clone score threshold (default 0.40)")
    parser.add_argument("--aggressiveness", type=float, default=1.0, help="Clone size multiplier (default 1.0)")
    args = parser.parse_args()

    wallet = args.wallet.lower()
    csv_path     = args.csv     or os.path.join(args.out_dir, f"clone_source_{wallet}.csv")
    profile_path = args.profile or os.path.join(args.out_dir, f"clone_profile_{wallet}.json")

    for path, label in [(csv_path, "CSV"), (profile_path, "Profile JSON")]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found: {path}")
            print("Run scripts/clone_extract.py → analytics/clone_profile.py first.")
            sys.exit(1)

    # Load CSV
    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    log.info("Loaded %d rows from %s", len(rows), csv_path)

    # Load profile
    with open(profile_path, encoding="utf-8") as fh:
        profile = json.load(fh)
    log.info("Loaded profile from %s", profile_path)

    # Filter to trades only (backtest against actual trades)
    trade_rows = [r for r in rows if r.get("record_type") == "trade"]
    log.info("Scoring %d trade records …", len(trade_rows))

    if not trade_rows:
        print("No trade records found — backtest requires trade records.")
        print("Re-run extraction with trades enabled (--only-trades or default mode).")
        sys.exit(1)

    # Score each trade
    results: list[TradeResult] = []
    for i, row in enumerate(trade_rows):
        r = score_trade(row, profile, args.threshold, args.aggressiveness)
        r.row_idx = i
        results.append(r)

    # Build summary
    summary = build_summary(results, args.threshold)

    # Save JSON
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"clone_backtest_{wallet}.json")
    output = {
        "wallet":  args.wallet,
        "summary": summary,
        "per_trade": [
            {
                "idx":              r.row_idx,
                "timestamp":        r.timestamp,
                "condition_id":     r.condition_id,
                "question":         r.question,
                "outcome":          r.outcome,
                "actual_price":     r.actual_price,
                "actual_size_usdc": r.actual_size_usdc,
                "clone_score":      r.clone_score,
                "clone_would_trade": r.clone_would_trade,
                "clone_size_usdc":  r.clone_size_usdc,
                "dte_days":         r.dte_days,
                "pnl_proxy":        r.pnl_proxy,
                "size_delta_pct":   r.size_delta_pct,
            }
            for r in results
        ],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)
    log.info("Backtest saved -> %s", out_path)

    # Print report
    print_report(args.wallet, summary, results)
    print(f"Backtest saved: {out_path}")


if __name__ == "__main__":
    main()
