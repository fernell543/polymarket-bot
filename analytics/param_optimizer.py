"""
Parameter optimization harness for paper-mode tuning.
=======================================================
Replays the quant trade log (logs/quant_trades.csv) through every
parameter configuration in a grid (or random sample), scoring each
config by:

    objective = total_net_pnl  −  DD_PENALTY × max_drawdown

where total_net_pnl is computed from:
    net = expected_edge × size_usdc × vol_scale − fee × size_usdc − slip × size_usdc

after filtering to trades that satisfy the config's thresholds.

Parameters searched:
  signal_confidence_threshold  — minimum confidence score to enter
  exec_min_edge                 — minimum expected net edge
  vol_target_scale              — multiplier applied to size_usdc
  mm_spread                     — half-spread gate (trades with smaller spread skipped)

Output:
  logs/opt_results.csv  — all configs ranked by objective
  logs/opt_best.json    — top-3 configs in JSON

Usage:
    python -m analytics.param_optimizer
    python -m analytics.param_optimizer --mode random --n-samples 500
    python -m analytics.param_optimizer --csv logs/quant_trades.csv --output logs/opt_results.csv
    python -m analytics.param_optimizer --dd-penalty 1.0
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

DEFAULT_CSV    = "logs/quant_trades.csv"
DEFAULT_OUT    = "logs/opt_results.csv"
DEFAULT_BEST   = "logs/opt_best.json"
DEFAULT_FEE    = 0.02
DEFAULT_SLIP   = 0.002
DEFAULT_DD_PEN = 0.5   # drawdown penalty coefficient


# ---------------------------------------------------------------------------
# Grid definition
# ---------------------------------------------------------------------------

PARAM_GRID: dict[str, list[float]] = {
    "signal_confidence_threshold": [0.30, 0.40, 0.50, 0.60, 0.70],
    "exec_min_edge":               [0.005, 0.010, 0.015, 0.020, 0.030],
    "vol_target_scale":            [0.50, 1.00, 1.50, 2.00],
    "mm_spread_gate":              [0.00, 0.005, 0.010, 0.020],
}

PARAM_RANGES: dict[str, tuple[float, float]] = {
    "signal_confidence_threshold": (0.20, 0.80),
    "exec_min_edge":               (0.002, 0.040),
    "vol_target_scale":            (0.25, 3.00),
    "mm_spread_gate":              (0.00, 0.030),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _stdev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))


def _max_drawdown(pnl_series: list[float]) -> float:
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_series:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Data loader
# ---------------------------------------------------------------------------

def load_records(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] Trade log not found: {path}")
        print("        Run the bot first with QUANT_MODE_ENABLED=1 DRY_RUN=1.")
        sys.exit(1)
    with open(p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("[ERROR] Trade log is empty.")
        sys.exit(1)
    return rows


# ---------------------------------------------------------------------------
# Score one config
# ---------------------------------------------------------------------------

def score_config(
    records:     list[dict],
    params:      dict[str, float],
    fee:         float,
    slip:        float,
    dd_penalty:  float,
) -> dict[str, Any] | None:
    """
    Filter records by param thresholds and compute performance metrics.
    Returns None when fewer than 5 trades pass the filter.
    """
    conf_thresh = params["signal_confidence_threshold"]
    min_edge    = params["exec_min_edge"]
    vol_scale   = params["vol_target_scale"]
    spread_gate = params["mm_spread_gate"]

    pnl_series: list[float] = []
    edges: list[float] = []

    for r in records:
        confidence = _safe_float(r.get("signal_confidence", ""))
        exp_edge   = _safe_float(r.get("expected_edge", ""))
        size       = _safe_float(r.get("size_usdc", ""))
        # Spread proxy: micro_score inversion isn't clean, skip spread gate
        # when spread_at_entry is absent (legacy logs).
        spread     = _safe_float(r.get("spread_at_entry", ""))

        if confidence < conf_thresh:
            continue
        if exp_edge < min_edge:
            continue
        if spread_gate > 0 and 0 < spread < spread_gate:
            continue   # market too tight — spread gate skips thin-spread entries
        if size <= 0:
            continue

        scaled_size = size * vol_scale
        net = exp_edge * scaled_size - fee * scaled_size - slip * scaled_size
        pnl_series.append(net)
        edges.append(exp_edge)

    n = len(pnl_series)
    if n < 5:
        return None

    total_pnl = sum(pnl_series)
    max_dd    = _max_drawdown(pnl_series)
    mean_edge = sum(edges) / n
    std_edge  = _stdev(edges)
    sharpe    = mean_edge / std_edge if std_edge > 0 else 0.0
    win_rate  = sum(1 for p in pnl_series if p > 0) / n * 100

    objective = total_pnl - dd_penalty * max_dd

    return {
        # Params
        "signal_confidence_threshold": round(conf_thresh, 3),
        "exec_min_edge":               round(min_edge, 4),
        "vol_target_scale":            round(vol_scale, 3),
        "mm_spread_gate":              round(spread_gate, 4),
        # Metrics
        "n_trades":       n,
        "selection_rate": round(n / len(records), 3),
        "win_rate_pct":   round(win_rate, 1),
        "mean_edge":      round(mean_edge, 5),
        "sharpe_like":    round(sharpe, 4),
        "total_net_pnl":  round(total_pnl, 4),
        "max_drawdown":   round(max_dd, 4),
        "objective":      round(objective, 4),
    }


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def grid_search(
    records:    list[dict],
    fee:        float,
    slip:       float,
    dd_penalty: float,
) -> list[dict]:
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    results = []
    total = 1
    for v in values:
        total *= len(v)
    print(f"Grid search: {total} configs …")

    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        result = score_config(records, params, fee, slip, dd_penalty)
        if result:
            results.append(result)

    return results


# ---------------------------------------------------------------------------
# Random search
# ---------------------------------------------------------------------------

def random_search(
    records:    list[dict],
    n_samples:  int,
    fee:        float,
    slip:       float,
    dd_penalty: float,
    seed:       int = 42,
) -> list[dict]:
    rng = random.Random(seed)
    results = []
    print(f"Random search: {n_samples} samples …")
    for _ in range(n_samples):
        params = {
            k: rng.uniform(lo, hi)
            for k, (lo, hi) in PARAM_RANGES.items()
        }
        result = score_config(records, params, fee, slip, dd_penalty)
        if result:
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(results: list[dict], out_csv: str, out_json: str) -> None:
    if not results:
        print("[WARN] No valid configs found (not enough trades per config).")
        print("       Collect more paper trades before optimizing.")
        return

    # Rank by objective descending
    results.sort(key=lambda r: r["objective"], reverse=True)

    # CSV
    p = Path(out_csv)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(f"Results written: {out_csv}  ({len(results)} configs)")

    # JSON (top 3)
    top3 = results[:3]
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(top3, f, indent=2)
    print(f"Top-3 written:   {out_json}")

    # Console summary
    SEP = "=" * 72
    print(SEP)
    print("  PARAMETER OPTIMIZER — TOP 10 CONFIGS")
    print(SEP)
    hdr = (
        f"  {'rank':>4}  {'conf':>5}  {'min_edge':>8}  {'vol_scale':>9}  "
        f"{'n':>5}  {'wr%':>5}  {'sharpe':>6}  {'pnl':>8}  {'dd':>8}  {'obj':>8}"
    )
    print(hdr)
    print("  " + "-" * 68)
    for i, r in enumerate(results[:10], 1):
        print(
            f"  {i:>4}  {r['signal_confidence_threshold']:>5.2f}  "
            f"{r['exec_min_edge']:>8.4f}  {r['vol_target_scale']:>9.2f}  "
            f"{r['n_trades']:>5}  {r['win_rate_pct']:>5.1f}  "
            f"{r['sharpe_like']:>6.3f}  ${r['total_net_pnl']:>7.2f}  "
            f"${r['max_drawdown']:>7.2f}  ${r['objective']:>7.2f}"
        )
    print(SEP)
    best = results[0]
    print(
        f"\n  BEST CONFIG:\n"
        f"    SIGNAL_CONFIDENCE_THRESHOLD={best['signal_confidence_threshold']}\n"
        f"    EXEC_MIN_EDGE={best['exec_min_edge']}\n"
        f"    VOL_TARGET_SCALE={best['vol_target_scale']}   "
        f"(multiply RISK_TARGET_VOL by this)\n"
        f"    MM_SPREAD_GATE={best['mm_spread_gate']}\n"
        f"    → {best['n_trades']} trades, "
        f"wr={best['win_rate_pct']}%, sharpe={best['sharpe_like']}, "
        f"pnl=${best['total_net_pnl']}"
    )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Polymarket bot — parameter optimizer"
    )
    parser.add_argument("--csv",        default=DEFAULT_CSV,   help="Input trade log CSV")
    parser.add_argument("--output",     default=DEFAULT_OUT,   help="Output results CSV")
    parser.add_argument("--best-json",  default=DEFAULT_BEST,  help="Output top-3 JSON")
    parser.add_argument("--mode",       default="grid",
                        choices=["grid", "random"], help="Search mode (default: grid)")
    parser.add_argument("--n-samples",  type=int, default=500,
                        help="Number of random samples (random mode only)")
    parser.add_argument("--fee",        type=float, default=DEFAULT_FEE)
    parser.add_argument("--slip",       type=float, default=DEFAULT_SLIP)
    parser.add_argument("--dd-penalty", type=float, default=DEFAULT_DD_PEN,
                        help="Drawdown penalty coefficient (default 0.5)")
    parser.add_argument("--seed",       type=int,   default=42)
    args = parser.parse_args()

    records = load_records(args.csv)
    print(f"Loaded {len(records)} trades from {args.csv}")

    if args.mode == "grid":
        results = grid_search(records, args.fee, args.slip, args.dd_penalty)
    else:
        results = random_search(
            records, args.n_samples, args.fee, args.slip, args.dd_penalty, args.seed
        )

    write_output(results, args.output, args.best_json)
