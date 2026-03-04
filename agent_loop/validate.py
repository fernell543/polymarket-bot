"""
Validation suite.

Replays existing trade data through proposed parameters to estimate
performance — mirrors the logic in analytics.param_optimizer but for a
single specific configuration.

Can optionally launch a short live paper session (run_paper=True), but the
default is pure replay so cycles complete in seconds without spawning a subprocess.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_loop.propose_patch import RecommendationPackage

LOGS_DIR = Path(__file__).parent.parent / "logs"

FEE_PCT  = 0.02   # Polymarket LP/taker fee
SLIP_PCT = 0.002  # assumed slippage


@dataclass
class ValidationResult:
    """Metrics from validating proposed parameters against trade data."""
    n_trades:               int   = 0
    n_qualifying:           int   = 0     # trades that pass proposed param filters
    selection_rate:         float = 0.0
    win_rate_pct:           float = 0.0
    mean_edge:              float = 0.0
    sharpe_like:            float = 0.0
    total_net_pnl:          float = 0.0
    max_drawdown:           float = 0.0
    fill_ratio:             float = 0.0
    adverse_selection_pct:  float = 0.0
    net_edge_bps:           float = 0.0
    data_source:            str   = "replay"
    warning:                str   = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades":              self.n_trades,
            "n_qualifying":          self.n_qualifying,
            "selection_rate":        round(self.selection_rate, 3),
            "win_rate_pct":          round(self.win_rate_pct, 2),
            "mean_edge":             round(self.mean_edge, 5),
            "sharpe_like":           round(self.sharpe_like, 4),
            "total_net_pnl":         round(self.total_net_pnl, 4),
            "max_drawdown":          round(self.max_drawdown, 4),
            "fill_ratio":            round(self.fill_ratio, 3),
            "adverse_selection_pct": round(self.adverse_selection_pct, 6),
            "net_edge_bps":          round(self.net_edge_bps, 2),
            "data_source":           self.data_source,
            "warning":               self.warning,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _nonempty_float(v: Any) -> float | None:
    """Return float only if the value is a non-empty string / numeric."""
    if v is None or str(v).strip() == "":
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _replay(trades: list[dict], params: dict[str, Any]) -> ValidationResult:
    """Replay trade list through proposed param filters and compute metrics."""
    r = ValidationResult()
    r.n_trades = len(trades)

    if not trades:
        r.warning = "No trade data available for replay."
        return r

    conf_thresh  = params.get("SIGNAL_CONFIDENCE_THRESHOLD", 0.40)
    min_edge     = params.get("EXEC_MIN_EDGE", 0.010)
    spread_gate  = params.get("MM_SPREAD_GATE", 0.0)

    filled    = [t for t in trades if t.get("status") in ("filled", "simulated")]
    cancelled = [t for t in trades if t.get("status") in ("cancelled", "timed_out", "expired")]

    total_placed = len(filled) + len(cancelled)
    r.fill_ratio = len(filled) / total_placed if total_placed else 0.0

    qualifying: list[dict] = []
    for t in filled:
        conf       = _nonempty_float(t.get("signal_confidence"))
        exp_edge   = _nonempty_float(t.get("expected_edge"))
        spread     = _nonempty_float(t.get("spread_at_entry"))

        if conf is not None and conf < conf_thresh:
            continue
        if exp_edge is not None and exp_edge < min_edge:
            continue
        if spread is not None and spread < spread_gate:
            continue
        qualifying.append(t)

    r.n_qualifying  = len(qualifying)
    r.selection_rate = len(qualifying) / len(filled) if filled else 0.0

    if not qualifying:
        r.warning = (
            f"No trades qualify under proposed params "
            f"(conf≥{conf_thresh}, edge≥{min_edge}, spread≥{spread_gate})."
        )
        return r

    edges:     list[float] = []
    net_pnls:  list[float] = []
    exp_edges: list[float] = []
    rea_edges: list[float] = []

    for t in qualifying:
        exp_edge      = _nonempty_float(t.get("expected_edge"))
        realized_edge = _nonempty_float(t.get("realized_edge"))
        size          = _safe_float(t.get("size_usdc"), 10.0)

        edge_for_pnl = realized_edge if realized_edge is not None else (exp_edge or 0.0)
        net_pnl = (edge_for_pnl - FEE_PCT - SLIP_PCT) * size
        net_pnls.append(net_pnl)

        if exp_edge is not None:
            exp_edges.append(exp_edge)
        if realized_edge is not None:
            rea_edges.append(realized_edge)
            edges.append(realized_edge)
        elif exp_edge is not None:
            edges.append(exp_edge)

    if edges:
        r.mean_edge  = sum(edges) / len(edges)
        std = (
            math.sqrt(sum((e - r.mean_edge) ** 2 for e in edges) / len(edges))
            if len(edges) > 1 else 0.0
        )
        r.sharpe_like  = r.mean_edge / std if std > 1e-9 else 0.0
        r.net_edge_bps = (r.mean_edge - FEE_PCT - SLIP_PCT) * 10_000

    if net_pnls:
        r.total_net_pnl = sum(net_pnls)
        r.win_rate_pct  = 100.0 * sum(1 for p in net_pnls if p >= 0) / len(net_pnls)

        peak = cum = max_dd = 0.0
        for p in net_pnls:
            cum  += p
            peak  = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        r.max_drawdown = max_dd

    if exp_edges and rea_edges and len(exp_edges) == len(rea_edges):
        r.adverse_selection_pct = sum(
            e - rv for e, rv in zip(exp_edges, rea_edges)
        ) / len(exp_edges)

    return r


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def validate(
    recommendation: RecommendationPackage,
    trades: list[dict] | None = None,
    logs_dir: Path = LOGS_DIR,
) -> ValidationResult:
    """Validate proposed parameters against available trade data (replay mode).

    Parameters
    ----------
    recommendation : RecommendationPackage from propose_patch.propose()
    trades         : pre-loaded trade list; reads quant_trades.csv if None
    logs_dir       : directory containing log files

    Returns
    -------
    ValidationResult with replay metrics.
    """
    if trades is None:
        csv_path = logs_dir / "quant_trades.csv"
        if csv_path.exists():
            with open(csv_path, newline="") as f:
                trades = list(csv.DictReader(f))
        else:
            trades = []

    result = _replay(trades, recommendation.proposed_params)

    if result.n_trades < 5:
        result.warning = (
            f"Only {result.n_trades} trades in quant_trades.csv — replay metrics unreliable. "
            "Run: .\\run_paper_super.ps1 (QUANT_MODE_ENABLED=1) to collect data first."
        )

    return result
