"""
Scorecard computation.

Maps ValidationResult + live perf snapshot → promotion decision
(REJECT / PAPER_PROMOTE / MICRO_LIVE_PROMOTE).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_loop.policy import Policy, PolicyViolation
from agent_loop.validate import ValidationResult


class Decision(str, Enum):
    REJECT             = "REJECT"
    PAPER_PROMOTE      = "PAPER_PROMOTE"
    MICRO_LIVE_PROMOTE = "MICRO_LIVE_PROMOTE"


@dataclass
class ScorecardMetrics:
    n_trades:               int   = 0
    win_rate_pct:           float = 0.0
    mean_edge_pct:          float = 0.0
    sharpe_like:            float = 0.0
    total_net_pnl:          float = 0.0
    max_drawdown_usdc:      float = 0.0
    fill_ratio:             float = 0.0
    adverse_selection_pct:  float = 0.0
    net_edge_bps:           float = 0.0
    daily_loss_usdc:        float = 0.0
    portfolio_exposure_usdc: float = 0.0
    kill_switch_tier:       int   = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades":               self.n_trades,
            "win_rate_pct":           round(self.win_rate_pct, 2),
            "mean_edge_pct":          round(self.mean_edge_pct, 5),
            "sharpe_like":            round(self.sharpe_like, 4),
            "total_net_pnl":          round(self.total_net_pnl, 4),
            "max_drawdown_usdc":      round(self.max_drawdown_usdc, 4),
            "fill_ratio":             round(self.fill_ratio, 3),
            "adverse_selection_pct":  round(self.adverse_selection_pct, 6),
            "net_edge_bps":           round(self.net_edge_bps, 2),
            "daily_loss_usdc":        round(self.daily_loss_usdc, 4),
            "portfolio_exposure_usdc": round(self.portfolio_exposure_usdc, 4),
            "kill_switch_tier":       self.kill_switch_tier,
        }


@dataclass
class ScorecardResult:
    decision:        Decision
    metrics:         ScorecardMetrics
    violations:      list[PolicyViolation]
    reasons:         list[str]
    composite_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision":        self.decision.value,
            "composite_score": round(self.composite_score, 4),
            "metrics":         self.metrics.to_dict(),
            "violations":      [v.to_dict() for v in self.violations],
            "reasons":         self.reasons,
        }


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _composite(metrics: ScorecardMetrics, weights: dict[str, float]) -> float:
    """Normalised composite score in [0, 1]."""
    score = 0.0
    # win_rate: 45%→0, 70%→1
    score += weights.get("win_rate", 0.25) * max(0.0, min(1.0, (metrics.win_rate_pct - 45) / 25))
    # sharpe_like: 0→0, 2→1
    score += weights.get("sharpe_like", 0.30) * max(0.0, min(1.0, metrics.sharpe_like / 2.0))
    # net_pnl: -50→0, +50→1
    score += weights.get("net_pnl", 0.20) * max(0.0, min(1.0, (metrics.total_net_pnl + 50) / 100))
    # fill_ratio: direct
    score += weights.get("fill_ratio", 0.15) * max(0.0, min(1.0, metrics.fill_ratio))
    # adverse selection: 0%→1, 2%→0
    adv_ok = max(0.0, 1.0 - metrics.adverse_selection_pct / 0.02)
    score += weights.get("adverse_sel", 0.10) * adv_ok
    return score


def compute_scorecard(
    val_result: ValidationResult,
    perf: dict[str, Any],
    policy: Policy,
) -> ScorecardResult:
    """Compute full scorecard and return a promotion decision.

    Parameters
    ----------
    val_result : ValidationResult from validate.validate()
    perf       : dict from perf.json (may be empty)
    policy     : Policy loaded from policy.json

    Returns
    -------
    ScorecardResult with decision, metrics, violations, and reasons.
    """
    m = ScorecardMetrics(
        n_trades               = val_result.n_qualifying,
        win_rate_pct           = val_result.win_rate_pct,
        mean_edge_pct          = val_result.mean_edge,
        sharpe_like            = val_result.sharpe_like,
        total_net_pnl          = val_result.total_net_pnl,
        max_drawdown_usdc      = val_result.max_drawdown,
        fill_ratio             = val_result.fill_ratio,
        adverse_selection_pct  = val_result.adverse_selection_pct,
        net_edge_bps           = val_result.net_edge_bps,
        daily_loss_usdc        = abs(_safe_float(perf.get("risk_daily_pnl", 0))),
        portfolio_exposure_usdc = _safe_float(perf.get("risk_portfolio_exposure", 0)),
        kill_switch_tier       = _safe_int(perf.get("kill_switch_tier", 0)),
    )

    reasons: list[str]          = []
    violations: list[PolicyViolation] = []

    # --- Hard constraint check (fail-fast) -----------------------------------
    hard_metrics = {
        "daily_loss_usdc":          m.daily_loss_usdc,
        "drawdown_usdc":            m.max_drawdown_usdc,
        "max_market_exposure_usdc": m.portfolio_exposure_usdc,  # best proxy
        "portfolio_exposure_usdc":  m.portfolio_exposure_usdc,
        "net_edge_bps":             m.net_edge_bps,
        "n_trades":                 float(m.n_trades),
        "kill_switch_tier":         float(m.kill_switch_tier),
    }
    hard_violations = policy.check_hard_constraints(hard_metrics)
    if hard_violations:
        violations.extend(hard_violations)
        for v in hard_violations:
            reasons.append(f"HARD FAIL: {v.message}")
        return ScorecardResult(Decision.REJECT, m, violations, reasons, 0.0)

    # --- Scorecard gate check ------------------------------------------------
    gate_metrics = {
        "win_rate_pct":          m.win_rate_pct,
        "sharpe_like":           m.sharpe_like,
        "fill_ratio":            m.fill_ratio,
        "adverse_selection_pct": m.adverse_selection_pct,
    }
    micro_violations = policy.check_promotion_gates("micro_live_promote", gate_metrics)
    paper_violations = policy.check_promotion_gates("paper_promote", gate_metrics)

    composite = _composite(m, policy.scorecard_weights)

    if not paper_violations and not micro_violations:
        decision = Decision.MICRO_LIVE_PROMOTE
        reasons.append("All micro-live and paper-promote gates passed.")
    elif not paper_violations:
        decision = Decision.PAPER_PROMOTE
        reasons.append("Paper-promote gates passed; micro-live gates not yet met.")
        for v in micro_violations:
            reasons.append(f"  Micro-live gate: {v.message}")
        violations.extend(micro_violations)
    else:
        decision = Decision.REJECT
        reasons.append("Paper-promote gates failed.")
        for v in paper_violations:
            reasons.append(f"  Paper gate: {v.message}")
        violations.extend(paper_violations)

    if val_result.warning:
        reasons.insert(0, f"Warning: {val_result.warning}")

    return ScorecardResult(decision, m, violations, reasons, composite)
