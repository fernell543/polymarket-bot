"""
Supervised AI Controller
=========================
Reads latest metrics and recommendations, then optionally applies parameter
updates within a strict policy-bounded allowlist.

Modes (CONTROL_MODE env var):
  manual      — (default) reads recommendations and prints them; NEVER writes.
  supervised  — auto-applies parameter updates within the tunable_params bounds
                defined in agent_loop/controller_policy.json.

Safety model:
  - Hard risk limits (RISK_DAILY_LOSS_LIMIT, RISK_MAX_DRAWDOWN, staircase caps,
    wallet keys, DRY_RUN flag) are in the forbidden_params list and are NEVER
    touched by the controller.
  - The controller ONLY writes to logs/applied_params.json — it never edits
    Python source files, .env, or any other config file.
  - The bot reads applied_params.json on startup (if present) to override env
    defaults; all hard caps in risk_engine._refresh_state() still apply.
  - Every action is appended to logs/controller_audit.jsonl with full input/
    output trace for operator review.
  - Live code patching is DISABLED. Only parameter value tuning is allowed.

Usage
-----
  python -m agent_loop.controller run     # one tuning cycle
  python -m agent_loop.controller status  # print last applied params + audit
  CONTROL_MODE=supervised python -m agent_loop.controller run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

LOGS_DIR              = _ROOT / "logs"
POLICY_FILE           = Path(__file__).parent / "controller_policy.json"
DECISIONS_LOG         = LOGS_DIR / "supervised_decisions.jsonl"
AUDIT_LOG             = LOGS_DIR / "controller_audit.jsonl"
APPLIED_PARAMS_FILE   = LOGS_DIR / "applied_params.json"

CONTROL_MODE = os.getenv("CONTROL_MODE", "manual")


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------

def _load_policy() -> dict[str, Any]:
    if not POLICY_FILE.exists():
        raise FileNotFoundError(f"Controller policy not found: {POLICY_FILE}")
    return json.loads(POLICY_FILE.read_text())


def _validate_param(
    key: str,
    value: Any,
    tunable: dict[str, Any],
    forbidden: list[str],
) -> tuple[bool, str]:
    """
    Validate a single proposed parameter update.

    Returns (ok, reason).  ok=False means the update must be rejected.
    """
    if key in forbidden:
        return False, f"FORBIDDEN — {key} is in the forbidden_params list"

    if key not in tunable:
        return False, f"NOT_ALLOWED — {key} is not in tunable_params allowlist"

    bounds = tunable[key]
    lo, hi = bounds.get("min"), bounds.get("max")

    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, f"INVALID — value {value!r} is not numeric"

    if lo is not None and v < lo:
        return False, f"OUT_OF_BOUNDS — {v} < min={lo} for {key}"
    if hi is not None and v > hi:
        return False, f"OUT_OF_BOUNDS — {v} > max={hi} for {key}"

    return True, "ok"


# ---------------------------------------------------------------------------
# Applied params helpers
# ---------------------------------------------------------------------------

def _load_applied() -> dict[str, Any]:
    if not APPLIED_PARAMS_FILE.exists():
        return {}
    try:
        return json.loads(APPLIED_PARAMS_FILE.read_text())
    except Exception:
        return {}


def _save_applied(params: dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    APPLIED_PARAMS_FILE.write_text(json.dumps(params, indent=2))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _audit(record: dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Latest recommendation reader
# ---------------------------------------------------------------------------

def _latest_recommendation() -> dict[str, Any] | None:
    """Read the most recent supervised-cycle recommendation from decisions log."""
    if not DECISIONS_LOG.exists():
        return None
    try:
        lines = [l for l in DECISIONS_LOG.read_text().strip().splitlines() if l.strip()]
        if not lines:
            return None
        last = json.loads(lines[-1])
        return last.get("recommendation")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core cycle
# ---------------------------------------------------------------------------

def run_cycle(verbose: bool = False) -> int:
    """
    One controller cycle:
      1. Load policy.
      2. Read latest recommendation from supervised decisions log.
      3. In manual mode: print recommendations, write nothing.
      4. In supervised mode: validate each proposed param against allowlist,
         apply approved changes to applied_params.json, audit every action.

    Returns exit code (0 = ok, 1 = error).
    """
    cycle_id = str(uuid.uuid4())[:8]
    ts       = datetime.now(timezone.utc).isoformat()
    mode     = CONTROL_MODE

    print("\n" + "=" * 60)
    print(f"  Supervised Controller  [{cycle_id}]")
    print("=" * 60)
    print(f"  Timestamp    : {ts}")
    print(f"  Control mode : {mode.upper()}")
    print(f"  Policy file  : {POLICY_FILE}")

    # 1. Load policy
    try:
        policy = _load_policy()
    except FileNotFoundError as exc:
        print(f"\n[ERROR] {exc}")
        return 1

    tunable  = policy.get("tunable_params", {})
    forbidden = policy.get("forbidden_params", [])

    if verbose:
        print(f"\n  Tunable keys : {', '.join(tunable.keys())}")
        print(f"  Forbidden    : {', '.join(forbidden)}")

    # 2. Read recommendation
    rec = _latest_recommendation()
    if not rec:
        print("\n  No recommendation found. Run a supervised cycle first:")
        print("  python -m agent_loop.orchestrator cycle")
        _audit({
            "cycle_id": cycle_id,
            "timestamp": ts,
            "control_mode": mode,
            "action": "no_op",
            "reason": "no recommendation available",
        })
        return 0

    proposed = rec.get("proposed_params", {})
    source   = rec.get("source", "unknown")
    rationale = rec.get("rationale", [])

    print(f"\n  Recommendation source : {source}")
    print(f"  Proposed params       : {len(proposed)}")
    for k, v in proposed.items():
        base = rec.get("baseline_params", {}).get(k, "?")
        print(f"    {k} = {v}  (was {base})")
    print("  Rationale:")
    for r in rationale:
        print(f"    * {r}")

    # 3. Manual mode — read-only, no writes
    if mode == "manual":
        print(
            "\n  [MANUAL MODE] No parameters applied. "
            "Set CONTROL_MODE=supervised to enable auto-tuning."
        )
        _audit({
            "cycle_id":     cycle_id,
            "timestamp":    ts,
            "control_mode": mode,
            "action":       "read_only",
            "proposed":     proposed,
            "applied":      {},
            "reason":       "manual mode — operator must apply changes",
        })
        return 0

    # 4. Supervised mode — validate + apply
    print("\n  [SUPERVISED MODE] Validating proposed params against allowlist ...")

    current_applied = _load_applied()
    new_applied:  dict[str, Any] = dict(current_applied)
    approved:     dict[str, Any] = {}
    rejected:     dict[str, str] = {}

    for key, value in proposed.items():
        ok, reason = _validate_param(key, value, tunable, forbidden)
        if ok:
            approved[key] = value
            print(f"    [APPROVED]  {key} = {value}")
        else:
            rejected[key] = reason
            print(f"    [REJECTED]  {key} = {value}  ({reason})")

    if not approved:
        print("\n  No approved changes — applied_params.json unchanged.")
        _audit({
            "cycle_id":     cycle_id,
            "timestamp":    ts,
            "control_mode": mode,
            "action":       "no_changes",
            "proposed":     proposed,
            "approved":     {},
            "rejected":     rejected,
        })
        return 0

    # Write approved params
    new_applied.update(approved)
    new_applied["_controller_last_updated"] = ts
    new_applied["_controller_cycle_id"]     = cycle_id
    _save_applied(new_applied)

    _audit({
        "cycle_id":         cycle_id,
        "timestamp":        ts,
        "control_mode":     mode,
        "action":           "apply",
        "source":           source,
        "proposed":         proposed,
        "approved":         approved,
        "rejected":         rejected,
        "applied_file":     str(APPLIED_PARAMS_FILE),
        "previous_applied": current_applied,
        "new_applied":      new_applied,
    })

    print(f"\n  Applied {len(approved)} param(s) -> {APPLIED_PARAMS_FILE}")
    print(f"  Audit   -> {AUDIT_LOG}")
    print("\n  NOTE: Restart the bot to pick up the new parameters.")
    print("        Hard risk limits are enforced by risk_engine regardless.")

    return 0


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

def run_status() -> int:
    """Print last applied params and last N audit entries."""
    print("\n" + "=" * 60)
    print("  Controller Status")
    print("=" * 60)
    print(f"  Control mode : {CONTROL_MODE.upper()}")

    applied = _load_applied()
    if applied:
        print(f"\n  Applied params ({APPLIED_PARAMS_FILE}):")
        for k, v in applied.items():
            print(f"    {k} = {v}")
    else:
        print("\n  No applied params yet.")

    if AUDIT_LOG.exists():
        lines = [l for l in AUDIT_LOG.read_text().strip().splitlines() if l.strip()]
        tail  = lines[-5:] if len(lines) > 5 else lines
        print(f"\n  Last {len(tail)} audit entries ({AUDIT_LOG}):")
        for line in tail:
            try:
                entry = json.loads(line)
                print(
                    f"    [{entry.get('timestamp','?')}] "
                    f"mode={entry.get('control_mode','?')} "
                    f"action={entry.get('action','?')} "
                    f"approved={list(entry.get('approved', {}).keys())}"
                )
            except Exception:
                print(f"    {line[:120]}")
    else:
        print("\n  No audit log yet.")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supervised parameter controller for polymarket-bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m agent_loop.controller run              # manual mode (read-only)\n"
            "  CONTROL_MODE=supervised python -m agent_loop.controller run\n"
            "  python -m agent_loop.controller status           # show applied params\n"
        ),
    )
    sub  = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run",    help="Run one controller cycle")
    run_p.add_argument("--verbose", action="store_true", help="Extra debug output")
    sub.add_parser("status", help="Show applied params and recent audit log")

    args = parser.parse_args()

    if args.command == "status":
        sys.exit(run_status())
    else:
        verbose = getattr(args, "verbose", False)
        sys.exit(run_cycle(verbose=verbose))


if __name__ == "__main__":
    main()
