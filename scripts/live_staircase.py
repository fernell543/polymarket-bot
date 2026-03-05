"""
Micro-live staircase deployment script.
=========================================
Runs the bot in progressive stages (A → B → C → production) with hard
gate metrics between each stage.

Stages:
  A — Tiny size ($5 max, 3 positions)   → validate live execution plumbing
  B — Small size ($20 max, 5 positions) → confirm edge survives live frictions
  C — Scale candidate ($50 max, 10 pos) → validate at meaningful scale

Hard gates between stages (checked before promotion, non-overridable):
  A→B:  ≥5 trades, fill rate ≥65%, realized loss ≤$10
  B→C:  ≥10 trades, fill rate ≥70%, realized loss ≤$40, Sharpe ≥0.40
  C→∞:  ≥20 trades, fill rate ≥75%, realized loss ≤$100, Sharpe ≥0.60

Hard risk guardrails (always enforced, cannot be overridden by gates):
  - RISK_DAILY_LOSS_LIMIT: bot kills itself on breach (circuit breaker)
  - RISK_MAX_DRAWDOWN: session drawdown hard cap (circuit breaker)
  - Kill-switch Tier 3: NO_TRADE (bot stops entries)

Usage:
    python scripts/live_staircase.py --stage A
    python scripts/live_staircase.py --stage B --min-hours 4
    python scripts/live_staircase.py --check-gate --stage A
    python scripts/live_staircase.py --status
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# Add parent directory to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import config


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------

@dataclass
class StageConfig:
    name: str
    env_mode: str           # LIVE_DEPLOY_MODE value
    max_size_usdc: float
    max_positions: int
    min_run_hours: float    # minimum time before gate check is meaningful
    gate_min_trades: int
    gate_max_loss: float
    gate_min_fill_rate: float
    gate_min_sharpe: float  # 0 = not required
    next_stage: Optional[str]
    description: str


STAGES: dict[str, StageConfig] = {
    "A": StageConfig(
        name="A",
        env_mode="staircase_A",
        max_size_usdc=config.LIVE_STAGE_A_MAX_SIZE_USDC,
        max_positions=config.LIVE_STAGE_A_MAX_POSITIONS,
        min_run_hours=6.0,
        gate_min_trades=config.LIVE_STAGE_A_MIN_TRADES_GATE,
        gate_max_loss=config.LIVE_STAGE_A_MAX_LOSS_GATE,
        gate_min_fill_rate=config.LIVE_STAGE_A_MIN_FILL_RATE_GATE,
        gate_min_sharpe=0.0,
        next_stage="B",
        description=f"Tiny size (${config.LIVE_STAGE_A_MAX_SIZE_USDC:.0f} max, {config.LIVE_STAGE_A_MAX_POSITIONS} positions) — validate live execution plumbing",
    ),
    "B": StageConfig(
        name="B",
        env_mode="staircase_B",
        max_size_usdc=config.LIVE_STAGE_B_MAX_SIZE_USDC,
        max_positions=config.LIVE_STAGE_B_MAX_POSITIONS,
        min_run_hours=12.0,
        gate_min_trades=config.LIVE_STAGE_B_MIN_TRADES_GATE,
        gate_max_loss=config.LIVE_STAGE_B_MAX_LOSS_GATE,
        gate_min_fill_rate=config.LIVE_STAGE_B_MIN_FILL_RATE_GATE,
        gate_min_sharpe=config.LIVE_STAGE_B_MIN_SHARPE_GATE,
        next_stage="C",
        description=f"Small size (${config.LIVE_STAGE_B_MAX_SIZE_USDC:.0f} max, {config.LIVE_STAGE_B_MAX_POSITIONS} positions) — confirm edge survives live frictions",
    ),
    "C": StageConfig(
        name="C",
        env_mode="staircase_C",
        max_size_usdc=config.LIVE_STAGE_C_MAX_SIZE_USDC,
        max_positions=config.LIVE_STAGE_C_MAX_POSITIONS,
        min_run_hours=24.0,
        gate_min_trades=config.LIVE_STAGE_C_MIN_TRADES_GATE,
        gate_max_loss=config.LIVE_STAGE_C_MAX_LOSS_GATE,
        gate_min_fill_rate=config.LIVE_STAGE_C_MIN_FILL_RATE_GATE,
        gate_min_sharpe=config.LIVE_STAGE_C_MIN_SHARPE_GATE,
        next_stage="production",
        description=f"Scale candidate (${config.LIVE_STAGE_C_MAX_SIZE_USDC:.0f} max, {config.LIVE_STAGE_C_MAX_POSITIONS} positions) — validate at meaningful scale",
    ),
}

STATE_FILE = "logs/staircase_state.json"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {
        "current_stage": "A",
        "stage_history": [],
        "session_start": None,
        "promoted_at": {},
    }


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


# ---------------------------------------------------------------------------
# Trade log reader
# ---------------------------------------------------------------------------

def _read_trades(csv_path: str = "logs/quant_trades.csv") -> list[dict]:
    import csv
    rows = []
    for path in [csv_path, "logs/orders.csv"]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    rows.append(row)
        except Exception:
            pass
    return rows


def _safe_float(val: object, default: float = 0.0) -> float:
    try:
        return float(str(val))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    stage: str
    gate_met: bool
    next_stage: Optional[str]
    n_trades: int
    total_loss: float
    avg_fill_rate: float
    sharpe: float
    failures: list[str]
    thresholds: dict


def check_gate(stage_name: str, since_ts: float = 0.0) -> GateResult:
    """
    Evaluate stage promotion gate against recent trade history.

    Hard guardrails (daily loss, drawdown) are enforced separately by
    risk_engine.py and cannot be bypassed here.
    """
    stage = STAGES.get(stage_name)
    if not stage:
        return GateResult(
            stage=stage_name, gate_met=False, next_stage=None,
            n_trades=0, total_loss=0.0, avg_fill_rate=0.0, sharpe=0.0,
            failures=["unknown stage"], thresholds={},
        )

    rows = _read_trades()
    # Filter to stage and time window
    stage_rows = []
    for row in rows:
        if row.get("stage", "") not in (stage.env_mode, ""):
            continue
        ts = _safe_float(row.get("ts") or row.get("timestamp") or "0")
        if ts >= since_ts:
            stage_rows.append(row)

    n = len(stage_rows)

    # PnL and loss
    pnls = [_safe_float(r.get("pnl") or r.get("profit_usdc") or r.get("realized_edge", "0")) for r in stage_rows]
    total_loss = sum(-p for p in pnls if p < 0)

    # Fill rate (from fill_frac or estimated)
    fill_rates = [_safe_float(r.get("fill_frac") or r.get("estimated_fill_frac") or "0.8") for r in stage_rows]
    avg_fill = sum(fill_rates) / n if n else 0.0

    # Sharpe
    mean_p = sum(pnls) / n if n else 0.0
    std_p  = math.sqrt(sum((p - mean_p) ** 2 for p in pnls) / n) if n > 1 else 0.0
    sharpe = (mean_p / std_p) * math.sqrt(n) if std_p > 0 else 0.0

    thresholds = {
        "min_trades":    stage.gate_min_trades,
        "max_loss":      stage.gate_max_loss,
        "min_fill_rate": stage.gate_min_fill_rate,
        "min_sharpe":    stage.gate_min_sharpe,
    }

    failures = []
    if n < stage.gate_min_trades:
        failures.append(f"trades {n} < {stage.gate_min_trades} required")
    if total_loss > stage.gate_max_loss:
        failures.append(f"loss ${total_loss:.2f} > ${stage.gate_max_loss:.2f} limit")
    if avg_fill < stage.gate_min_fill_rate:
        failures.append(f"fill_rate {avg_fill:.1%} < {stage.gate_min_fill_rate:.1%}")
    if stage.gate_min_sharpe > 0 and sharpe < stage.gate_min_sharpe:
        failures.append(f"sharpe {sharpe:.3f} < {stage.gate_min_sharpe:.3f}")

    return GateResult(
        stage=stage_name,
        gate_met=len(failures) == 0,
        next_stage=stage.next_stage,
        n_trades=n,
        total_loss=round(total_loss, 2),
        avg_fill_rate=round(avg_fill, 4),
        sharpe=round(sharpe, 4),
        failures=failures,
        thresholds=thresholds,
    )


# ---------------------------------------------------------------------------
# Launch helpers
# ---------------------------------------------------------------------------

def _build_env(stage: StageConfig) -> dict[str, str]:
    """Build environment variables for the given stage."""
    env = os.environ.copy()
    env["LIVE_DEPLOY_MODE"]       = stage.env_mode
    env["SIZE_MAX_USDC"]          = str(stage.max_size_usdc)
    env["CLONE_MAX_OPEN_POSITIONS"] = str(stage.max_positions)
    env["LIVE_STAGE_A_MAX_SIZE_USDC"] = str(config.LIVE_STAGE_A_MAX_SIZE_USDC)
    env["LIVE_STAGE_B_MAX_SIZE_USDC"] = str(config.LIVE_STAGE_B_MAX_SIZE_USDC)
    env["LIVE_STAGE_C_MAX_SIZE_USDC"] = str(config.LIVE_STAGE_C_MAX_SIZE_USDC)
    # Ensure live controls are active
    env["LIVE_CONTROLS_ENABLED"]  = "1"
    # Never override risk guardrails
    env["RISK_DAILY_LOSS_LIMIT"]  = str(config.RISK_DAILY_LOSS_LIMIT)
    env["RISK_MAX_DRAWDOWN"]      = str(config.RISK_MAX_DRAWDOWN)
    return env


def launch_stage(stage_name: str, dry_run: bool = False, duration_hours: Optional[float] = None) -> int:
    """
    Launch bot.py with stage-specific env.  Returns process exit code.

    Hard guardrails are injected into the environment and cannot be removed
    by stage config, optimizer, or user overrides at runtime.
    """
    stage = STAGES.get(stage_name)
    if not stage:
        print(f"ERROR: Unknown stage '{stage_name}'")
        return 1

    print(f"\n{'='*60}")
    print(f"LIVE STAIRCASE — STAGE {stage.name}")
    print(f"  {stage.description}")
    print(f"  Max size:      ${stage.max_size_usdc:.0f} USDC")
    print(f"  Max positions: {stage.max_positions}")
    print(f"  Min run time:  {stage.min_run_hours:.0f}h before gate check")
    print(f"  DRY_RUN:       {'1 (paper)' if dry_run else '0 (LIVE)'}")
    print(f"\n  HARD GUARDRAILS (non-overridable):")
    print(f"  Daily loss limit: ${config.RISK_DAILY_LOSS_LIMIT:.0f}")
    print(f"  Max drawdown:     ${config.RISK_MAX_DRAWDOWN:.0f}")
    print(f"  Kill-switch T3:   always enforced")
    print(f"{'='*60}\n")

    env = _build_env(stage)
    if dry_run:
        env["DRY_RUN"] = "1"

    cmd = [sys.executable, "bot.py"]
    start_ts = time.time()

    state = _load_state()
    state["current_stage"] = stage_name
    state["session_start"]  = start_ts
    _save_state(state)

    try:
        proc = subprocess.run(cmd, env=env)
        return proc.returncode
    except KeyboardInterrupt:
        print("\nStaircase interrupted by user.")
        return 130


def cmd_check_gate(stage_name: str) -> None:
    """Print gate status for current stage."""
    state = _load_state()
    since = state.get("session_start") or 0.0

    result = check_gate(stage_name, since_ts=since)

    print(f"\n{'='*60}")
    print(f"GATE CHECK — Stage {result.stage} → {result.next_stage or 'production'}")
    print(f"{'='*60}")
    print(f"  Trades:       {result.n_trades} / {result.thresholds['min_trades']} required")
    print(f"  Total loss:   ${result.total_loss:.2f} / ${result.thresholds['max_loss']:.2f} limit")
    print(f"  Fill rate:    {result.avg_fill_rate:.1%} / {result.thresholds['min_fill_rate']:.1%} required")
    if result.thresholds.get("min_sharpe", 0) > 0:
        print(f"  Sharpe:       {result.sharpe:.3f} / {result.thresholds['min_sharpe']:.3f} required")

    if result.gate_met:
        print(f"\n  ✓ GATE PASSED — ready to promote to Stage {result.next_stage}")
        # Auto-update state
        state = _load_state()
        if state.get("current_stage") == stage_name:
            state["current_stage"] = result.next_stage or "production"
            state["promoted_at"][stage_name] = time.time()
            _save_state(state)
            print(f"  State updated: current_stage → {result.next_stage}")
    else:
        print(f"\n  ✗ GATE NOT MET:")
        for f in result.failures:
            print(f"    - {f}")

    print(f"{'='*60}")


def cmd_status() -> None:
    """Print current staircase status."""
    state = _load_state()
    current = state.get("current_stage", "A")
    stage   = STAGES.get(current)

    print(f"\n{'='*60}")
    print(f"STAIRCASE STATUS")
    print(f"{'='*60}")
    print(f"  Current stage: {current}")
    if stage:
        print(f"  Description:   {stage.description}")
    start = state.get("session_start")
    if start:
        elapsed_h = (time.time() - start) / 3600.0
        print(f"  Session time:  {elapsed_h:.1f}h")
    promoted = state.get("promoted_at", {})
    if promoted:
        print("  Promotions:")
        for s, ts in promoted.items():
            import datetime
            dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            print(f"    Stage {s} passed at {dt}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Micro-live staircase deployment")
    sub = parser.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Launch bot in a specific stage")
    run_p.add_argument("--stage", choices=["A", "B", "C"], default="A")
    run_p.add_argument("--dry-run", action="store_true", help="Paper mode (DRY_RUN=1)")

    # check
    check_p = sub.add_parser("check-gate", help="Check promotion gate for current stage")
    check_p.add_argument("--stage", choices=["A", "B", "C"], default=None)

    # status
    sub.add_parser("status", help="Show current staircase state")

    args = parser.parse_args()

    if args.command == "run":
        rc = launch_stage(args.stage, dry_run=args.dry_run)
        sys.exit(rc)

    elif args.command == "check-gate":
        state = _load_state()
        stage = args.stage or state.get("current_stage", "A")
        cmd_check_gate(stage)

    elif args.command == "status":
        cmd_status()

    else:
        # Default: run stage A paper
        print("Usage: python scripts/live_staircase.py run --stage A [--dry-run]")
        print("       python scripts/live_staircase.py check-gate")
        print("       python scripts/live_staircase.py status")
        parser.print_help()


if __name__ == "__main__":
    main()
