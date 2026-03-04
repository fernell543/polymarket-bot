"""
Deployment planner.

Generates safe, operator-reviewed promotion plans.

IMPORTANT: This module NEVER auto-applies changes to live trading.
All plans are written to logs/deployment_plan.json for operator review.
The operator must manually apply environment-variable changes before the next session.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_loop.policy import Policy
from agent_loop.propose_patch import RecommendationPackage
from agent_loop.scorecard import Decision, ScorecardResult

LOGS_DIR   = Path(__file__).parent.parent / "logs"
STATE_FILE = LOGS_DIR / "supervised_state.json"
PLAN_FILE  = LOGS_DIR / "deployment_plan.json"

PHASE_ORDER = ["paper", "micro_live", "scale_candidate"]


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"phase": "paper", "cycle_count": 0, "last_promotion": None}


def save_state(state: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Deployment plan
# ---------------------------------------------------------------------------

@dataclass
class DeploymentPlan:
    """A deployment plan for operator review. Never auto-executed.

    Attributes
    ----------
    action          : "reject" | "stay_paper" | "promote_micro_live" | "manual_review"
    from_phase      : current promotion phase
    to_phase        : target phase (may equal from_phase)
    env_overrides   : environment variables to set before next session
    micro_live_caps : policy caps that apply in micro-live phase
    instructions    : human-readable operator checklist
    auto_deployable : always False — hard design constraint
    """
    action:          str
    from_phase:      str
    to_phase:        str
    env_overrides:   dict[str, Any]
    micro_live_caps: dict[str, Any]
    instructions:    list[str]
    auto_deployable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action":          self.action,
            "from_phase":      self.from_phase,
            "to_phase":        self.to_phase,
            "env_overrides":   self.env_overrides,
            "micro_live_caps": self.micro_live_caps,
            "instructions":    self.instructions,
            "auto_deployable": self.auto_deployable,
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "WARNING":         "OPERATOR REVIEW REQUIRED — do NOT auto-apply.",
        }


def plan_deployment(
    scorecard: ScorecardResult,
    recommendation: RecommendationPackage,
    policy: Policy,
    logs_dir: Path = LOGS_DIR,
) -> DeploymentPlan:
    """Generate a safe deployment plan based on the scorecard decision.

    Writes the plan to logs/deployment_plan.json.
    The operator must review and apply changes manually.
    """
    state = load_state()
    current_phase = state.get("phase", "paper")
    params = recommendation.proposed_params
    caps   = policy.micro_live_caps

    env_overrides: dict[str, Any] = {}
    instructions:  list[str]      = []
    action   = "reject"
    to_phase = current_phase

    # ------------------------------------------------------------------
    if scorecard.decision == Decision.REJECT:
        action = "reject"
        instructions.append("Scorecard: REJECT — no parameter changes recommended.")
        instructions.append("Address the following violations before re-running a cycle:")
        for r in scorecard.reasons:
            instructions.append(f"  * {r}")
        instructions.append("")
        instructions.append(
            "Next step: collect more paper trades, then re-run .\\run_supervised_cycle.ps1"
        )

    # ------------------------------------------------------------------
    elif scorecard.decision == Decision.PAPER_PROMOTE:
        action    = "stay_paper"
        to_phase  = "paper"
        env_overrides = {**params, "DRY_RUN": "1", "QUANT_MODE_ENABLED": "1"}
        instructions.append("Scorecard: PAPER_PROMOTE — apply updated params in paper mode.")
        instructions.append("NOT ready for micro-live. Continue paper validation.")
        instructions.append("")
        instructions.append("Apply in PowerShell before your next paper session:")
        for k, v in env_overrides.items():
            instructions.append(f"  $env:{k} = '{v}'")
        instructions.append("")
        instructions.append("Or add the parameter changes to your .env file (keep DRY_RUN=1).")

    # ------------------------------------------------------------------
    elif scorecard.decision == Decision.MICRO_LIVE_PROMOTE:

        if current_phase == "paper":
            action   = "promote_micro_live"
            to_phase = "micro_live"
            env_overrides = {
                **params,
                "DRY_RUN":                    "0",
                "QUANT_MODE_ENABLED":         "1",
                "RISK_DAILY_LOSS_LIMIT":      str(caps.get("max_daily_loss_usdc", 10.0)),
                "RISK_MAX_DRAWDOWN":          str(caps.get("max_drawdown_usdc", 20.0)),
                "RISK_MAX_MARKET_EXPOSURE":   str(caps.get("max_market_exposure_usdc", 25.0)),
                "RISK_MAX_PORTFOLIO_EXPOSURE":str(caps.get("max_portfolio_exposure_usdc", 50.0)),
                "SIZE_MAX_USDC":              str(caps.get("size_max_usdc", 10.0)),
                "SIZE_MIN_USDC":              str(caps.get("size_min_usdc", 2.0)),
            }
            instructions += [
                "=" * 60,
                "  MICRO-LIVE PROMOTION — OPERATOR ACTION REQUIRED",
                "=" * 60,
                "",
                "IMPORTANT SAFETY CHECKLIST (verify before applying):",
                "  1. PRIVATE_KEY and WALLET_ADDRESS are set correctly in .env",
                "  2. Wallet has sufficient USDC (recommend ≥ $50 buffer above caps)",
                "  3. Caps are enforced: $10/trade max, $50 portfolio max",
                "  4. Kill-switch Tier-3 threshold is configured conservatively",
                "  5. Monitor live for at least 30 minutes before stepping away",
                "  6. This is real money — losses are real",
                "",
                "To apply (PowerShell):",
            ]
            for k, v in env_overrides.items():
                instructions.append(f"  $env:{k} = '{v}'")
            instructions += [
                "",
                "Then run: python bot.py",
                "",
                "To stay in paper mode instead: set $env:DRY_RUN = '1'",
            ]

        elif current_phase == "micro_live":
            action   = "manual_review"
            to_phase = "scale_candidate"
            instructions += [
                "=" * 60,
                "  SCALE CANDIDATE — MANUAL SIGN-OFF REQUIRED",
                "=" * 60,
                "",
                "Bot has met micro-live promotion criteria.",
                "Scaling beyond micro-live is NOT automated.",
                "",
                "Review full scorecard, logs, and risk limits before proceeding.",
                "Gradually raise caps in .env — do not make large jumps.",
            ]

        else:
            action   = "manual_review"
            to_phase = current_phase
            instructions.append(
                "Already at scale_candidate or beyond. Operator review required."
            )

    plan = DeploymentPlan(
        action          = action,
        from_phase      = current_phase,
        to_phase        = to_phase,
        env_overrides   = env_overrides,
        micro_live_caps = caps,
        instructions    = instructions,
        auto_deployable = False,
    )

    # Persist plan for operator reference
    plan_path = logs_dir / "deployment_plan.json"
    logs_dir.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan.to_dict(), indent=2))

    return plan
