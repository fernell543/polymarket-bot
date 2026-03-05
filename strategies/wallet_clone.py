"""
Wallet Clone Strategy
======================
Approximates the trading behavior of a target Polymarket wallet by:
  1. Loading an inferred behavioral profile (from analytics/clone_profile.py).
  2. Scoring live markets against the profile (category, price range, timing).
  3. Filtering by REALIZED live edge (after slippage, partial fill, spread stability).
  4. Executing the highest-scoring opportunity once per cycle.
  5. Using the shared PositionSizer for risk management.

Live-first changes vs paper version:
  - Scoring now incorporates LiveRealism.realized_edge (not theoretical).
  - scan_once() enforces LIVE_MIN_DEPTH_USDC and spread stability pre-filter.
  - execute() uses avoid-chase and staircase size caps.
  - Scoring weights shift: realized_edge_score (0.25) replaces naive price_score.

This is a behavioral approximation — it does NOT access the target wallet's
private signals, intent, or future actions.  It can only replicate observable
patterns from public historical data.

Config flags (all env-overridable):
  CLONE_ENABLED=1               Master switch (default 0)
  CLONE_WALLET=0x...            Target wallet address
  CLONE_PROFILE_PATH=logs/...   Path to profile JSON (auto-derived if not set)
  CLONE_AGGRESSIVENESS=1.0      Multiplier on size (0.1–2.0, default 1.0)
  CLONE_SCORE_THRESHOLD=0.40    Min score to open a position (0–1)
  CLONE_MAX_OPEN_POSITIONS=3    Max concurrent clone positions
  CLONE_POLL_INTERVAL=60        Seconds between scans
  CLONE_BIAS_ENABLED=1          Apply YES/NO directional bias from profile
  LIVE_DEPLOY_MODE=staircase_A  Stage-specific size/position caps
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import config
from client import PolymarketClient, Market
from execution.live_realism import LiveRealism

if TYPE_CHECKING:
    from health_state import HealthState
    from risk.sizing import PositionSizer

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config shim — reads env vars with defaults
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

CLONE_ENABLED           = os.getenv("CLONE_ENABLED", "0") == "1"
CLONE_WALLET            = os.getenv("CLONE_WALLET", "").lower()
CLONE_PROFILE_PATH      = os.getenv("CLONE_PROFILE_PATH", "")
CLONE_AGGRESSIVENESS    = _env_float("CLONE_AGGRESSIVENESS",  1.0)
CLONE_SCORE_THRESHOLD   = _env_float("CLONE_SCORE_THRESHOLD", 0.40)
CLONE_MAX_OPEN_POSITIONS = _env_int("CLONE_MAX_OPEN_POSITIONS", 3)
CLONE_POLL_INTERVAL     = _env_int("CLONE_POLL_INTERVAL",     60)
CLONE_BIAS_ENABLED      = os.getenv("CLONE_BIAS_ENABLED", "1") == "1"

# HF Hedge Mode — read from env (mirrors config.py, safe defaults)
CLONE_HF_MODE_ENABLED           = os.getenv("CLONE_HF_MODE_ENABLED",           "0") == "1"
CLONE_COMBINED_PRICE_MIN        = _env_float("CLONE_COMBINED_PRICE_MIN",        0.85)
CLONE_COMBINED_PRICE_MAX        = _env_float("CLONE_COMBINED_PRICE_MAX",        0.97)
CLONE_HEDGE_TIMEOUT_SECS        = _env_int("CLONE_HEDGE_TIMEOUT_SECS",          30)
CLONE_HEDGE_TAKER_FALLBACK_SECS = _env_int("CLONE_HEDGE_TAKER_FALLBACK_SECS",   10)
CLONE_MAX_SLIPPAGE_BPS          = _env_float("CLONE_MAX_SLIPPAGE_BPS",          50.0)
CLONE_CYCLE_INTERVAL_SECS       = _env_int("CLONE_CYCLE_INTERVAL_SECS",         5)
CLONE_HF_MAX_POSITIONS          = _env_int("CLONE_HF_MAX_POSITIONS",            5)
CLONE_HF_MIN_DEPTH_USDC         = _env_float("CLONE_MIN_DEPTH_USDC",            100.0)
CLONE_HF_PAPER_SIMULATE_PARTIAL = os.getenv("CLONE_HF_PAPER_SIMULATE_PARTIAL",  "0") == "1"

# Staircase size cap: derive from LIVE_DEPLOY_MODE
_STAGE_SIZE_CAP = {
    "staircase_A": config.LIVE_STAGE_A_MAX_SIZE_USDC,
    "staircase_B": config.LIVE_STAGE_B_MAX_SIZE_USDC,
    "staircase_C": config.LIVE_STAGE_C_MAX_SIZE_USDC,
    "production":  config.SIZE_MAX_USDC,
}
_STAGE_POS_CAP = {
    "staircase_A": config.LIVE_STAGE_A_MAX_POSITIONS,
    "staircase_B": config.LIVE_STAGE_B_MAX_POSITIONS,
    "staircase_C": config.LIVE_STAGE_C_MAX_POSITIONS,
    "production":  CLONE_MAX_OPEN_POSITIONS,
}
CLONE_LIVE_MAX_SIZE = _STAGE_SIZE_CAP.get(config.LIVE_DEPLOY_MODE, config.LIVE_STAGE_A_MAX_SIZE_USDC)
CLONE_LIVE_MAX_POSITIONS = _STAGE_POS_CAP.get(config.LIVE_DEPLOY_MODE, config.LIVE_STAGE_A_MAX_POSITIONS)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CloneProfile:
    """Parsed subset of the profile JSON used by the strategy."""
    wallet: str
    yes_min_price: float
    yes_max_price: float
    no_min_price: float
    no_max_price: float
    base_size_usdc: float
    min_size_usdc: float
    max_size_usdc: float
    preferred_categories: list[str]
    min_dte_days: float
    max_dte_days: float
    yes_bias: float           # 0..1  (>0.5 = YES-biased)
    bias_direction: str       # "yes" | "no"
    bias_strength: float      # 0..1
    trades_per_day_target: float
    confidence_floor: float   = 0.30
    edge_floor_bps: float     = 50.0

    @classmethod
    def load(cls, path: str) -> "CloneProfile":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        p = data.get("inferred_params", {})
        return cls(
            wallet               = data.get("wallet", ""),
            yes_min_price        = float(p.get("yes_entry_price_min", 0.05)),
            yes_max_price        = float(p.get("yes_entry_price_max", 0.85)),
            no_min_price         = float(p.get("no_entry_price_min",  0.05)),
            no_max_price         = float(p.get("no_entry_price_max",  0.85)),
            base_size_usdc       = float(p.get("base_size_usdc",  25.0)),
            min_size_usdc        = float(p.get("min_size_usdc",   10.0)),
            max_size_usdc        = float(p.get("max_size_usdc",  200.0)),
            preferred_categories = list(p.get("preferred_categories", [])),
            min_dte_days         = float(p.get("min_dte_days", 0.0)),
            max_dte_days         = float(p.get("max_dte_days", 365.0)),
            yes_bias             = float(p.get("yes_bias", 0.5)),
            bias_direction       = str(p.get("bias_direction", "yes")),
            bias_strength        = float(p.get("bias_strength", 0.0)),
            trades_per_day_target = float(p.get("trades_per_day_target", 1.0)),
            confidence_floor     = float(p.get("confidence_floor", 0.30)),
            edge_floor_bps       = float(p.get("edge_floor_bps", 50.0)),
        )

    def summary(self) -> str:
        return (
            f"wallet={self.wallet[:12]}…  "
            f"size=[{self.min_size_usdc:.0f},{self.max_size_usdc:.0f}] USDC  "
            f"yes_price=[{self.yes_min_price:.2f},{self.yes_max_price:.2f}]  "
            f"bias={self.bias_direction}({self.bias_strength:.2f})  "
            f"cats={self.preferred_categories[:3]}"
        )


@dataclass
class CloneOpportunity:
    market: Market
    token_id: str
    outcome: str        # "Yes" | "No"
    price: float
    score: float
    dte_days: Optional[float]
    category: str
    size_usdc: float    # pre-computed base size (before sizer adjustments)
    spread: float = 0.0
    depth_usdc: float = 500.0
    realized_edge: float = 0.0

    def __repr__(self) -> str:
        return (
            f"CloneOpp(score={self.score:.2f} outcome={self.outcome} "
            f"price={self.price:.3f} size={self.size_usdc:.1f} "
            f"re_edge={self.realized_edge:.4f} "
            f"dte={self.dte_days} cat={self.category})"
        )


@dataclass
class ClonePosition:
    condition_id: str
    question: str
    outcome: str
    token_id: str
    entry_price: float
    size_usdc: float
    opened_at: float = field(default_factory=time.time)
    order_id: str = ""


# ---------------------------------------------------------------------------
# HF Hedge Mode — data structures
# ---------------------------------------------------------------------------

class HFState(enum.Enum):
    """Per-position lifecycle states for paired YES/NO execution."""
    COLLECTING       = "collecting"        # both legs placed, awaiting fills
    PARTIALLY_FILLED = "partially_filled"  # one leg filled, hedge in progress
    HEDGED           = "hedged"            # both legs filled — success
    CLOSED           = "closed"            # settled / expired
    ABORTED          = "aborted"           # timeout or slippage abort


@dataclass
class HFLeg:
    """One side (YES or NO) of a paired HF position."""
    token_id:    str
    outcome:     str    # "Yes" | "No"
    order_id:    str   = ""
    filled:      bool  = False
    fill_price:  float = 0.0
    entry_price: float = 0.0   # price at order placement
    placed_at:   float = field(default_factory=time.time)
    filled_at:   float = 0.0
    is_maker:    bool  = True   # True=maker/limit, False=taker/market


@dataclass
class HFPosition:
    """Paired YES+NO position with full lifecycle state machine."""
    position_id:              str
    condition_id:             str
    question:                 str
    yes_leg:                  HFLeg
    no_leg:                   HFLeg
    state:                    HFState = HFState.COLLECTING
    opened_at:                float   = field(default_factory=time.time)
    state_changed_at:         float   = field(default_factory=time.time)
    combined_price_at_entry:  float   = 0.0   # yes_ask + no_ask at entry
    size_usdc:                float   = 0.0
    close_reason:             str     = ""
    net_edge_proxy:           float   = 0.0   # 1.0 - combined - 2*fee (gross)


@dataclass
class HFPairOpportunity:
    """A market where YES_ask + NO_ask falls within the combined-price band."""
    market:          "Market"   # type: ignore[name-defined]
    yes_token_id:    str
    no_token_id:     str
    yes_ask:         float
    no_ask:          float
    yes_bid:         float
    no_bid:          float
    combined_ask:    float     # yes_ask + no_ask
    yes_depth_usdc:  float
    no_depth_usdc:   float
    yes_spread:      float
    no_spread:       float
    edge_proxy:      float     # 1.0 - combined_ask - 2*fee (gross)

    def __repr__(self) -> str:
        return (
            f"HFPair(combined={self.combined_ask:.3f} edge={self.edge_proxy:.4f} "
            f"yes_ask={self.yes_ask:.3f} no_ask={self.no_ask:.3f} "
            f"q={self.market.question[:40]})"
        )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class WalletCloneStrategy:
    """
    Approximates target wallet trading behavior using inferred profile parameters.

    Scoring function (0–1), live-first version:
      category_score      (0.30) — market category matches preferred categories
      price_score         (0.25) — current best price within target's entry range
      timing_score        (0.20) — days-to-expiry within target's observed window
      bias_score          (0.10) — direction matches target's YES/NO bias
      realized_edge_score (0.15) — positive realized edge after live frictions

    Live filters (applied before scoring, short-circuit failures):
      - LIVE_MIN_DEPTH_USDC  — skip markets with thin depth
      - Spread stability     — skip on rapid spread widening
      - Book freshness       — skip stale price data
    """

    def __init__(self, client: PolymarketClient):
        self.client = client
        self._realism = LiveRealism()
        self._profile: Optional[CloneProfile] = None
        self._open_positions: dict[str, ClonePosition] = {}   # condition_id → position
        self._sizer: Optional["PositionSizer"] = None
        self._health: Optional["HealthState"] = None
        self._trades: list[dict] = []
        self._last_trade_time: float = 0.0
        self._cycle_count: int = 0
        self._errors: int = 0

        # HF Hedge Mode state
        self._hf_positions: dict[str, HFPosition] = {}   # pos_id → HFPosition
        self._hf_position_counter: int = 0
        # HF metrics
        self._hf_hedge_success: int = 0
        self._hf_aborted: int = 0
        self._hf_time_to_hedge: list[float] = []
        self._hf_net_edge_samples: list[float] = []

        self._load_profile()

    # ------------------------------------------------------------------
    # Injection API (matches other strategies)
    # ------------------------------------------------------------------

    def set_sizer(self, sizer: "PositionSizer") -> None:
        self._sizer = sizer

    def set_health(self, health: "HealthState") -> None:
        self._health = health

    # ------------------------------------------------------------------
    # Profile loading
    # ------------------------------------------------------------------

    def _load_profile(self) -> None:
        if not CLONE_ENABLED:
            log.debug("WalletClone: disabled (CLONE_ENABLED=0)")
            return

        profile_path = CLONE_PROFILE_PATH
        if not profile_path and CLONE_WALLET:
            profile_path = os.path.join("logs", f"clone_profile_{CLONE_WALLET}.json")

        if not profile_path or not os.path.exists(profile_path):
            log.warning(
                "WalletClone: profile not found at '%s'. "
                "Run scripts/clone_extract.py and analytics/clone_profile.py first.",
                profile_path or "(not set)",
            )
            return

        try:
            self._profile = CloneProfile.load(profile_path)
            log.info("WalletClone: profile loaded — %s", self._profile.summary())
        except Exception as exc:
            log.error("WalletClone: failed to load profile: %s", exc, exc_info=True)

    def reload_profile(self) -> None:
        """Hot-reload profile from disk (e.g. after re-extraction)."""
        self._load_profile()

    # ------------------------------------------------------------------
    # Scoring sub-components
    # ------------------------------------------------------------------

    def _category_score(self, market: Market) -> float:
        if not self._profile or not self._profile.preferred_categories:
            return 0.5
        q_lower = market.question.lower()
        for cat in self._profile.preferred_categories:
            if cat and cat in q_lower:
                return 1.0
        return 0.1

    def _price_score(self, price: float, outcome: str) -> float:
        if not self._profile:
            return 0.5
        if outcome.lower() == "yes":
            lo, hi = self._profile.yes_min_price, self._profile.yes_max_price
        else:
            lo, hi = self._profile.no_min_price, self._profile.no_max_price

        if lo >= hi:
            return 0.5

        if price < lo or price > hi:
            dist = min(abs(price - lo), abs(price - hi))
            return max(0.0, 0.5 - dist * 2.0)

        mid = (lo + hi) / 2.0
        dist_from_mid = abs(price - mid) / ((hi - lo) / 2.0)
        return 1.0 - 0.3 * dist_from_mid  # 0.7–1.0

    def _timing_score(self, market: Market) -> tuple[float, Optional[float]]:
        if not self._profile:
            return 0.5, None

        from datetime import datetime, timezone
        end_date = getattr(market, "end_date_iso", None)
        if not end_date:
            return 0.5, None

        try:
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    end_dt = datetime.strptime(end_date, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                return 0.5, None

            now = datetime.now(tz=timezone.utc)
            dte = (end_dt - now).total_seconds() / 86400.0
            if dte < 0:
                return 0.0, dte

            lo, hi = self._profile.min_dte_days, self._profile.max_dte_days
            if lo >= hi:
                return 0.5, dte

            if dte < lo or dte > hi:
                dist = min(abs(dte - lo), abs(dte - hi))
                return max(0.0, 0.5 - dist / max(hi - lo, 1.0)), dte

            mid = (lo + hi) / 2.0
            dist_from_mid = abs(dte - mid) / ((hi - lo) / 2.0)
            return 1.0 - 0.3 * dist_from_mid, dte
        except Exception:
            return 0.5, None

    def _bias_score(self, outcome: str) -> float:
        if not self._profile or not CLONE_BIAS_ENABLED:
            return 0.5
        direction_match = (outcome.lower() == self._profile.bias_direction)
        if direction_match:
            return 0.5 + 0.5 * self._profile.bias_strength
        else:
            return 0.5 - 0.5 * self._profile.bias_strength

    def _realized_edge_score(
        self,
        price: float,
        spread: float,
        size_usdc: float,
        depth_usdc: float,
    ) -> float:
        """
        0.0–1.0: how positive is the realized live edge after frictions?

        Uses LiveRealism to estimate slippage and partial fill, then scores:
          > 3% realized edge → 1.0
          > 0%               → 0.5 + (re_edge / 0.06)
          ≤ 0% (net negative) → 0.0
        """
        theoretical_edge = max(0.0, (1.0 - price) - config.POLYMARKET_FEE)
        slip  = self._realism.slippage_estimate(spread, size_usdc, depth_usdc)
        fill  = self._realism.partial_fill_fraction(size_usdc, depth_usdc)
        re    = self._realism.realized_edge(theoretical_edge, slip, fill)

        if re <= 0.0:
            return 0.0
        # Normalize: 3% = max useful edge → score=1.0
        score = min(1.0, re / 0.03)
        return round(score, 4)

    def _score_opportunity(
        self,
        market: Market,
        token_id: str,
        outcome: str,
        price: float,
        spread: float = 0.02,
        size_usdc: float = 25.0,
        depth_usdc: float = 500.0,
    ) -> tuple[float, Optional[float], float]:
        """
        Composite score (0–1), DTE, and realized edge.

        Live-first weights:
          category_score      × 0.30
          price_score         × 0.25
          timing_score        × 0.20
          bias_score          × 0.10
          realized_edge_score × 0.15
        """
        cat_s    = self._category_score(market)                              * 0.30
        price_s  = self._price_score(price, outcome)                         * 0.25
        timing_s, dte = self._timing_score(market)
        timing_s *= 0.20
        bias_s   = self._bias_score(outcome)                                 * 0.10
        re_score = self._realized_edge_score(price, spread, size_usdc, depth_usdc) * 0.15

        score = cat_s + price_s + timing_s + bias_s + re_score

        theoretical_edge = max(0.0, (1.0 - price) - config.POLYMARKET_FEE)
        slip = self._realism.slippage_estimate(spread, size_usdc, depth_usdc)
        fill = self._realism.partial_fill_fraction(size_usdc, depth_usdc)
        re_edge = self._realism.realized_edge(theoretical_edge, slip, fill)

        return score, dte, re_edge

    # ------------------------------------------------------------------
    # Market scanning
    # ------------------------------------------------------------------

    async def scan_once(self, markets: list[Market]) -> list[CloneOpportunity]:
        """
        Score all live markets and return ranked opportunities.

        Live filters applied before scoring:
          1. Skip markets with no/expired tokens.
          2. Skip markets already in open positions.
          3. Apply LIVE_MIN_DEPTH_USDC filter.
          4. Apply spread stability gate.
          5. Apply staircase position cap.
        """
        if not self._profile:
            return []

        effective_max_pos = min(CLONE_MAX_OPEN_POSITIONS, CLONE_LIVE_MAX_POSITIONS)
        if len(self._open_positions) >= effective_max_pos:
            log.debug(
                "WalletClone: max open positions reached (%d/%d)",
                len(self._open_positions), effective_max_pos,
            )
            return []

        opportunities: list[CloneOpportunity] = []
        open_cids = set(self._open_positions.keys())

        for market in markets:
            if market.closed or not market.active:
                continue
            if market.condition_id in open_cids:
                continue
            if not market.tokens:
                continue

            for token in market.tokens:
                token_id = token.get("token_id", "")
                outcome  = token.get("outcome", "")
                if not token_id or not outcome:
                    continue

                try:
                    bid, ask = await self.client.get_best_prices(token_id)
                    price = (bid + ask) / 2.0
                    if price <= 0.01 or price >= 0.99:
                        continue
                    spread = max(0.0, ask - bid)
                    depth_usdc = 500.0  # conservative default; upgraded below if available
                except Exception:
                    continue

                # Update realism layer with fresh book data
                self._realism.record_book_update(token_id)
                self._realism.record_spread(token_id, spread)

                # --- Live pre-filters ---
                if not config.DRY_RUN:
                    # Depth filter
                    if depth_usdc < config.LIVE_MIN_DEPTH_USDC:
                        continue

                    # Spread stability
                    if not self._realism.spread_is_stable(token_id, spread):
                        continue

                    # Book freshness
                    if not self._realism.book_is_fresh(token_id):
                        continue

                # Estimate base size (apply staircase cap)
                base_size = self._profile.base_size_usdc * CLONE_AGGRESSIVENESS
                base_size = max(self._profile.min_size_usdc, min(self._profile.max_size_usdc, base_size))
                base_size = min(base_size, CLONE_LIVE_MAX_SIZE)

                score, dte, re_edge = self._score_opportunity(
                    market, token_id, outcome, price, spread, base_size, depth_usdc,
                )
                if score < CLONE_SCORE_THRESHOLD:
                    continue

                # Live mode: also require positive realized edge
                if not config.DRY_RUN and re_edge <= 0:
                    log.debug(
                        "WalletClone: skipping %s %s — realized edge %.4f ≤ 0",
                        outcome, token_id[:8], re_edge,
                    )
                    continue

                category = "unknown"
                for cat in self._profile.preferred_categories:
                    if cat and cat in market.question.lower():
                        category = cat
                        break

                opportunities.append(CloneOpportunity(
                    market=market,
                    token_id=token_id,
                    outcome=outcome,
                    price=price,
                    score=score,
                    dte_days=dte,
                    category=category,
                    size_usdc=base_size,
                    spread=spread,
                    depth_usdc=depth_usdc,
                    realized_edge=re_edge,
                ))

        opportunities.sort(key=lambda o: o.score, reverse=True)
        if opportunities:
            log.info(
                "WalletClone: %d opportunities (top: %s)",
                len(opportunities), opportunities[0],
            )
        return opportunities

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, opp: CloneOpportunity) -> bool:
        """Place order for the best opportunity, respecting all live controls."""
        from health_state import HealthLevel

        # Re-fetch current price to check for chase
        try:
            bid, ask = await self.client.get_best_prices(opp.token_id)
            current_price = (bid + ask) / 2.0
            current_spread = max(0.0, ask - bid)
        except Exception:
            current_price = opp.price
            current_spread = opp.spread

        # Avoid-chase check (live mode only)
        if not config.DRY_RUN:
            chase_ok = self._realism.chase_allowed(
                signal_price=opp.price,
                current_price=current_price,
                spread=current_spread,
                urgency=0.5,
            )
            if not chase_ok:
                log.info(
                    "WalletClone: avoid-chase blocked %s %s (signal=%.3f current=%.3f)",
                    opp.outcome, opp.token_id[:8], opp.price, current_price,
                )
                return False

        # Apply PositionSizer
        size_usdc = opp.size_usdc
        if self._sizer is not None:
            edge_bps = max(50.0, opp.realized_edge * 10_000)
            health_level = self._health.level if self._health else HealthLevel.NORMAL
            sr = self._sizer.compute(
                edge_bps=edge_bps,
                confidence=opp.score,
                vol_regime="mid",
                health_level=health_level,
                label="WalletClone",
            )
            if sr.size_usdc == 0:
                log.info("WalletClone: sizer blocked (%s)", sr.reason)
                return False
            size_usdc = sr.size_usdc

        # Apply staircase size cap (hard, non-overridable)
        size_usdc = min(size_usdc, CLONE_LIVE_MAX_SIZE)

        log.info(
            "WalletClone: executing %s %s @ %.3f  size=%.2f USDC  "
            "score=%.2f  re_edge=%.4f  dte=%s  stage=%s",
            opp.outcome, opp.market.question[:50], current_price, size_usdc,
            opp.score, opp.realized_edge,
            f"{opp.dte_days:.1f}d" if opp.dte_days is not None else "N/A",
            config.LIVE_DEPLOY_MODE,
        )

        result = await self.client.place_market_order(opp.token_id, "BUY", size_usdc)

        self._trades.append({
            "ts":             time.time(),
            "condition_id":   opp.market.condition_id,
            "question":       opp.market.question[:80],
            "outcome":        opp.outcome,
            "price":          opp.price,
            "current_price":  current_price,
            "spread":         current_spread,
            "size_usdc":      size_usdc,
            "score":          opp.score,
            "realized_edge":  opp.realized_edge,
            "stage":          config.LIVE_DEPLOY_MODE,
            "order_id":       result.order_id if result else "",
            "success":        result.success if result else False,
        })

        if result and result.success:
            self._open_positions[opp.market.condition_id] = ClonePosition(
                condition_id=opp.market.condition_id,
                question=opp.market.question,
                outcome=opp.outcome,
                token_id=opp.token_id,
                entry_price=current_price,
                size_usdc=size_usdc,
                order_id=result.order_id,
            )
            self._last_trade_time = time.time()
            log.info("WalletClone: order placed — %s", result.order_id)
            return True
        else:
            err = result.error if result else "unknown"
            log.warning("WalletClone: order failed — %s", err)
            return False

    # ------------------------------------------------------------------
    # HF Hedge Mode — scanning
    # ------------------------------------------------------------------

    async def scan_hf_pairs(self, markets: list) -> list[HFPairOpportunity]:
        """
        Scan for YES+NO pairs within the combined-price target band.

        combined_ask = yes_ask + no_ask
        edge_proxy   = 1.0 - combined_ask - 2*POLYMARKET_FEE

        Returns list of HFPairOpportunity sorted by edge_proxy descending.

        Filters:
          1. Skip closed/inactive markets.
          2. Skip markets already in an open HF position.
          3. combined_ask ∈ [COMBINED_PRICE_MIN, COMBINED_PRICE_MAX].
          4. Per-leg depth ≥ CLONE_HF_MIN_DEPTH_USDC.
          5. Live: spread stability + book freshness per leg.
        """
        if not CLONE_HF_MODE_ENABLED or not self._profile:
            return []

        effective_max = min(CLONE_HF_MAX_POSITIONS, CLONE_LIVE_MAX_POSITIONS)
        if len(self._hf_positions) >= effective_max:
            log.debug(
                "CloneHF: max positions reached (%d/%d)", len(self._hf_positions), effective_max
            )
            return []

        open_cids: set[str] = {
            pos.condition_id
            for pos in self._hf_positions.values()
            if pos.state in (HFState.COLLECTING, HFState.PARTIALLY_FILLED)
        }

        pairs: list[HFPairOpportunity] = []

        for market in markets:
            if market.closed or not market.active:
                continue
            if market.condition_id in open_cids:
                continue
            if not market.tokens or len(market.tokens) < 2:
                continue

            # Find YES and NO tokens
            yes_token = no_token = None
            for tok in market.tokens:
                outcome_lower = tok.get("outcome", "").lower()
                if outcome_lower == "yes":
                    yes_token = tok
                elif outcome_lower == "no":
                    no_token = tok

            if not yes_token or not no_token:
                continue

            yes_token_id = yes_token.get("token_id", "")
            no_token_id  = no_token.get("token_id", "")
            if not yes_token_id or not no_token_id:
                continue

            # Fetch best prices for both legs
            try:
                yes_bid, yes_ask = await self.client.get_best_prices(yes_token_id)
                no_bid,  no_ask  = await self.client.get_best_prices(no_token_id)
            except Exception:
                continue

            if yes_ask <= 0.01 or yes_ask >= 0.99:
                continue
            if no_ask  <= 0.01 or no_ask  >= 0.99:
                continue

            combined_ask = yes_ask + no_ask

            # Combined-price band filter
            if combined_ask < CLONE_COMBINED_PRICE_MIN or combined_ask > CLONE_COMBINED_PRICE_MAX:
                continue

            yes_spread = max(0.0, yes_ask - yes_bid)
            no_spread  = max(0.0, no_ask  - no_bid)

            # Depth (conservative default — real depth would come from order-book query)
            yes_depth_usdc = 500.0
            no_depth_usdc  = 500.0

            # Per-leg depth filter
            if yes_depth_usdc < CLONE_HF_MIN_DEPTH_USDC or no_depth_usdc < CLONE_HF_MIN_DEPTH_USDC:
                continue

            # Update realism layer
            self._realism.record_book_update(yes_token_id)
            self._realism.record_book_update(no_token_id)
            self._realism.record_spread(yes_token_id, yes_spread)
            self._realism.record_spread(no_token_id,  no_spread)

            # Live-only spread stability + book freshness
            if not config.DRY_RUN:
                if (
                    not self._realism.spread_is_stable(yes_token_id, yes_spread)
                    or not self._realism.spread_is_stable(no_token_id, no_spread)
                    or not self._realism.book_is_fresh(yes_token_id)
                    or not self._realism.book_is_fresh(no_token_id)
                ):
                    continue

            edge_proxy = 1.0 - combined_ask - 2.0 * config.POLYMARKET_FEE

            pairs.append(HFPairOpportunity(
                market         = market,
                yes_token_id   = yes_token_id,
                no_token_id    = no_token_id,
                yes_ask        = yes_ask,
                no_ask         = no_ask,
                yes_bid        = yes_bid,
                no_bid         = no_bid,
                combined_ask   = combined_ask,
                yes_depth_usdc = yes_depth_usdc,
                no_depth_usdc  = no_depth_usdc,
                yes_spread     = yes_spread,
                no_spread      = no_spread,
                edge_proxy     = edge_proxy,
            ))

        pairs.sort(key=lambda p: p.edge_proxy, reverse=True)
        if pairs:
            log.info("CloneHF: %d pair opportunities (top: %s)", len(pairs), pairs[0])
        return pairs

    # ------------------------------------------------------------------
    # HF Hedge Mode — execution
    # ------------------------------------------------------------------

    async def execute_hf_pair(self, pair: HFPairOpportunity) -> Optional[str]:
        """
        Place YES and NO legs simultaneously for a paired HF position.

        Returns position_id on success, None if blocked.
        Initial state: COLLECTING.  monitor_hf_positions() steps the machine.
        """
        from health_state import HealthLevel

        if not self._profile:
            return None

        # --- Dynamic sizing via CloneSizer ---
        from risk.clone_sizer import compute_clone_size, CLONE_SIZE_MODE
        health_level = self._health.level if self._health else HealthLevel.NORMAL
        # Use per-leg minimum depth as a conservative estimate
        depth_usdc = min(pair.yes_depth_usdc, pair.no_depth_usdc)
        avg_spread = (pair.yes_spread + pair.no_spread) / 2.0
        csr = compute_clone_size(
            profile    = self._profile,
            edge_proxy = pair.edge_proxy,
            confidence = 0.70,          # HF pairs: fixed structural confidence
            depth_usdc = depth_usdc,
            spread     = avg_spread,
            health_level = health_level,
            global_max = CLONE_LIVE_MAX_SIZE,
        )
        if csr.blocked or csr.size_usdc == 0:
            log.info(
                "CloneHF: clone-sizer blocked  pos_candidate  reason=%s  mode=%s",
                csr.reason, CLONE_SIZE_MODE,
            )
            return None
        size_usdc = csr.size_usdc
        # Log full sizing trace for audit
        log.info(
            "CloneHF: size_decision  mode=%s  edge_proxy=%.4f  depth=%.0f  "
            "spread=%.4f  size=%.2f USDC  reason=%s",
            csr.mode, pair.edge_proxy, depth_usdc, avg_spread, size_usdc, csr.reason,
        )
        # Fallback to global sizer only if present (secondary guard)
        if self._sizer is not None:
            edge_bps = max(50.0, pair.edge_proxy * 10_000)
            sr = self._sizer.compute(
                edge_bps=edge_bps,
                confidence=0.70,
                vol_regime="low",
                health_level=health_level,
                label="CloneHF",
            )
            if sr.size_usdc == 0:
                log.info("CloneHF: global-sizer blocked (%s)", sr.reason)
                return None
            # Use the more conservative of the two
            size_usdc = min(size_usdc, sr.size_usdc)

        if size_usdc < self._profile.min_size_usdc:
            log.debug("CloneHF: size too small (%.2f < profile_min=%.2f)",
                      size_usdc, self._profile.min_size_usdc)
            return None

        pos_id = f"hf_{self._hf_position_counter:04d}_{uuid.uuid4().hex[:6]}"
        self._hf_position_counter += 1

        log.info(
            "CloneHF: opening pair  pos=%s  combined=%.3f  edge=%.4f  size=%.2f USDC  "
            "yes_ask=%.3f  no_ask=%.3f  q=%s",
            pos_id, pair.combined_ask, pair.edge_proxy, size_usdc,
            pair.yes_ask, pair.no_ask, pair.market.question[:50],
        )

        now = time.time()

        # Place both legs
        yes_result = await self.client.place_market_order(pair.yes_token_id, "BUY", size_usdc)
        no_result  = await self.client.place_market_order(pair.no_token_id,  "BUY", size_usdc)

        yes_ok = bool(yes_result and yes_result.success)
        no_ok  = bool(no_result  and no_result.success)

        yes_leg = HFLeg(
            token_id    = pair.yes_token_id,
            outcome     = "Yes",
            order_id    = (yes_result.order_id if yes_result else ""),
            filled      = yes_ok,
            fill_price  = pair.yes_ask if yes_ok else 0.0,
            entry_price = pair.yes_ask,
            placed_at   = now,
            filled_at   = now if yes_ok else 0.0,
            is_maker    = False,
        )
        no_leg = HFLeg(
            token_id    = pair.no_token_id,
            outcome     = "No",
            order_id    = (no_result.order_id if no_result else ""),
            filled      = no_ok,
            fill_price  = pair.no_ask if no_ok else 0.0,
            entry_price = pair.no_ask,
            placed_at   = now,
            filled_at   = now if no_ok else 0.0,
            is_maker    = False,
        )

        # Paper-mode partial-fill simulation (exercises hedge path)
        if config.DRY_RUN and CLONE_HF_PAPER_SIMULATE_PARTIAL:
            import random
            if random.random() < 0.35:
                no_leg.filled    = False
                no_leg.fill_price = 0.0
                no_leg.filled_at  = 0.0
                log.info(
                    "CloneHF [paper-sim]: forcing partial fill — NO leg unfilled  pos=%s", pos_id
                )

        both_filled    = yes_leg.filled and no_leg.filled
        neither_filled = not yes_leg.filled and not no_leg.filled

        if both_filled:
            initial_state = HFState.HEDGED
        elif neither_filled:
            initial_state = HFState.COLLECTING
        else:
            initial_state = HFState.PARTIALLY_FILLED

        pos = HFPosition(
            position_id             = pos_id,
            condition_id            = pair.market.condition_id,
            question                = pair.market.question,
            yes_leg                 = yes_leg,
            no_leg                  = no_leg,
            state                   = initial_state,
            opened_at               = now,
            state_changed_at        = now,
            combined_price_at_entry = pair.combined_ask,
            size_usdc               = size_usdc,
            net_edge_proxy          = pair.edge_proxy,
        )
        self._hf_positions[pos_id] = pos
        self._last_trade_time = now

        self._trades.append({
            "ts":           now,
            "pos_id":       pos_id,
            "condition_id": pair.market.condition_id,
            "question":     pair.market.question[:80],
            "mode":         "hf_pair",
            "combined_ask": pair.combined_ask,
            "edge_proxy":   pair.edge_proxy,
            "size_usdc":    size_usdc,
            "yes_filled":   yes_leg.filled,
            "no_filled":    no_leg.filled,
            "state":        initial_state.value,
            "reason":       "hf_pair_entry",
        })

        log.info(
            "CloneHF: pos=%s  state=%s  yes=%s  no=%s",
            pos_id, initial_state.value,
            "FILLED" if yes_leg.filled else "pending",
            "FILLED" if no_leg.filled  else "pending",
        )

        if initial_state == HFState.HEDGED:
            self._record_hf_hedge_success(pos)

        return pos_id

    # ------------------------------------------------------------------
    # HF Hedge Mode — state machine monitor
    # ------------------------------------------------------------------

    async def monitor_hf_positions(self) -> None:
        """
        Advance the HF state machine for all active positions.

        Call once per HF cycle.  Handles:
          COLLECTING       → PARTIALLY_FILLED when one leg fills
                           → HEDGED           when both fill
                           → ABORTED          on collect timeout
          PARTIALLY_FILLED → triggers hedge (maker then taker fallback)
                           → HEDGED           when hedge fills
                           → ABORTED          on hedge timeout or slippage
        """
        now = time.time()
        to_remove: list[str] = []

        for pos_id, pos in list(self._hf_positions.items()):
            if pos.state in (HFState.HEDGED, HFState.CLOSED, HFState.ABORTED):
                to_remove.append(pos_id)
                continue

            # Paper-mode: advance simulated fills
            if config.DRY_RUN:
                self._sim_advance_fills(pos, now)

            both_filled = pos.yes_leg.filled and pos.no_leg.filled
            if both_filled and pos.state != HFState.HEDGED:
                pos.state            = HFState.HEDGED
                pos.state_changed_at = now
                self._record_hf_hedge_success(pos)
                log.info(
                    "CloneHF: pos=%s HEDGED — both legs filled  "
                    "net_edge=%.4f  time=%.1fs  reason=hedge_both_filled",
                    pos_id, pos.net_edge_proxy, now - pos.opened_at,
                )
                to_remove.append(pos_id)
                continue

            one_filled = pos.yes_leg.filled or pos.no_leg.filled

            # COLLECTING → PARTIALLY_FILLED when one leg fills
            if one_filled and pos.state == HFState.COLLECTING:
                pos.state            = HFState.PARTIALLY_FILLED
                pos.state_changed_at = now
                side = "YES" if pos.yes_leg.filled else "NO"
                log.info(
                    "CloneHF: pos=%s PARTIALLY_FILLED (%s leg filled)  reason=partial_fill",
                    pos_id, side,
                )

            # COLLECTING timeout (neither leg filled)
            if pos.state == HFState.COLLECTING:
                if now - pos.opened_at > CLONE_HEDGE_TIMEOUT_SECS:
                    pos.state        = HFState.ABORTED
                    pos.close_reason = "collect_timeout"
                    self._hf_aborted += 1
                    log.warning(
                        "CloneHF: pos=%s ABORTED (collect timeout %.0fs)  reason=collect_timeout",
                        pos_id, now - pos.opened_at,
                    )
                    to_remove.append(pos_id)
                continue

            # PARTIALLY_FILLED — hedge management
            if pos.state == HFState.PARTIALLY_FILLED:
                unfilled     = pos.no_leg if pos.yes_leg.filled else pos.yes_leg
                secs_partial = now - pos.state_changed_at

                # Hard timeout
                if secs_partial > CLONE_HEDGE_TIMEOUT_SECS:
                    pos.state        = HFState.ABORTED
                    pos.close_reason = f"hedge_timeout_{secs_partial:.0f}s"
                    self._hf_aborted += 1
                    log.warning(
                        "CloneHF: pos=%s ABORTED (hedge timeout %.0fs)  reason=hedge_timeout",
                        pos_id, secs_partial,
                    )
                    to_remove.append(pos_id)
                    continue

                # Urgency ladder: maker first, taker after fallback threshold
                use_taker = secs_partial >= CLONE_HEDGE_TAKER_FALLBACK_SECS
                if not unfilled.filled:
                    await self._place_hedge(pos, unfilled, use_taker)
                    # Re-check after hedge attempt
                    if unfilled.filled:
                        both_now = pos.yes_leg.filled and pos.no_leg.filled
                        if both_now:
                            pos.state            = HFState.HEDGED
                            pos.state_changed_at = now
                            self._record_hf_hedge_success(pos)
                            log.info(
                                "CloneHF: pos=%s HEDGED (via _place_hedge)  reason=hedge_filled",
                                pos_id,
                            )
                            to_remove.append(pos_id)

        for pos_id in to_remove:
            self._hf_positions.pop(pos_id, None)

    async def _place_hedge(self, pos: HFPosition, leg: HFLeg, use_taker: bool) -> None:
        """
        Place a hedge order for an unfilled leg.

        Urgency ladder:
          use_taker=False  — maker/limit at current ask (lower cost, may not fill)
          use_taker=True   — market order (guarantees fill, higher slippage)

        Aborts and transitions position to ABORTED if slippage exceeds guard.

        Reason codes logged: hedge_maker, hedge_taker, hedge_slippage_abort,
                             hedge_maker_fill, hedge_taker_fill, hedge_order_failed.
        """
        try:
            bid, ask = await self.client.get_best_prices(leg.token_id)
            current_price = (bid + ask) / 2.0
        except Exception:
            current_price = leg.entry_price

        slippage_bps = (
            abs(current_price - leg.entry_price) / max(leg.entry_price, 0.001) * 10_000
        )

        if slippage_bps > CLONE_MAX_SLIPPAGE_BPS:
            log.warning(
                "CloneHF: hedge slippage abort  pos=%s  leg=%s  "
                "entry=%.3f  current=%.3f  slippage=%.0f bps > max=%.0f  "
                "reason=hedge_slippage_abort",
                pos.position_id, leg.outcome,
                leg.entry_price, current_price,
                slippage_bps, CLONE_MAX_SLIPPAGE_BPS,
            )
            pos.state        = HFState.ABORTED
            pos.close_reason = f"slippage_{slippage_bps:.0f}bps"
            self._hf_aborted += 1
            return

        order_type = "taker" if use_taker else "maker"
        log.info(
            "CloneHF: hedge %s  pos=%s  leg=%s  "
            "price=%.3f (entry=%.3f slip=%.0f bps)  size=%.2f  reason=hedge_%s",
            order_type, pos.position_id, leg.outcome,
            current_price, leg.entry_price, slippage_bps,
            pos.size_usdc, order_type,
        )

        result = await self.client.place_market_order(leg.token_id, "BUY", pos.size_usdc)
        leg.is_maker = not use_taker

        if result and result.success:
            leg.filled    = True
            leg.fill_price = current_price
            leg.filled_at  = time.time()
            leg.order_id   = result.order_id
            self._trades.append({
                "ts":           time.time(),
                "pos_id":       pos.position_id,
                "condition_id": pos.condition_id,
                "mode":         "hf_hedge",
                "leg":          leg.outcome,
                "price":        current_price,
                "size_usdc":    pos.size_usdc,
                "slippage_bps": slippage_bps,
                "reason":       f"hedge_{order_type}_fill",
            })
            log.info(
                "CloneHF: hedge filled  pos=%s  leg=%s @ %.3f  reason=hedge_%s_fill",
                pos.position_id, leg.outcome, current_price, order_type,
            )
        else:
            err = result.error if result else "unknown"
            log.warning(
                "CloneHF: hedge order failed  pos=%s  leg=%s  err=%s  reason=hedge_order_failed",
                pos.position_id, leg.outcome, err,
            )

    def _sim_advance_fills(self, pos: HFPosition, now: float) -> None:
        """
        DRY_RUN fill simulation.

        Without SIMULATE_PARTIAL: both legs are already marked filled at entry.
        With SIMULATE_PARTIAL: unfilled legs fill after TAKER_FALLBACK_SECS
        (simulates the maker→taker hedge path for paper testing).
        """
        if not CLONE_HF_PAPER_SIMULATE_PARTIAL:
            return
        for leg in (pos.yes_leg, pos.no_leg):
            if not leg.filled:
                secs_waiting = now - leg.placed_at
                if secs_waiting >= CLONE_HEDGE_TAKER_FALLBACK_SECS:
                    leg.filled    = True
                    leg.fill_price = leg.entry_price
                    leg.filled_at  = now
                    log.debug(
                        "CloneHF [paper-sim]: leg=%s filled at %.1fs  pos=%s",
                        leg.outcome, secs_waiting, pos.position_id,
                    )

    def _record_hf_hedge_success(self, pos: HFPosition) -> None:
        """Record a successful hedge completion."""
        self._hf_hedge_success += 1
        t2h = (
            max(pos.yes_leg.filled_at, pos.no_leg.filled_at) - pos.opened_at
            if (pos.yes_leg.filled_at and pos.no_leg.filled_at) else 0.0
        )
        self._hf_time_to_hedge.append(t2h)
        self._hf_net_edge_samples.append(pos.net_edge_proxy)

    # ------------------------------------------------------------------
    # HF metrics
    # ------------------------------------------------------------------

    def hf_stats(self) -> dict:
        """Return HF hedge mode performance metrics."""
        total   = self._hf_hedge_success + self._hf_aborted
        h_rate  = self._hf_hedge_success / max(1, total)
        avg_t2h = (
            sum(self._hf_time_to_hedge) / len(self._hf_time_to_hedge)
            if self._hf_time_to_hedge else 0.0
        )
        avg_edge = (
            sum(self._hf_net_edge_samples) / len(self._hf_net_edge_samples)
            if self._hf_net_edge_samples else 0.0
        )
        return {
            "hf_mode_enabled":        CLONE_HF_MODE_ENABLED,
            "active_hf_positions":    len(self._hf_positions),
            "hedge_success":          self._hf_hedge_success,
            "aborted":                self._hf_aborted,
            "hedge_success_rate":     round(h_rate, 3),
            "avg_time_to_hedge_secs": round(avg_t2h, 2),
            "avg_net_edge_proxy":     round(avg_edge, 4),
            "combined_price_band":    f"[{CLONE_COMBINED_PRICE_MIN},{CLONE_COMBINED_PRICE_MAX}]",
            "cycle_interval_secs":    CLONE_CYCLE_INTERVAL_SECS,
            "simulate_partial":       CLONE_HF_PAPER_SIMULATE_PARTIAL,
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "enabled":        CLONE_ENABLED,
            "clone_wallet":   CLONE_WALLET[:12] + "…" if CLONE_WALLET else "(not set)",
            "profile_loaded": self._profile is not None,
            "cycles":         self._cycle_count,
            "trades":         len(self._trades),
            "open_positions": len(self._open_positions),
            "errors":         self._errors,
            "last_trade_ago": round(time.time() - self._last_trade_time, 1) if self._last_trade_time else None,
            "stage":          config.LIVE_DEPLOY_MODE,
            "stage_max_size": CLONE_LIVE_MAX_SIZE,
            "stage_max_pos":  CLONE_LIVE_MAX_POSITIONS,
        }
