"""
Execution latency and fill-quality monitor.
============================================
Reads logs/quant_trades.csv and computes per-trade execution quality
metrics to surface:

  - Fill ratio      : fraction of placed orders that were filled (not cancelled)
  - Cancel ratio    : fraction cancelled (timed out or manually cancelled)
  - Avg submit latency (ms)  — from spread_at_entry / submit_latency_ms fields
  - Adverse selection proxy  : (expected_edge − realized_edge) per trade
                               Positive = we got worse than expected (adverse)
  - Per order-type breakdown : LIMIT vs MARKET

These metrics supplement the performance report and help identify whether
losses stem from signal quality vs execution quality.

Usage:
    python -m analytics.fill_quality
    python -m analytics.fill_quality --csv logs/quant_trades.csv
    python -m analytics.fill_quality -v   # verbose: per-trade rows
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_CSV = "logs/quant_trades.csv"

# Adverse selection threshold: flag if mean adverse selection > this fraction
ADVERSE_FLAG_THRESHOLD = 0.005   # 0.5%


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


def _pct(num: int, denom: int) -> str:
    if denom == 0:
        return "n/a"
    return f"{num/denom*100:.1f}%"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(
    path:    str  = DEFAULT_CSV,
    verbose: bool = False,
) -> None:
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] Trade log not found: {path}")
        sys.exit(1)

    with open(p, newline="", encoding="utf-8") as f:
        all_records = list(csv.DictReader(f))

    if not all_records:
        print("No trades found.")
        return

    # -------------------------------------------------------------------
    # Accumulators
    # -------------------------------------------------------------------

    total = 0
    filled = 0
    cancelled = 0
    other = 0   # dry_run / unknown

    latencies: list[float] = []
    adverse_samples: list[float] = []   # exp_edge - rea_edge when rea != 0

    by_type: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "filled": 0, "cancelled": 0,
        "latencies": [], "adverse": [],
    })

    verbose_rows = []

    for r in all_records:
        status      = (r.get("status", "") or "").lower()
        order_type  = (r.get("order_type", "LIMIT") or "LIMIT").upper()
        dry_run_val = r.get("dry_run", "1")
        is_dry      = str(dry_run_val) in ("1", "True", "true")

        exp_edge    = _safe_float(r.get("expected_edge", ""))
        rea_edge    = _safe_float(r.get("realized_edge", ""))
        latency_ms  = _safe_float(r.get("submit_latency_ms", ""))
        spread_val  = _safe_float(r.get("spread_at_entry", ""))

        total += 1
        d = by_type[order_type]
        d["n"] += 1

        # Fill / cancel categorisation
        if is_dry or status in ("filled", "dry_run"):
            filled += 1
            d["filled"] += 1
        elif status in ("cancelled", "timed_out", "expired"):
            cancelled += 1
            d["cancelled"] += 1
        else:
            other += 1

        # Latency
        if latency_ms > 0:
            latencies.append(latency_ms)
            d["latencies"].append(latency_ms)

        # Adverse selection: only when realized edge is known
        if rea_edge != 0.0:
            adv = exp_edge - rea_edge   # positive = worse than expected
            adverse_samples.append(adv)
            d["adverse"].append(adv)

        if verbose:
            verbose_rows.append({
                "ts":         r.get("timestamp_utc", "")[:19],
                "type":       order_type,
                "status":     status or ("dry_run" if is_dry else "?"),
                "exp_edge":   f"{exp_edge:+.4f}",
                "rea_edge":   f"{rea_edge:+.4f}" if rea_edge != 0.0 else "n/a",
                "adv":        f"{exp_edge - rea_edge:+.4f}" if rea_edge != 0.0 else "n/a",
                "lat_ms":     f"{latency_ms:.0f}" if latency_ms > 0 else "n/a",
                "spread":     f"{spread_val:.4f}" if spread_val > 0 else "n/a",
            })

    # -------------------------------------------------------------------
    # Derived metrics
    # -------------------------------------------------------------------

    mean_lat      = sum(latencies) / len(latencies)     if latencies else 0.0
    std_lat       = _stdev(latencies)
    mean_adverse  = sum(adverse_samples) / len(adverse_samples) if adverse_samples else 0.0
    std_adverse   = _stdev(adverse_samples)

    # -------------------------------------------------------------------
    # Print
    # -------------------------------------------------------------------

    SEP = "=" * 62

    print(SEP)
    print("  FILL QUALITY REPORT")
    print(SEP)
    print(f"  Log file          : {path}")
    print(f"  Total orders      : {total}")
    print()

    print(f"  Filled            : {filled:4d}  {_pct(filled, total)}")
    print(f"  Cancelled         : {cancelled:4d}  {_pct(cancelled, total)}")
    print(f"  Other/dry-run     : {other:4d}  {_pct(other, total)}")
    print()

    if latencies:
        print(f"  Submit latency    : avg={mean_lat:.1f}ms  std={std_lat:.1f}ms  "
              f"n={len(latencies)}")
    else:
        print("  Submit latency    : n/a  "
              "(add submit_latency_ms to TradeRecord for timing data)")
    print()

    if adverse_samples:
        flag = " ⚠ ADVERSE SELECTION DETECTED" if mean_adverse > ADVERSE_FLAG_THRESHOLD else ""
        print(f"  Adverse selection : avg={mean_adverse:+.5f}  std={std_adverse:.5f}"
              f"  n={len(adverse_samples)}{flag}")
        print(f"    (positive = realized edge < expected; "
              f"persistent >+0.5% suggests fill-quality issue)")
    else:
        print("  Adverse selection : n/a  "
              "(back-fill realized_edge via trade_logger.update_realized_edge)")
    print()

    # Per order-type breakdown
    if len(by_type) > 1 or by_type:
        print("  By order type:")
        for otype, d in sorted(by_type.items()):
            n_t   = d["n"]
            f_t   = d["filled"]
            c_t   = d["cancelled"]
            lats  = d["latencies"]
            advs  = d["adverse"]
            lat_s = f"{sum(lats)/len(lats):.1f}ms" if lats else "n/a"
            adv_s = f"{sum(advs)/len(advs):+.5f}" if advs else "n/a"
            print(
                f"    {otype:<8s}  n={n_t:4d}  "
                f"fill={_pct(f_t, n_t)}  cancel={_pct(c_t, n_t)}  "
                f"lat={lat_s}  adv_sel={adv_s}"
            )
        print()

    # Verbose: per-trade table
    if verbose and verbose_rows:
        print(f"  {'TS':19s}  {'TYPE':7s}  {'STATUS':10s}  "
              f"{'EXP':8s}  {'REA':8s}  {'ADV':8s}  {'LAT_MS':7s}  {'SPREAD':7s}")
        print("  " + "-" * 80)
        for row in verbose_rows[-50:]:   # last 50 trades
            print(
                f"  {row['ts']:19s}  {row['type']:7s}  {row['status']:10s}  "
                f"{row['exp_edge']:8s}  {row['rea_edge']:8s}  {row['adv']:8s}  "
                f"{row['lat_ms']:7s}  {row['spread']:7s}"
            )
        print()

    # Recommendations
    print(SEP)
    print("  RECOMMENDATIONS")
    print(SEP)
    if cancelled > 0 and cancelled / total > 0.25:
        print(f"  • Cancel rate {_pct(cancelled, total)} is high.")
        print("    → Lower EXEC_ORDER_TIMEOUT_SECS or use tighter limit prices.")
    if adverse_samples and mean_adverse > ADVERSE_FLAG_THRESHOLD:
        print(f"  • Mean adverse selection {mean_adverse:+.4f} exceeds {ADVERSE_FLAG_THRESHOLD}.")
        print("    → Consider raising EXEC_MIN_EDGE or lowering EXEC_TAKER_CONFIDENCE_THRESHOLD.")
    if latencies and mean_lat > 2000:
        print(f"  • High avg submit latency ({mean_lat:.0f}ms).")
        print("    → Check network / CLOB_HOST connectivity; consider co-location.")
    if not adverse_samples and not latencies:
        print("  • Limited fill-quality data. Back-fill realized_edge in the CSV")
        print("    and ensure submit_latency_ms is populated by execution_manager.")
    print(SEP)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Polymarket bot — fill quality report"
    )
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-trade rows (last 50)")
    args = parser.parse_args()
    report(path=args.csv, verbose=args.verbose)
