"""
Live-proxy validator.
======================
Retroactively applies live-execution frictions to historical paper/dry-run
trade logs to estimate what live performance would have looked like.

Inputs  (any combination):
  logs/quant_trades.csv   — detailed quant trade journal
  logs/orders.csv         — paper order log (DRY_RUN=1 mode)

Outputs:
  Printed report + optional JSON dump (--output path)

Friction model applied per-trade:
  - Slippage:     base + spread_at_entry * SLIP_SPREAD_FACTOR + (size/$100) * IMPACT
  - Partial fill: min(1, depth_estimate * 0.5 / size)
  - Realized PnL = raw_pnl - slippage * size - (1 - fill_frac) * estimated_holding_cost

Usage:
    python -m analytics.live_proxy_validator
    python -m analytics.live_proxy_validator --output logs/live_proxy_report.json
    python -m analytics.live_proxy_validator --csv logs/orders.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import config

# ---------------------------------------------------------------------------
# Friction model (mirrors live_realism.py but works on CSV data offline)
# ---------------------------------------------------------------------------

def _slippage(spread: float, size_usdc: float, depth_usdc: float = 500.0) -> float:
    base       = config.LIVE_SLIP_BASE
    spread_c   = spread * config.LIVE_SLIP_SPREAD_FACTOR
    impact_c   = (size_usdc / 100.0) * config.LIVE_SLIP_IMPACT_FACTOR
    available  = depth_usdc * config.LIVE_DEPTH_FILL_FRACTION
    overflow   = max(0.0, size_usdc - available) / max(depth_usdc, 1.0)
    thin_pen   = overflow * spread * 0.5
    return min(spread, base + spread_c + impact_c + thin_pen)


def _fill_frac(size_usdc: float, depth_usdc: float = 500.0) -> float:
    available = depth_usdc * config.LIVE_DEPTH_FILL_FRACTION
    return min(1.0, available / size_usdc) if size_usdc > 0 else 1.0


def _realized_edge(theoretical: float, slip: float, fill: float) -> float:
    filled_edge = theoretical - slip
    unfilled_adverse = (1.0 - fill) * slip * 0.3
    return filled_edge - unfilled_adverse


# ---------------------------------------------------------------------------
# Per-trade proxy result
# ---------------------------------------------------------------------------

@dataclass
class TradeProxyResult:
    ts: str
    question: str
    outcome: str
    size_usdc: float
    entry_price: float
    spread_at_entry: float
    depth_estimate: float
    paper_pnl: float
    estimated_slippage: float
    estimated_fill_frac: float
    live_pnl_estimate: float
    theoretical_edge: float
    realized_edge: float
    pass_live_filter: bool   # would this trade have been taken in live mode?
    skip_reason: str


# ---------------------------------------------------------------------------
# CSV readers
# ---------------------------------------------------------------------------

def _read_quant_trades(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(row)
    except FileNotFoundError:
        pass
    return rows


def _read_orders(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(row)
    except FileNotFoundError:
        pass
    return rows


def _safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val) if val not in ("", None, "N/A") else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Proxy analysis
# ---------------------------------------------------------------------------

def analyse_trades(rows: list[dict]) -> list[TradeProxyResult]:
    results = []
    for row in rows:
        size    = _safe_float(row.get("size_usdc") or row.get("size", "0"))
        price   = _safe_float(row.get("price") or row.get("entry_price", "0.5"))
        spread  = _safe_float(row.get("spread_at_entry") or row.get("spread", "0.04"))
        depth   = _safe_float(row.get("depth_usdc", "500"))
        paper_pnl = _safe_float(row.get("pnl") or row.get("profit_usdc", "0"))
        question  = str(row.get("question") or row.get("market", ""))
        outcome   = str(row.get("outcome") or row.get("side", ""))
        ts        = str(row.get("ts") or row.get("timestamp") or row.get("time", ""))

        if size <= 0 or price <= 0:
            continue

        if spread <= 0:
            spread = 0.04  # conservative default if missing

        slip       = _slippage(spread, size, depth)
        fill       = _fill_frac(size, depth)
        theoretical = max(0.0, (1.0 - price) - config.POLYMARKET_FEE)
        re         = _realized_edge(theoretical, slip, fill)
        live_pnl   = paper_pnl - slip * size - (1.0 - fill) * size * 0.01

        # Live filter checks (would this trade pass live guards?)
        skip_reason = ""
        pass_filter = True

        if depth < config.LIVE_MIN_DEPTH_USDC:
            pass_filter = False
            skip_reason = f"depth {depth:.0f} < {config.LIVE_MIN_DEPTH_USDC:.0f}"
        elif re <= 0:
            pass_filter = False
            skip_reason = f"realized edge {re:.4f} ≤ 0"
        elif spread > 0.20:
            pass_filter = False
            skip_reason = f"spread {spread:.2f} > 20% (likely illiquid)"

        results.append(TradeProxyResult(
            ts=ts,
            question=question[:60],
            outcome=outcome,
            size_usdc=size,
            entry_price=price,
            spread_at_entry=spread,
            depth_estimate=depth,
            paper_pnl=round(paper_pnl, 4),
            estimated_slippage=round(slip, 6),
            estimated_fill_frac=round(fill, 4),
            live_pnl_estimate=round(live_pnl, 4),
            theoretical_edge=round(theoretical, 6),
            realized_edge=round(re, 6),
            pass_live_filter=pass_filter,
            skip_reason=skip_reason,
        ))

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(results: list[TradeProxyResult]) -> dict:
    if not results:
        return {"error": "no trades found in log files"}

    n_total     = len(results)
    n_pass      = sum(1 for r in results if r.pass_live_filter)
    n_fail      = n_total - n_pass

    paper_pnl   = sum(r.paper_pnl for r in results)
    live_pnl    = sum(r.live_pnl_estimate for r in results if r.pass_live_filter)
    live_pnl_all = sum(r.live_pnl_estimate for r in results)

    total_slip  = sum(r.estimated_slippage * r.size_usdc for r in results)
    avg_fill    = sum(r.estimated_fill_frac for r in results) / n_total
    avg_re_edge = sum(r.realized_edge for r in results) / n_total

    pass_results = [r for r in results if r.pass_live_filter]
    pass_paper_pnl = sum(r.paper_pnl for r in pass_results)
    pass_pnls = [r.live_pnl_estimate for r in pass_results]
    if pass_pnls:
        mean_p = sum(pass_pnls) / len(pass_pnls)
        std_p  = math.sqrt(sum((p - mean_p) ** 2 for p in pass_pnls) / len(pass_pnls)) if len(pass_pnls) > 1 else 0.0
        sharpe = (mean_p / std_p) * math.sqrt(len(pass_pnls)) if std_p > 0 else 0.0
    else:
        sharpe = 0.0

    skip_reasons: dict[str, int] = {}
    for r in results:
        if r.skip_reason:
            skip_reasons[r.skip_reason] = skip_reasons.get(r.skip_reason, 0) + 1

    worst_trades = sorted(results, key=lambda r: r.live_pnl_estimate)[:5]
    best_trades  = sorted(results, key=lambda r: r.live_pnl_estimate, reverse=True)[:5]

    # Gate check against staircase thresholds
    stage = config.LIVE_DEPLOY_MODE
    gate_checks = _check_stage_gates(stage, pass_results)

    return {
        "summary": {
            "n_total":            n_total,
            "n_pass_live_filter": n_pass,
            "n_fail_live_filter": n_fail,
            "live_filter_rate":   round(n_pass / n_total, 4) if n_total else 0.0,
            "paper_pnl_all":      round(paper_pnl, 4),
            "paper_pnl_passing":  round(pass_paper_pnl, 4),
            "live_pnl_estimate_passing": round(live_pnl, 4),
            "live_pnl_estimate_all":     round(live_pnl_all, 4),
            "total_slippage_cost":round(total_slip, 4),
            "avg_fill_fraction":  round(avg_fill, 4),
            "avg_realized_edge":  round(avg_re_edge, 6),
            "sharpe_live_proxy":  round(sharpe, 4),
        },
        "skip_reason_breakdown": skip_reasons,
        "gate_checks": gate_checks,
        "worst_5_live_trades": [asdict(t) for t in worst_trades],
        "best_5_live_trades":  [asdict(t) for t in best_trades],
    }


def _check_stage_gates(stage: str, pass_results: list[TradeProxyResult]) -> dict:
    """Check if results meet the promotion gate for the current stage."""
    n = len(pass_results)
    if n == 0:
        return {"stage": stage, "gate_met": False, "reason": "no passing trades"}

    total_loss = sum(-r.live_pnl_estimate for r in pass_results if r.live_pnl_estimate < 0)
    pnl_all    = sum(r.live_pnl_estimate for r in pass_results)

    # Approximate fill rate from fill_frac average
    avg_fill = sum(r.estimated_fill_frac for r in pass_results) / n

    pnls = [r.live_pnl_estimate for r in pass_results]
    mean_p = sum(pnls) / n
    std_p  = math.sqrt(sum((p - mean_p) ** 2 for p in pnls) / n) if n > 1 else 0.0
    sharpe = (mean_p / std_p) * math.sqrt(n) if std_p > 0 else 0.0

    gates = {
        "staircase_A": {
            "min_trades":   config.LIVE_STAGE_A_MIN_TRADES_GATE,
            "max_loss":     config.LIVE_STAGE_A_MAX_LOSS_GATE,
            "min_fill_rate": config.LIVE_STAGE_A_MIN_FILL_RATE_GATE,
        },
        "staircase_B": {
            "min_trades":   config.LIVE_STAGE_B_MIN_TRADES_GATE,
            "max_loss":     config.LIVE_STAGE_B_MAX_LOSS_GATE,
            "min_fill_rate": config.LIVE_STAGE_B_MIN_FILL_RATE_GATE,
            "min_sharpe":   config.LIVE_STAGE_B_MIN_SHARPE_GATE,
        },
        "staircase_C": {
            "min_trades":   config.LIVE_STAGE_C_MIN_TRADES_GATE,
            "max_loss":     config.LIVE_STAGE_C_MAX_LOSS_GATE,
            "min_fill_rate": config.LIVE_STAGE_C_MIN_FILL_RATE_GATE,
            "min_sharpe":   config.LIVE_STAGE_C_MIN_SHARPE_GATE,
        },
    }

    thresholds = gates.get(stage)
    if not thresholds:
        return {"stage": stage, "gate_met": True, "reason": "production mode — no gate"}

    failures = []
    if n < thresholds["min_trades"]:
        failures.append(f"trades {n} < {thresholds['min_trades']} required")
    if total_loss > thresholds["max_loss"]:
        failures.append(f"loss {total_loss:.2f} > {thresholds['max_loss']:.2f} limit")
    if avg_fill < thresholds["min_fill_rate"]:
        failures.append(f"fill_rate {avg_fill:.2%} < {thresholds['min_fill_rate']:.2%}")
    if "min_sharpe" in thresholds and sharpe < thresholds["min_sharpe"]:
        failures.append(f"sharpe {sharpe:.3f} < {thresholds['min_sharpe']:.3f}")

    gate_met = len(failures) == 0
    return {
        "stage":        stage,
        "gate_met":     gate_met,
        "next_stage":   {"staircase_A": "staircase_B", "staircase_B": "staircase_C"}.get(stage, "production"),
        "metrics": {
            "trades":    n,
            "total_loss_usdc": round(total_loss, 2),
            "avg_fill_rate": round(avg_fill, 4),
            "sharpe":    round(sharpe, 4),
        },
        "thresholds":   thresholds,
        "failures":     failures,
        "reason":       "PASS" if gate_met else "; ".join(failures),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Live-proxy validator for trade logs")
    parser.add_argument("--csv",    default="logs/quant_trades.csv",
                        help="Path to trade CSV (default: logs/quant_trades.csv)")
    parser.add_argument("--orders", default="logs/orders.csv",
                        help="Path to orders CSV (default: logs/orders.csv)")
    parser.add_argument("--output", default="",
                        help="Write JSON report to this path (optional)")
    args = parser.parse_args()

    rows = []
    for path in [args.csv, args.orders]:
        loaded = _read_quant_trades(path) or _read_orders(path)
        if loaded:
            print(f"Loaded {len(loaded)} rows from {path}")
            rows.extend(loaded)

    if not rows:
        print("No trade data found. Run paper mode first to generate logs.")
        sys.exit(1)

    results = analyse_trades(rows)
    report  = generate_report(results)

    # --- Print summary ---
    s = report["summary"]
    print("\n" + "=" * 60)
    print("LIVE PROXY VALIDATION REPORT")
    print("=" * 60)
    print(f"  Trades analysed:        {s['n_total']}")
    print(f"  Pass live filter:       {s['n_pass_live_filter']}  ({s['live_filter_rate']:.1%})")
    print(f"  Fail live filter:       {s['n_fail_live_filter']}")
    print(f"  Paper PnL (all):        ${s['paper_pnl_all']:+.2f}")
    print(f"  Paper PnL (passing):    ${s['paper_pnl_passing']:+.2f}")
    print(f"  Live PnL est (passing): ${s['live_pnl_estimate_passing']:+.2f}")
    print(f"  Live PnL est (all):     ${s['live_pnl_estimate_all']:+.2f}")
    print(f"  Total slippage cost:    ${s['total_slippage_cost']:.2f}")
    print(f"  Avg fill fraction:      {s['avg_fill_fraction']:.1%}")
    print(f"  Avg realized edge:      {s['avg_realized_edge']:.4f}")
    print(f"  Sharpe (live proxy):    {s['sharpe_live_proxy']:.3f}")

    if report["skip_reason_breakdown"]:
        print("\n  Skip-reason breakdown:")
        for reason, count in report["skip_reason_breakdown"].items():
            print(f"    {count:3d}x  {reason}")

    gc = report["gate_checks"]
    print(f"\n  Stage gate ({gc['stage']} → {gc.get('next_stage', 'production')}):")
    print(f"    Gate met: {'YES ✓' if gc['gate_met'] else 'NO ✗'}")
    if gc.get("failures"):
        for f in gc["failures"]:
            print(f"    FAIL: {f}")
    if gc.get("metrics"):
        m = gc["metrics"]
        print(f"    trades={m['trades']}  loss=${m['total_loss_usdc']:.2f}  "
              f"fill={m['avg_fill_rate']:.1%}  sharpe={m['sharpe']:.3f}")

    print("=" * 60)

    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nFull report written to: {args.output}")


if __name__ == "__main__":
    main()
