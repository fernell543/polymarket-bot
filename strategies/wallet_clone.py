"""
Wallet Clone Strategy
======================
Approximates the trading behavior of a target Polymarket wallet by:
  1. Loading an inferred behavioral profile (from analytics/clone_profile.py).
  2. Scoring live markets against the profile (category, price range, timing).
  3. Executing the highest-scoring opportunity once per cycle.
  4. Using the shared PositionSizer for risk management.

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

    def __repr__(self) -> str:
        return (
            f"CloneOpp(score={self.score:.2f} outcome={self.outcome} "
            f"price={self.price:.3f} size={self.size_usdc:.1f} "
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

    Scoring function (0–1):
      category_score (0.35) — market category matches preferred categories
      price_score    (0.35) — current best price within target's entry range
      timing_score   (0.20) — days-to-expiry within target's observed window
      bias_score     (0.10) — direction matches target's YES/NO bias
    """

    def __init__(self, client: PolymarketClient):
        self.client = client
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

        # Resolve profile path
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
    # Scoring
    # ------------------------------------------------------------------

    def _category_score(self, market: Market) -> float:
        """0.0–1.0 based on how well market category matches profile."""
        if not self._profile or not self._profile.preferred_categories:
            return 0.5  # neutral if no data
        q_lower = market.question.lower()
        for cat in self._profile.preferred_categories:
            if cat and cat in q_lower:
                return 1.0
        return 0.1

    def _price_score(self, price: float, outcome: str) -> float:
        """0.0–1.0 — how well the current price fits target's entry range."""
        if not self._profile:
            return 0.5
        if outcome.lower() == "yes":
            lo, hi = self._profile.yes_min_price, self._profile.yes_max_price
        else:
            lo, hi = self._profile.no_min_price, self._profile.no_max_price

        if lo >= hi:
            return 0.5

        if price < lo or price > hi:
            # Penalty: score decays as distance from range increases
            dist = min(abs(price - lo), abs(price - hi))
            return max(0.0, 0.5 - dist * 2.0)

        # Within range: peak score at midpoint
        mid = (lo + hi) / 2.0
        dist_from_mid = abs(price - mid) / ((hi - lo) / 2.0)
        return 1.0 - 0.3 * dist_from_mid  # 0.7–1.0

    def _timing_score(self, market: Market) -> tuple[float, Optional[float]]:
        """0.0–1.0 and DTE in days."""
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
                return 0.0, dte  # already expired

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
        """0.0–1.0 — direction alignment with target's YES/NO bias."""
        if not self._profile or not CLONE_BIAS_ENABLED:
            return 0.5
        direction_match = (outcome.lower() == self._profile.bias_direction)
        if direction_match:
            return 0.5 + 0.5 * self._profile.bias_strength
        else:
            return 0.5 - 0.5 * self._profile.bias_strength

    def _score_opportunity(
        self,
        market: Market,
        token_id: str,
        outcome: str,
        price: float,
    ) -> tuple[float, Optional[float]]:
        """Composite score (0–1) and DTE."""
        cat_s  = self._category_score(market)        * 0.35
        price_s = self._price_score(price, outcome)  * 0.35
        timing_s, dte = self._timing_score(market)
        timing_s *= 0.20
        bias_s = self._bias_score(outcome)            * 0.10

        score = cat_s + price_s + timing_s + bias_s
        return score, dte

    # ------------------------------------------------------------------
    # Market scanning
    # ------------------------------------------------------------------

    async def scan_once(self, markets: list[Market]) -> list[CloneOpportunity]:
        """Score all live markets and return ranked opportunities."""
        if not self._profile:
            return []

        if len(self._open_positions) >= CLONE_MAX_OPEN_POSITIONS:
            log.debug("WalletClone: max open positions reached (%d)", CLONE_MAX_OPEN_POSITIONS)
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
                except Exception:
                    continue

                score, dte = self._score_opportunity(market, token_id, outcome, price)
                if score < CLONE_SCORE_THRESHOLD:
                    continue

                # Estimate base size
                base_size = self._profile.base_size_usdc * CLONE_AGGRESSIVENESS
                base_size = max(self._profile.min_size_usdc, min(self._profile.max_size_usdc, base_size))

                # Infer category from question keywords
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
                ))

        # Sort by score descending
        opportunities.sort(key=lambda o: o.score, reverse=True)
        if opportunities:
            log.info("WalletClone: %d opportunities scored (top: %s)", len(opportunities), opportunities[0])
        return opportunities

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, opp: CloneOpportunity) -> bool:
        """Place order for the best opportunity, respecting risk controls."""
        from health_state import HealthLevel

        # Apply PositionSizer
        size_usdc = opp.size_usdc
        if self._sizer is not None:
            # Edge: spread * 10000 / 2 (half-spread in bps as proxy)
            edge_bps = max(50.0, (1.0 - opp.price) * 5000.0)
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

        log.info(
            "WalletClone: executing %s %s @ %.3f  size=%.2f USDC  score=%.2f  dte=%s",
            opp.outcome, opp.market.question[:50], opp.price, size_usdc, opp.score,
            f"{opp.dte_days:.1f}d" if opp.dte_days is not None else "N/A",
        )

        result = await self.client.place_market_order(opp.token_id, "BUY", size_usdc)

        self._trades.append({
            "ts":           time.time(),
            "condition_id": opp.market.condition_id,
            "question":     opp.market.question[:80],
            "outcome":      opp.outcome,
            "price":        opp.price,
            "size_usdc":    size_usdc,
            "score":        opp.score,
            "order_id":     result.order_id if result else "",
            "success":      result.success if result else False,
        })

        if result and result.success:
            self._open_positions[opp.market.condition_id] = ClonePosition(
                condition_id=opp.market.condition_id,
                question=opp.market.question,
                outcome=opp.outcome,
                token_id=opp.token_id,
                entry_price=opp.price,
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
        }
