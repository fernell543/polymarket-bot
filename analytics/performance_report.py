"""
Quant performance report.
==========================
Reads logs/quant_trades.csv and prints a concise summary:

  - Total trades and win rate
  - Average expected and realized edge
  - Net PnL estimate (after Polymarket fee + slippage assumption)
  - Max intra-session drawdown
  - Sharpe-like ratio (mean_edge / std_edge)
  - Per-regime breakdown
  - Per-strategy breakdown

Usage:
    python -m analytics.performance_report
    python -m analytics.performance_report --csv path/to/quant_trades.csv
    python -m analytics.performance_report --fee 0.02 --slip 0.002
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_CSV  = "logs/quant_trades.csv"
DEFAULT_FEE  = 0.02    # Polymarket: 2% of winnings
DEFAULT_SLIP = 0.002   # assumed slippage: 0.2%


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var  = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_records(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] Trade log not found: {path}")
        print("        Run the bot first with DRY_RUN=1 to generate data.")
        sys.exit(1)

    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(
    path:     str   = DEFAULT_CSV,
    fee:      float = DEFAULT_FEE,
    slip:     float = DEFAULT_SLIP,
    verbose:  bool  = False,
) -> None:
    records = load_records(path)
    if not records:
        print("No trades found in the log.")
        return

    total = len(records)
    wins  = 0
    losses = 0
    skipped = 0

    exp_edges:  list[float] = []
    rea_edges:  list[float] = []
    net_pnl_series: list[float] = []

    running_pnl = 0.0
    peak_pnl    = 0.0
    max_dd      = 0.0

    # Per-regime  and per-strategy accumulators
    by_regime:    dict[str, dict] = defaultdict(
        lambda: {"w": 0, "l": 0, "edges": [], "pnl": 0.0}
    )
    by_strategy:  dict[str, dict] = defaultdict(
        lambda: {"count": 0, "edges": [], "pnl": 0.0}
    )

    for r in records:
        try:
            exp_edge  = _safe_float(r.get("expected_edge", ""))
            rea_edge  = _safe_float(r.get("realized_edge", ""))
            size      = _safe_float(r.get("size_usdc", ""))
            regime    = r.get("regime", "unknown") or "unknown"
            strategy  = r.get("strategy", "unknown") or "unknown"
        except Exception:
            skipped += 1
            continue

        if size <= 0:
            skipped += 1
            continue

        # Use realized edge when available, fall back to expected
        edge_used = rea_edge if rea_edge != 0.0 else exp_edge
        exp_edges.append(exp_edge)
        if rea_edge != 0.0:
            rea_edges.append(rea_edge)

        # Net P&L per trade (edge × size − fees − slippage)
        net = edge_used * size - fee * size - slip * size

        if net >= 0:
            wins += 1
            by_regime[regime]["w"] += 1
        else:
            losses += 1
            by_regime[regime]["l"] += 1

        running_pnl += net
        net_pnl_series.append(net)
        if running_pnl > peak_pnl:
            peak_pnl = running_pnl
        dd = peak_pnl - running_pnl
        if dd > max_dd:
            max_dd = dd

        by_regime[regime]["edges"].append(edge_used)
        by_regime[regime]["pnl"]  += net
        by_strategy[strategy]["count"]  += 1
        by_strategy[strategy]["edges"].append(edge_used)
        by_strategy[strategy]["pnl"]    += net

    # Derived statistics
    win_rate   = wins / total * 100 if total else 0.0
    avg_exp    = sum(exp_edges) / len(exp_edges) if exp_edges else 0.0
    avg_rea    = sum(rea_edges) / len(rea_edges) if rea_edges else 0.0
    std_edge   = _stdev(exp_edges)
    sharpe     = avg_exp / std_edge if std_edge > 0 else 0.0

    # -------------------------------------------------------------------
    # Print
    # -------------------------------------------------------------------
    SEP = "=" * 62

    print(SEP)
    print("  QUANT PERFORMANCE REPORT")
    print(SEP)
    print(f"  Log file       : {path}")
    print(f"  Fee assumption : {fee*100:.1f}%   Slippage: {slip*100:.2f}%")
    print(SEP)
    print(f"  Total trades   : {total}")
    if skipped:
        print(f"  Skipped rows   : {skipped}  (parse errors / zero-size)")
    print(f"  Win rate       : {win_rate:.1f}%  ({wins}W / {losses}L)")
    print()
    print(f"  Avg exp edge   : {avg_exp:+.4f}  (pre-trade estimate)")
    avg_rea_str = f"{avg_rea:+.4f}" if rea_edges else "n/a (no realized data)"
    print(f"  Avg rea edge   : {avg_rea_str}")
    print(f"  Sharpe-like    : {sharpe:.3f}")
    print()
    print(
        f"  Net PnL (est.) : ${running_pnl:+.2f}  "
        f"(after {fee*100:.0f}% fee + {slip*100:.1f}% slippage)"
    )
    print(f"  Max drawdown   : ${max_dd:.2f}")
    print()

    if by_regime:
        print("  By regime:")
        for reg, d in sorted(by_regime.items()):
            t   = d["w"] + d["l"]
            wr  = d["w"] / t * 100 if t else 0.0
            avg = sum(d["edges"]) / len(d["edges"]) if d["edges"] else 0.0
            pnl = d["pnl"]
            print(
                f"    {reg:<14s}  {t:4d} trades  "
                f"wr={wr:4.0f}%  avg_edge={avg:+.4f}  pnl=${pnl:+.2f}"
            )
        print()

    if by_strategy:
        print("  By strategy:")
        for strat, d in sorted(by_strategy.items()):
            t   = d["count"]
            avg = sum(d["edges"]) / len(d["edges"]) if d["edges"] else 0.0
            pnl = d["pnl"]
            print(
                f"    {strat:<22s}  {t:4d} trades  "
                f"avg_edge={avg:+.4f}  pnl=${pnl:+.2f}"
            )
        print()

    if verbose and net_pnl_series:
        print("  Trade P&L series (last 20):")
        for i, p in enumerate(net_pnl_series[-20:], 1):
            bar = "█" * int(abs(p) / max(abs(x) for x in net_pnl_series) * 20)
            sign = "+" if p >= 0 else "-"
            print(f"    {i:3d}  {sign}${abs(p):.2f}  {bar}")
        print()

    print(SEP)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Polymarket bot — quant performance report",
    )
    parser.add_argument(
        "--csv",  default=DEFAULT_CSV,
        help=f"Path to quant_trades.csv (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--fee",  type=float, default=DEFAULT_FEE,
        help=f"Taker fee fraction (default: {DEFAULT_FEE})",
    )
    parser.add_argument(
        "--slip", type=float, default=DEFAULT_SLIP,
        help=f"Slippage fraction (default: {DEFAULT_SLIP})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show per-trade P&L bar chart",
    )
    args = parser.parse_args()
    report(path=args.csv, fee=args.fee, slip=args.slip, verbose=args.verbose)
