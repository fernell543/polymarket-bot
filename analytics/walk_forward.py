"""
Walk-forward validation for paper-mode trade logs.
====================================================
Splits logs/quant_trades.csv into chronological windows and measures
consistency of key metrics (win rate, avg edge, net PnL, Sharpe-like
ratio) across windows to surface overfitting and instability risks.

Outputs:
  - Per-window stats table
  - Train (first 70%) vs validation (last 30%) comparison
  - Overfit ratio: train_sharpe / val_sharpe  (>2 = high overfit risk)
  - Stability score: 1 − (std_win_sharpe / mean_win_sharpe) ∈ [0..1]
  - Regime drift: did regime distribution shift across windows?

Usage:
    python -m analytics.walk_forward
    python -m analytics.walk_forward --windows 6 --csv logs/quant_trades.csv
    python -m analytics.walk_forward --fee 0.02 --slip 0.002
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter
from pathlib import Path

DEFAULT_CSV     = "logs/quant_trades.csv"
DEFAULT_FEE     = 0.02
DEFAULT_SLIP    = 0.002
DEFAULT_WINDOWS = 5
MIN_WINDOW_SIZE = 5   # fewer than this → window skipped


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
    running = peak = max_dd = 0.0
    for p in pnl_series:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Window stats
# ---------------------------------------------------------------------------

def _window_stats(
    records: list[dict],
    fee: float,
    slip: float,
    label: str,
) -> dict:
    """Compute performance metrics for a list of trade records."""
    pnl_series: list[float] = []
    edges: list[float] = []
    regimes: list[str] = []
    wins = losses = skipped = 0

    for r in records:
        exp_edge = _safe_float(r.get("expected_edge", ""))
        rea_edge = _safe_float(r.get("realized_edge", ""))
        size     = _safe_float(r.get("size_usdc", ""))
        regime   = r.get("regime", "unknown") or "unknown"

        if size <= 0:
            skipped += 1
            continue

        edge_used = rea_edge if rea_edge != 0.0 else exp_edge
        edges.append(exp_edge)
        regimes.append(regime)

        net = edge_used * size - fee * size - slip * size
        pnl_series.append(net)
        if net >= 0:
            wins += 1
        else:
            losses += 1

    n = len(pnl_series)
    if n == 0:
        return {"label": label, "n": 0, "valid": False}

    total_pnl   = sum(pnl_series)
    mean_edge   = sum(edges) / n
    std_edge    = _stdev(edges)
    sharpe      = mean_edge / std_edge if std_edge > 0 else 0.0
    win_rate    = wins / n * 100
    max_dd      = _max_drawdown(pnl_series)
    regime_dist = Counter(regimes)

    return {
        "label":      label,
        "n":          n,
        "valid":      True,
        "win_rate":   round(win_rate, 1),
        "mean_edge":  round(mean_edge, 5),
        "std_edge":   round(std_edge, 5),
        "sharpe":     round(sharpe, 4),
        "total_pnl":  round(total_pnl, 4),
        "max_dd":     round(max_dd, 4),
        "regime_dist": dict(regime_dist),
    }


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

def report(
    path:       str   = DEFAULT_CSV,
    n_windows:  int   = DEFAULT_WINDOWS,
    fee:        float = DEFAULT_FEE,
    slip:       float = DEFAULT_SLIP,
    verbose:    bool  = False,
) -> None:
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] Trade log not found: {path}")
        print("        Run the bot first with QUANT_MODE_ENABLED=1 DRY_RUN=1.")
        sys.exit(1)

    with open(p, newline="", encoding="utf-8") as f:
        all_records = list(csv.DictReader(f))

    if not all_records:
        print("No trades found.")
        return

    # Sort by timestamp (ISO8601 — lexicographic sort is correct)
    try:
        all_records.sort(key=lambda r: r.get("timestamp_utc", ""))
    except Exception:
        pass   # if sort fails, use original order

    total = len(all_records)
    print(f"\nWALK-FORWARD VALIDATION — {total} trades, {n_windows} windows")
    print(f"Fee={fee*100:.1f}%  Slip={slip*100:.2f}%  Source={path}")

    # -------------------------------------------------------------------
    # Per-window stats
    # -------------------------------------------------------------------

    SEP = "=" * 72

    window_size = total // n_windows
    if window_size < MIN_WINDOW_SIZE:
        print(
            f"[WARN] Too few trades for {n_windows} windows "
            f"(need >= {MIN_WINDOW_SIZE * n_windows}). "
            f"Reduce --windows or collect more data."
        )
        n_windows = max(1, total // MIN_WINDOW_SIZE)
        window_size = total // n_windows
        print(f"       Reducing to {n_windows} windows ({window_size} trades each).")

    window_stats: list[dict] = []
    print(SEP)
    print(f"  {'WIN':>4}  {'N':>5}  {'WR%':>5}  {'AVG_EDGE':>8}  "
          f"{'SHARPE':>6}  {'PNL':>8}  {'MAX_DD':>8}  {'DOMINANT_REGIME':>16}")
    print("  " + "-" * 66)

    for i in range(n_windows):
        start = i * window_size
        end   = start + window_size if i < n_windows - 1 else total
        chunk = all_records[start:end]
        label = f"W{i+1:02d}"
        st    = _window_stats(chunk, fee, slip, label)
        window_stats.append(st)

        if not st["valid"]:
            print(f"  {label:>4}  {'—':>5}")
            continue

        # Dominant regime
        rd = st.get("regime_dist", {})
        dom_regime = max(rd, key=rd.get) if rd else "?"
        print(
            f"  {label:>4}  {st['n']:>5}  {st['win_rate']:>5.1f}  "
            f"{st['mean_edge']:>8.4f}  {st['sharpe']:>6.3f}  "
            f"${st['total_pnl']:>7.2f}  ${st['max_dd']:>7.2f}  "
            f"{dom_regime:>16}"
        )

    # -------------------------------------------------------------------
    # Cross-window stability
    # -------------------------------------------------------------------

    valid_windows = [st for st in window_stats if st.get("valid")]
    sharpes  = [st["sharpe"]   for st in valid_windows]
    win_rates = [st["win_rate"] for st in valid_windows]
    pnls     = [st["total_pnl"] for st in valid_windows]

    mean_sharpe   = sum(sharpes)  / len(sharpes)  if sharpes   else 0.0
    std_sharpe    = _stdev(sharpes)
    mean_wr       = sum(win_rates) / len(win_rates) if win_rates else 0.0
    std_wr        = _stdev(win_rates)
    mean_pnl      = sum(pnls) / len(pnls) if pnls else 0.0

    stability = (
        1.0 - min(1.0, std_sharpe / abs(mean_sharpe))
        if mean_sharpe != 0.0 and len(sharpes) >= 2
        else 0.0
    )

    # -------------------------------------------------------------------
    # Train vs Validation split
    # -------------------------------------------------------------------

    train_end   = int(total * 0.70)
    train_recs  = all_records[:train_end]
    val_recs    = all_records[train_end:]

    train_st = _window_stats(train_recs, fee, slip, "TRAIN (70%)")
    val_st   = _window_stats(val_recs,   fee, slip, "VAL   (30%)")

    overfit_ratio = (
        train_st["sharpe"] / val_st["sharpe"]
        if val_st.get("valid") and val_st["sharpe"] > 0.0
        else float("inf")
    )
    if not train_st.get("valid"):
        overfit_ratio = float("nan")

    # -------------------------------------------------------------------
    # Regime drift
    # -------------------------------------------------------------------

    if n_windows >= 2 and valid_windows:
        first_rd = valid_windows[0].get("regime_dist", {})
        last_rd  = valid_windows[-1].get("regime_dist", {})
        first_dom = max(first_rd, key=first_rd.get) if first_rd else "?"
        last_dom  = max(last_rd, key=last_rd.get) if last_rd else "?"
        regime_drift_flag = first_dom != last_dom
    else:
        first_dom = last_dom = "?"
        regime_drift_flag = False

    # -------------------------------------------------------------------
    # Print summary
    # -------------------------------------------------------------------

    print(SEP)
    print("  CROSS-WINDOW STABILITY")
    print(SEP)
    print(f"  Windows analysed   : {len(valid_windows)}/{n_windows}")
    print(f"  Mean sharpe-like   : {mean_sharpe:+.4f}  (std={std_sharpe:.4f})")
    print(f"  Mean win rate      : {mean_wr:.1f}%  (std={std_wr:.1f}pp)")
    print(f"  Mean net PnL/window: ${mean_pnl:+.2f}")
    print(f"  Stability score    : {stability:.3f}  "
          f"({'GOOD ≥0.7' if stability >= 0.7 else 'MODERATE 0.4–0.7' if stability >= 0.4 else 'LOW <0.4'})")
    print()

    print("  TRAIN vs VALIDATION SPLIT")
    print(SEP)
    for st in (train_st, val_st):
        if not st.get("valid"):
            print(f"  {st['label']:12s}  insufficient data")
            continue
        print(
            f"  {st['label']:12s}  n={st['n']:4d}  wr={st['win_rate']:4.1f}%  "
            f"sharpe={st['sharpe']:+.4f}  pnl=${st['total_pnl']:+.2f}  "
            f"dd=${st['max_dd']:.2f}"
        )

    print()
    if math.isnan(overfit_ratio) or math.isinf(overfit_ratio):
        print("  Overfit ratio      : n/a (insufficient validation data)")
    else:
        risk_label = (
            "LOW (<2)"
            if overfit_ratio < 2.0
            else ("MODERATE (2-3)" if overfit_ratio < 3.0 else "HIGH (>3) ⚠")
        )
        print(f"  Overfit ratio      : {overfit_ratio:.2f}  [{risk_label}]")

    drift_msg = (
        f"DETECTED ({first_dom} → {last_dom}) ⚠"
        if regime_drift_flag
        else f"none ({first_dom} stable)"
    )
    print(f"  Regime drift       : {drift_msg}")
    print()

    # Recommendations
    print(SEP)
    print("  RECOMMENDATIONS")
    print(SEP)
    if not train_st.get("valid") or not val_st.get("valid"):
        print("  • Collect more paper trades (need ≥50 total for reliable analysis).")
    else:
        if overfit_ratio > 2.0:
            print("  • High overfit risk: tighten SIGNAL_CONFIDENCE_THRESHOLD or EXEC_MIN_EDGE.")
        if stability < 0.4:
            print("  • Low stability: strategy edge may be regime-dependent; consider PARAM_PROFILE=auto.")
        if val_st["sharpe"] < 0.3:
            print("  • Val Sharpe < 0.3: edge may not be real; do not go live until it improves.")
        if stability >= 0.7 and overfit_ratio < 2.0 and val_st["sharpe"] >= 0.3:
            print("  • Metrics look consistent. Run param_optimizer to find optimal thresholds.")
        if regime_drift_flag:
            print(f"  • Regime shifted from {first_dom} to {last_dom}. "
                  "Ensure PARAM_PROFILE=auto to adapt.")
    print(SEP)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Polymarket bot — walk-forward validation"
    )
    parser.add_argument("--csv",     default=DEFAULT_CSV)
    parser.add_argument("--windows", type=int,   default=DEFAULT_WINDOWS)
    parser.add_argument("--fee",     type=float, default=DEFAULT_FEE)
    parser.add_argument("--slip",    type=float, default=DEFAULT_SLIP)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    report(
        path=args.csv,
        n_windows=args.windows,
        fee=args.fee,
        slip=args.slip,
        verbose=args.verbose,
    )
