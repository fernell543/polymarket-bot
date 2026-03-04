"""
Supervised improvement loop orchestrator.

Runs one complete cycle:
  ingest → propose → validate → scorecard → deploy plan → log decision

NO automatic deployment to live trading.
All changes require operator review of logs/deployment_plan.json.

Usage
-----
python -m agent_loop.orchestrator cycle    # run one supervised cycle
python -m agent_loop.orchestrator report   # show latest scorecard + recommendation
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is importable when run as __main__
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent_loop.policy       import Policy, POLICY_FILE
from agent_loop.propose_patch import propose
from agent_loop.validate     import validate
from agent_loop.scorecard    import compute_scorecard
from agent_loop.deploy       import plan_deployment, load_state, save_state

LOGS_DIR      = _ROOT / "logs"
DECISIONS_LOG = LOGS_DIR / "supervised_decisions.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _log_decision(record: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(DECISIONS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def _hdr(title: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def _sec(title: str) -> None:
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------

def run_cycle(verbose: bool = False) -> int:
    """Run one supervised improvement cycle. Returns exit code (0=ok, 1=error)."""
    cycle_id = str(uuid.uuid4())[:8]
    ts       = datetime.now(timezone.utc).isoformat()

    _hdr(f"Supervised Improvement Cycle  [{cycle_id}]")
    print(f"  Timestamp : {ts}")
    print(f"  Policy    : {POLICY_FILE}")

    # 1. Load policy
    try:
        policy = Policy.load(POLICY_FILE)
    except Exception as e:
        print(f"\n[ERROR] Failed to load policy: {e}")
        return 1

    state = load_state()
    print(f"  Phase     : {state.get('phase', 'paper')}")
    print(f"  Cycle #   : {state.get('cycle_count', 0) + 1}")

    # 2. Ingest analytics
    _sec("1. Ingest Analytics")
    perf   = _load_json(LOGS_DIR / "perf.json")
    trades = _load_csv(LOGS_DIR / "quant_trades.csv")

    # opt_best.json may be a list (top-N) or a single dict
    raw_opt = _load_json(LOGS_DIR / "opt_best.json")
    if not raw_opt:
        # Try reading as a list-type JSON
        opt_path = LOGS_DIR / "opt_best.json"
        if opt_path.exists():
            try:
                raw_list = json.loads(opt_path.read_text())
                if isinstance(raw_list, list) and raw_list:
                    raw_opt = raw_list[0]
            except Exception:
                pass
    opt_best: dict[str, Any] = raw_opt if isinstance(raw_opt, dict) else {}

    print(f"  perf.json        : {'found' if perf else 'not found'}")
    print(f"  quant_trades.csv : {len(trades)} rows")
    print(f"  opt_best.json    : {'found' if opt_best else 'not found'}")
    if perf:
        print(f"  Health           : {perf.get('health', 'unknown')}")
        print(f"  Kill-switch tier : {perf.get('kill_switch_tier', 'N/A')}")
        print(f"  Balance USDC     : {perf.get('balance_usdc', 'N/A')}")

    # 3. Propose patch
    _sec("2. Generate Recommendation")
    analytics_data = {"perf": perf, "opt_best": opt_best, "trades": trades}
    recommendation = propose(analytics_data, LOGS_DIR)

    print(f"  Source           : {recommendation.source}")
    print("  Proposed params  :")
    for k, v in recommendation.proposed_params.items():
        base   = recommendation.baseline_params.get(k, "?")
        change = " (unchanged)" if v == base else f"  (was {base})"
        print(f"    {k} = {v}{change}")
    print("  Rationale        :")
    for r in recommendation.rationale:
        print(f"    * {r}")

    # 4. Validate
    _sec("3. Validation (Trade Replay)")
    val_result = validate(recommendation, trades, LOGS_DIR)

    print(f"  Data source      : {val_result.data_source}")
    print(f"  Total trades     : {val_result.n_trades}")
    print(f"  Qualifying       : {val_result.n_qualifying}")
    print(f"  Selection rate   : {val_result.selection_rate:.1%}")
    print(f"  Win rate         : {val_result.win_rate_pct:.1f}%")
    print(f"  Sharpe-like      : {val_result.sharpe_like:.3f}")
    print(f"  Net edge (bps)   : {val_result.net_edge_bps:.1f}")
    print(f"  Total net PnL    : ${val_result.total_net_pnl:.4f}")
    print(f"  Max drawdown     : ${val_result.max_drawdown:.4f}")
    print(f"  Fill ratio       : {val_result.fill_ratio:.1%}")
    print(f"  Adverse sel.     : {val_result.adverse_selection_pct:.4%}")
    if val_result.warning:
        print(f"  [WARN] {val_result.warning}")

    # 5. Scorecard
    _sec("4. Scorecard")
    scorecard = compute_scorecard(val_result, perf, policy)

    print(f"  Decision         : {scorecard.decision.value}")
    print(f"  Composite score  : {scorecard.composite_score:.3f}  (0=worst, 1=best)")
    print(f"  Win rate         : {scorecard.metrics.win_rate_pct:.1f}%")
    print(f"  Sharpe-like      : {scorecard.metrics.sharpe_like:.3f}")
    print(f"  Net edge (bps)   : {scorecard.metrics.net_edge_bps:.1f}")
    print(f"  Violations       : {len(scorecard.violations)}")
    for v in scorecard.violations:
        print(f"    [X] {v.message}")
    for r in scorecard.reasons:
        print(f"    -> {r}")

    # 6. Deployment plan
    _sec("5. Deployment Plan")
    plan = plan_deployment(scorecard, recommendation, policy, LOGS_DIR)

    print(f"  Action           : {plan.action}")
    print(f"  Phase            : {plan.from_phase}  ->  {plan.to_phase}")
    print(f"  Auto-deployable  : {plan.auto_deployable}  (always False by design)")
    print()
    for instr in plan.instructions:
        print(f"  {instr}")

    # 7. Log decision
    record: dict[str, Any] = {
        "cycle_id":   cycle_id,
        "timestamp":  ts,
        "phase":      state.get("phase", "paper"),
        "decision":   scorecard.decision.value,
        "composite_score": scorecard.composite_score,
        "scorecard":  scorecard.to_dict(),
        "recommendation": recommendation.to_dict(),
        "validation": val_result.to_dict(),
        "plan":       plan.to_dict(),
    }
    _log_decision(record)

    # Update persistent state
    state["cycle_count"]   = state.get("cycle_count", 0) + 1
    state["last_cycle"]    = ts
    state["last_decision"] = scorecard.decision.value
    if plan.action == "promote_micro_live":
        state["phase"]          = plan.to_phase
        state["last_promotion"] = ts
    save_state(state)

    _hdr("Cycle Complete")
    print(f"  Decision : {scorecard.decision.value}")
    print(f"  Plan     : {LOGS_DIR / 'deployment_plan.json'}")
    print(f"  Log      : {DECISIONS_LOG}")
    print()
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def run_report() -> int:
    """Print the latest scorecard and recommendation from the decision log."""
    _hdr("Latest Supervised Cycle Report")

    state = load_state()
    print(f"  Current phase : {state.get('phase', 'paper')}")
    print(f"  Total cycles  : {state.get('cycle_count', 0)}")
    print(f"  Last cycle    : {state.get('last_cycle', 'never')}")
    print(f"  Last decision : {state.get('last_decision', 'none')}")

    if not DECISIONS_LOG.exists():
        print("\n  No decision log found. Run a cycle first:")
        print("  .\\run_supervised_cycle.ps1")
        return 1

    lines = [l for l in DECISIONS_LOG.read_text().strip().splitlines() if l.strip()]
    if not lines:
        print("\n  Decision log is empty.")
        return 1

    last = json.loads(lines[-1])

    _sec("Last Cycle")
    print(f"  Cycle ID  : {last.get('cycle_id')}")
    print(f"  Timestamp : {last.get('timestamp')}")
    print(f"  Decision  : {last.get('decision')}")
    print(f"  Score     : {last.get('composite_score', 0):.3f}")

    sc = last.get("scorecard", {})
    m  = sc.get("metrics", {})
    _sec("Scorecard Metrics")
    for k, v in m.items():
        print(f"  {k:<35s}: {v}")

    _sec("Violations")
    viols = sc.get("violations", [])
    if viols:
        for v in viols:
            print(f"  [X] {v.get('message')}")
    else:
        print("  (none)")

    _sec("Reasons")
    for r in sc.get("reasons", []):
        print(f"  {r}")

    _sec("Recommendation")
    rec = last.get("recommendation", {})
    print(f"  Source : {rec.get('source')}")
    for k, v in rec.get("proposed_params", {}).items():
        print(f"  {k} = {v}")
    for r in rec.get("rationale", []):
        print(f"  * {r}")

    _sec("Deployment Plan")
    plan = last.get("plan", {})
    print(f"  Action : {plan.get('action')}")
    print(f"  Phase  : {plan.get('from_phase')} -> {plan.get('to_phase')}")
    for instr in plan.get("instructions", [])[:15]:
        print(f"  {instr}")

    plan_file = LOGS_DIR / "deployment_plan.json"
    if plan_file.exists():
        print(f"\n  Full plan : {plan_file}")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supervised improvement loop for polymarket-bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m agent_loop.orchestrator cycle    # run one cycle\n"
            "  python -m agent_loop.orchestrator report   # show latest report\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")
    cycle_p = sub.add_parser("cycle",  help="Run one supervised improvement cycle")
    cycle_p.add_argument("--verbose", action="store_true", help="Extra debug output")
    sub.add_parser("report", help="Show latest scorecard and recommendation")
    args = parser.parse_args()

    if args.command == "report":
        sys.exit(run_report())
    else:
        verbose = getattr(args, "verbose", False)
        sys.exit(run_cycle(verbose=verbose))


if __name__ == "__main__":
    main()
