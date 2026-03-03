"""
Multi-factor signal engine for Polymarket bot.
===============================================
Computes a composite signal score from three independent factors:

  1. Microstructure  — bid-ask spread width + order-book depth imbalance.
  2. Short-horizon momentum — recent price direction over the last N ticks.
  3. Mean-reversion z-score — deviation of current price from its rolling mean.

The engine also classifies the current market regime (trending vs ranging)
and applies regime-specific factor weights so only regime-appropriate signals
are active at any time.

All output scores are in [-1, +1].  Positive = bullish (buy signal),
negative = bearish (sell signal).  Confidence is in [0, 1].

Gated by QUANT_MODE_ENABLED config flag:
  - When False: every call returns a neutral, zero-confidence result.
  - When True : full multi-factor computation runs.
"""

from __future__ import annotations

import collections
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import config

log = logging.getLogger(__name__)

# Rolling window depth (number of mid-price samples kept per token)
PRICE_HISTORY_LEN = 30


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SignalComponents:
    """Individual factor scores, each normalised to [-1, +1]."""
    microstructure: float = 0.0   # positive = buy pressure in book
    momentum:       float = 0.0   # positive = recent uptrend
    mean_reversion: float = 0.0   # positive = oversold (buy signal)


@dataclass
class SignalResult:
    """Full output of one SignalEngine.evaluate() call."""
    score:      float             # composite directional score [-1, +1]
    confidence: float             # 0..1; trade gated if < threshold
    regime:     str               # "trending" | "ranging" | "unknown" | "disabled"
    components: SignalComponents = field(default_factory=SignalComponents)
    reason:     str = ""          # human-readable detail for logging/audit

    @property
    def is_tradeable(self) -> bool:
        """True when confidence clears the configured threshold."""
        return self.confidence >= config.SIGNAL_CONFIDENCE_THRESHOLD

    @property
    def direction(self) -> str:
        """Human-readable direction of the signal."""
        if self.score > 0.05:
            return "BUY"
        if self.score < -0.05:
            return "SELL"
        return "NEUTRAL"


# ---------------------------------------------------------------------------
# Rolling price buffer
# ---------------------------------------------------------------------------

class PriceHistory:
    """
    Lightweight circular buffer of (monotonic_time, mid_price) pairs.

    Provides rolling statistics needed by the signal factors.
    """

    def __init__(self, maxlen: int = PRICE_HISTORY_LEN):
        self._buf: collections.deque[tuple[float, float]] = collections.deque(
            maxlen=maxlen
        )

    def push(self, price: float) -> None:
        self._buf.append((time.monotonic(), price))

    @property
    def prices(self) -> list[float]:
        return [p for _, p in self._buf]

    @property
    def n(self) -> int:
        return len(self._buf)

    def rolling_mean(self) -> float:
        prices = self.prices
        return sum(prices) / len(prices) if prices else 0.0

    def rolling_std(self) -> float:
        prices = self.prices
        if len(prices) < 2:
            return 0.0
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / (len(prices) - 1)
        return math.sqrt(variance)

    def recent_return(self, lookback: int = 5) -> float:
        """
        Fractional price change over the last `lookback` samples.

        Returns 0.0 if not enough history yet.
        """
        prices = self.prices
        if len(prices) < lookback + 1:
            return 0.0
        base = prices[-(lookback + 1)]
        if base == 0:
            return 0.0
        return (prices[-1] - base) / base


# ---------------------------------------------------------------------------
# Signal engine (one instance per token)
# ---------------------------------------------------------------------------

