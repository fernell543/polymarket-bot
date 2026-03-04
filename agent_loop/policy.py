"""
Policy guardrails loader and enforcer.

Hard constraints in policy.json cannot be auto-relaxed by the orchestrator.
Any violation immediately yields a REJECT decision with no further checks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_FILE = Path(__file__).parent / "policy.json"


@dataclass
class PolicyViolation:
    constraint: str
    value: float
    limit: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint": self.constraint,
            "value": self.value,
            "limit": self.limit,
            "message": self.message,
        }


@dataclass
class Policy:
    hard: dict[str, Any]
    scorecard_thresholds: dict[str, Any]
    scorecard_weights: dict[str, float]
    micro_live_caps: dict[str, float]
    promotion_ladder: list[dict]

    @classmethod
    def load(cls, path: Path = POLICY_FILE) -> "Policy":
        if not path.exists():
            raise FileNotFoundError(f"Policy file not found: {path}")
        data = json.loads(path.read_text())
        return cls(
            hard=data.get("hard_constraints", {}),
            scorecard_thresholds=data.get("scorecard_thresholds", {}),
            scorecard_weights=data.get("scorecard_weights", {}),
            micro_live_caps=data.get("micro_live_caps", {}),
            promotion_ladder=data.get("promotion_ladder", []),
        )

    def check_hard_constraints(self, metrics: dict[str, Any]) -> list[PolicyViolation]:
        """Return list of hard constraint violations. Empty list = all pass."""
        violations: list[PolicyViolation] = []

        def _hi(key: str, val: float | None, limit_key: str) -> None:
            """Fail if value exceeds limit (higher is bad)."""
            limit = self.hard.get(limit_key)
            if val is None or limit is None:
                return
            if val > limit:
                violations.append(PolicyViolation(key, val, limit,
                    f"{key} = {val:.3f} exceeds limit {limit:.3f}"))

        def _lo(key: str, val: float | None, limit_key: str) -> None:
            """Fail if value is below minimum (lower is bad)."""
            limit = self.hard.get(limit_key)
            if val is None or limit is None:
                return
            if val < limit:
                violations.append(PolicyViolation(key, val, limit,
                    f"{key} = {val:.3f} below minimum {limit:.3f}"))

        _hi("daily_loss_usdc",          metrics.get("daily_loss_usdc"),          "max_daily_loss_usdc")
        _hi("drawdown_usdc",            metrics.get("drawdown_usdc"),            "max_drawdown_usdc")
        _hi("max_market_exposure_usdc", metrics.get("max_market_exposure_usdc"), "max_market_exposure_usdc")
        _hi("portfolio_exposure_usdc",  metrics.get("portfolio_exposure_usdc"),  "max_portfolio_exposure_usdc")
        _lo("net_edge_bps",             metrics.get("net_edge_bps"),             "min_net_edge_bps")
        _lo("n_trades",                 metrics.get("n_trades"),                 "min_paper_trades")

        try:
            ks = int(metrics.get("kill_switch_tier") or 0)
        except (TypeError, ValueError):
            ks = 0
        max_ks = int(self.hard.get("max_kill_switch_tier", 2))
        if ks > max_ks:
            violations.append(PolicyViolation(
                "kill_switch_tier", ks, max_ks,
                f"Kill-switch tier {ks} triggered (max allowed: {max_ks})",
            ))

        return violations

    def check_promotion_gates(
        self, level: str, metrics: dict[str, Any]
    ) -> list[PolicyViolation]:
        """Check scorecard thresholds for a given promotion level."""
        thresholds = self.scorecard_thresholds.get(level, {})
        violations: list[PolicyViolation] = []

        def _lo(key: str, val: float | None, t_key: str) -> None:
            t = thresholds.get(t_key)
            if val is None or t is None:
                return
            if val < t:
                violations.append(PolicyViolation(key, val, t,
                    f"{key} = {val:.4f} below threshold {t:.4f}"))

        def _hi(key: str, val: float | None, t_key: str) -> None:
            t = thresholds.get(t_key)
            if val is None or t is None:
                return
            if val > t:
                violations.append(PolicyViolation(key, val, t,
                    f"{key} = {val:.4f} exceeds threshold {t:.4f}"))

        _lo("win_rate_pct",          metrics.get("win_rate_pct"),          "win_rate_pct")
        _lo("sharpe_like",           metrics.get("sharpe_like"),           "sharpe_like")
        _lo("fill_ratio",            metrics.get("fill_ratio"),            "fill_ratio")
        _hi("adverse_selection_pct", metrics.get("adverse_selection_pct"), "adverse_selection_pct")

        return violations
