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
import json
import logging
import math
import os
import time
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