class SignalEngine:
    """
    Stateful signal engine for a single market token.

    Maintains a rolling price history and computes composite signal scores
    from order-book snapshots.

    Typical usage (inside a strategy loop):
        engine = SignalEngine(token_id)
        ...
        result = engine.evaluate(bid, ask, bids, asks)
        if not result.is_tradeable:
            continue
        if result.direction == "BUY":
            await place_order(...)
    """

    def __init__(self, token_id: str):
        self.token_id = token_id
        self._history = PriceHistory(maxlen=PRICE_HISTORY_LEN)

    def evaluate(
        self,
        bid:  float,
        ask:  float,
        bids: list[tuple[float, float]],   # [(price, size), …]
        asks: list[tuple[float, float]],
    ) -> SignalResult:
        """
        Compute a composite signal from a current order-book snapshot.

        Parameters
        ----------
        bid, ask : best bid and ask prices (0..1)
        bids, asks : full order-book levels as (price, size) pairs
        """
        # --- Guard: quant mode off ------------------------------------------
        if not config.QUANT_MODE_ENABLED:
            return SignalResult(
                score=0.0, confidence=0.0, regime="disabled",
                reason="QUANT_MODE_ENABLED=False — signal engine inactive",
            )

        # --- Update history --------------------------------------------------
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        if mid > 0:
            self._history.push(mid)

        if mid <= 0:
            return SignalResult(
                score=0.0, confidence=0.0, regime="unknown",
                reason="no valid mid price",
            )

        spread = ask - bid

        # =====================================================================
        # Factor 1 — Microstructure
        # =====================================================================
        # Order-book depth imbalance at top 5 levels
        #   +1 = all depth on bid side (strong buy pressure)
        #   -1 = all depth on ask side (strong sell pressure)
        bid_vol = sum(s for _, s in bids[:5])
        ask_vol = sum(s for _, s in asks[:5])
        total_vol = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0.0
        micro_score = float(imbalance)  # already in [-1, +1]

        # =====================================================================
        # Factor 2 — Short-horizon momentum
        # =====================================================================
        # Fractional return over last 5 samples, squeezed into [-1,+1] via tanh.
        # A 5% move maps to ~tanh(1.0) ≈ 0.76.
        mom_raw = self._history.recent_return(lookback=5)
        mom_score = math.tanh(mom_raw * 20.0)

        # =====================================================================
        # Factor 3 — Mean-reversion z-score
        # =====================================================================
        mean = self._history.rolling_mean()
        std  = self._history.rolling_std()
        if std > 1e-6:
            z = (mid - mean) / std
            # z > 0 → above mean → bearish (expect reversion down) → negative score
            mr_score = -math.tanh(z)
        else:
            mr_score = 0.0

        # =====================================================================
        # Regime classification
        # =====================================================================
        # Relative volatility = rolling_std / rolling_mean
        rel_vol = std / mean if mean > 0 else 0.0
        n = self._history.n

        if n < 10:
            regime = "unknown"
        elif rel_vol > config.REGIME_HIGH_VOL_THRESHOLD:
            regime = "trending"
        else:
            regime = "ranging"

        # =====================================================================
        # Regime-gated composite score
        # =====================================================================
        w_micro = config.SIGNAL_WEIGHT_MICRO
        w_mom   = config.SIGNAL_WEIGHT_MOMENTUM
        w_mr    = config.SIGNAL_WEIGHT_MEAN_REV

        if regime == "trending":
            # Momentum is the primary edge in trending markets;
            # mean-reversion weight is shifted toward momentum.
            w_mom_eff = w_mom + w_mr * 0.5
            w_mr_eff  = w_mr  * 0.5
            composite = (
                w_micro * micro_score
                + w_mom_eff * mom_score
                + w_mr_eff  * mr_score
            )
        elif regime == "ranging":
            # Mean-reversion leads in range-bound markets;
            # momentum weight is shifted toward mean-reversion.
            w_mom_eff = w_mom * 0.5
            w_mr_eff  = w_mr  + w_mom * 0.5
            composite = (
                w_micro * micro_score
                + w_mom_eff * mom_score
                + w_mr_eff  * mr_score
            )
        else:
            # Unknown regime: emit neutral signal (no edge claim)
            composite = 0.0

        composite = max(-1.0, min(1.0, composite))

        # =====================================================================
        # Confidence
        # =====================================================================
        # Confidence measures factor agreement.
        # High confidence when active factors point the same direction as the
        # composite and have meaningful magnitude.
        if regime == "trending":
            active = [micro_score, mom_score]
        elif regime == "ranging":
            active = [micro_score, mr_score]
        else:
            active = []

        if active and composite != 0.0:
            # Fraction of active factors that agree with composite direction
            agreeing = sum(1 for s in active if s * composite > 0)
            agreement_ratio = agreeing / len(active)
            # Weight by mean magnitude of agreeing factors
            mean_abs = sum(abs(s) for s in active) / len(active)
            confidence = agreement_ratio * mean_abs
        else:
            confidence = 0.0

        confidence = max(0.0, min(1.0, confidence))

        # =====================================================================
        # Assemble result
        # =====================================================================
        rel_spread = spread / mid if mid > 0 else 1.0
        reason = (
            f"regime={regime} score={composite:.3f} conf={confidence:.3f} "
            f"spread={rel_spread:.4f} imbal={imbalance:.3f} "
            f"mom={mom_score:.3f} mr={mr_score:.3f} n_hist={n}"
        )

        return SignalResult(
            score=round(composite, 4),
            confidence=round(confidence, 4),
            regime=regime,
            components=SignalComponents(
                microstructure=round(micro_score, 4),
                momentum=round(mom_score, 4),
                mean_reversion=round(mr_score, 4),
            ),
            reason=reason,
        )
