"""
Recommendation package generator.

Placeholder interface for the Claude-generated patch workflow.

Current implementation reads analytics output (opt_best.json, perf.json) and
proposes parameter changes based on the optimizer's best configuration.

To integrate with the Claude API later, replace the body of `propose()` with a
call to a Claude client that receives the analytics summary and returns structured
parameter recommendations. The RecommendationPackage interface stays the same.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGS_DIR = Path(__file__).parent.parent / "logs"

# Default parameters used when no optimizer output is available.
_DEFAULTS: dict[str, Any] = {
    "SIGNAL_CONFIDENCE_THRESHOLD": 0.40,
    "EXEC_MIN_EDGE": 0.010,
    "VOL_TARGET_SCALE": 1.0,
    "MM_SPREAD_GATE": 0.0,
}


@dataclass
class RecommendationPackage:
    """Parameter change recommendation with supporting rationale.

    Attributes
    ----------
    proposed_params     : env-var style dict, e.g. {"SIGNAL_CONFIDENCE_THRESHOLD": 0.45}
    baseline_params     : current / default values for comparison
    expected_improvement: expected metric deltas from the optimizer
    rationale           : human-readable list of reasons
    source              : "param_optimizer" | "claude_api" | "manual"
    data_summary        : snapshot of ingested analytics state
    """
    proposed_params: dict[str, Any]
    baseline_params: dict[str, Any]
    expected_improvement: dict[str, Any]
    rationale: list[str]
    source: str = "param_optimizer"
    data_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "proposed_params": self.proposed_params,
            "baseline_params": self.baseline_params,
            "expected_improvement": self.expected_improvement,
            "rationale": self.rationale,
            "data_summary": self.data_summary,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_opt_best(logs_dir: Path) -> dict | None:
    path = logs_dir / "opt_best.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list) and data:
            return data[0]  # top-ranked config
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _load_perf(logs_dir: Path) -> dict:
    path = logs_dir / "perf.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def propose(
    analytics_data: dict[str, Any] | None = None,
    logs_dir: Path = LOGS_DIR,
) -> RecommendationPackage:
    """Generate a recommendation package from available analytics data.

    Parameters
    ----------
    analytics_data : optional pre-loaded dict with keys:
        - perf     : dict from perf.json
        - opt_best : dict (top config) from opt_best.json
        - trades   : list of trade dicts from quant_trades.csv (unused here;
                     passed through for orchestrator convenience)
    logs_dir : directory containing log files (default: ../logs relative to module)

    Returns
    -------
    RecommendationPackage with proposed params and rationale.

    Claude API integration point
    ----------------------------
    Replace the block below with:
        response = anthropic_client.messages.create(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": build_prompt(analytics_data)}],
        )
        return parse_claude_response(response)
    """
    data = analytics_data or {}
    perf = data.get("perf") or _load_perf(logs_dir)
    opt_best = data.get("opt_best") or _load_opt_best(logs_dir)

    baseline = dict(_DEFAULTS)
    proposed = dict(_DEFAULTS)
    rationale: list[str] = []
    expected: dict[str, Any] = {}

    if opt_best:
        proposed = {
            "SIGNAL_CONFIDENCE_THRESHOLD": _safe_float(
                opt_best.get("signal_confidence_threshold"),
                baseline["SIGNAL_CONFIDENCE_THRESHOLD"],
            ),
            "EXEC_MIN_EDGE": _safe_float(
                opt_best.get("exec_min_edge"),
                baseline["EXEC_MIN_EDGE"],
            ),
            "VOL_TARGET_SCALE": _safe_float(
                opt_best.get("vol_target_scale"),
                baseline["VOL_TARGET_SCALE"],
            ),
            "MM_SPREAD_GATE": _safe_float(
                opt_best.get("mm_spread_gate"),
                baseline["MM_SPREAD_GATE"],
            ),
        }
        expected = {
            "n_trades":        opt_best.get("n_trades"),
            "win_rate_pct":    opt_best.get("win_rate_pct"),
            "sharpe_like":     opt_best.get("sharpe_like"),
            "total_net_pnl":   opt_best.get("total_net_pnl"),
            "max_drawdown":    opt_best.get("max_drawdown"),
            "objective_score": opt_best.get("objective"),
        }
        rationale.append(
            f"Optimizer best config: conf={proposed['SIGNAL_CONFIDENCE_THRESHOLD']}, "
            f"edge={proposed['EXEC_MIN_EDGE']}, vol_scale={proposed['VOL_TARGET_SCALE']}, "
            f"spread_gate={proposed['MM_SPREAD_GATE']}"
        )
        if opt_best.get("win_rate_pct"):
            rationale.append(f"Expected win rate: {opt_best['win_rate_pct']:.1f}%")
        if opt_best.get("sharpe_like"):
            rationale.append(f"Expected Sharpe-like: {opt_best['sharpe_like']:.3f}")
        if opt_best.get("objective"):
            rationale.append(f"Optimizer objective score: {opt_best['objective']:.4f}")
    else:
        rationale.append("No optimizer output found — using default parameters.")
        rationale.append(
            "Tip: run `python -m analytics.param_optimizer` first to generate opt_best.json."
        )

    # Conservative tightening when health is degraded
    health = str(perf.get("health", "NORMAL"))
    # kill_switch_tier may be an int (0-3) or a string label ("NORMAL") — normalise to int
    _raw_ks = perf.get("kill_switch_tier", 0)
    try:
        ks_tier = int(_raw_ks)
    except (TypeError, ValueError):
        ks_tier = 0

    if health in ("DEGRADED", "SAFE_MODE", "CIRCUIT_BREAKER"):
        proposed["SIGNAL_CONFIDENCE_THRESHOLD"] = min(
            0.70, proposed["SIGNAL_CONFIDENCE_THRESHOLD"] + 0.10
        )
        proposed["EXEC_MIN_EDGE"] = min(0.030, proposed["EXEC_MIN_EDGE"] * 1.5)
        rationale.append(
            f"Health state is {health} — tightening thresholds conservatively."
        )

    if ks_tier >= 2:
        proposed["SIGNAL_CONFIDENCE_THRESHOLD"] = min(
            0.70, proposed["SIGNAL_CONFIDENCE_THRESHOLD"] + 0.05
        )
        rationale.append(f"Kill-switch tier {ks_tier} — raising confidence threshold.")

    data_summary = {
        "health": health,
        "kill_switch_tier": ks_tier,
        "mode": perf.get("mode", "unknown"),
        "balance_usdc": perf.get("balance_usdc", 0),
        "opt_best_available": opt_best is not None,
    }

    return RecommendationPackage(
        proposed_params=proposed,
        baseline_params=baseline,
        expected_improvement=expected,
        rationale=rationale,
        source="param_optimizer" if opt_best else "defaults",
        data_summary=data_summary,
    )
