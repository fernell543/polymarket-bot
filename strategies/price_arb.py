"""
Price Arbitrage Strategy
========================
Scans every active market for cases where:

    YES_ask + NO_ask < 1.0 - fees - min_profit

If found, simultaneously buys both YES and NO tokens.
At settlement exactly one side pays $1.00, guaranteeing a locked profit.

Risk: settlement / oracle failure (rare on Polymarket with UMA).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import config
from client import PolymarketClient, Market

if TYPE_CHECKING:
    from health_state import HealthState
    from risk.sizing import PositionSizer

log = logging.getLogger(__name__)


@dataclass
class ArbOpportunity:
    market: Market
    yes_token_id: str
    no_token_id: str
    yes_ask: float          # price to buy YES
    no_ask: float           # price to buy NO
    combined_cost: float    # yes_ask + no_ask
    gross_profit: float     # 1.0 - combined_cost
    net_profit: float       # gross_profit - fees
    profit_pct: float       # net_profit as fraction of combined_cost


@dataclass
class ArbResult:
    opportunity: ArbOpportunity
    yes_order_id: Optional[str] = None
    no_order_id: Optional[str] = None
    success: bool = False
    error: Optional[str] = None


class PriceArbStrategy:
    """
    Polls all active markets concurrently, identifies arbitrage windows,
    and executes both legs simultaneously.
    """

    def __init__(self, client: PolymarketClient):
        self.client = client
        self._executions: list[ArbResult] = []
        self._sizer: Optional["PositionSizer"] = None
        self._health: Optional["HealthState"] = None

    def set_sizer(self, sizer: "PositionSizer") -> None:
        self._sizer = sizer

    def set_health(self, health: "HealthState") -> None:
        self._health = health

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    async def scan_once(self) -> list[ArbOpportunity]:
        """Fetch all markets and return arb opportunities sorted by profit."""
        try:
            markets = await self.client.get_markets(active_only=True)
        except Exception as exc:
            log.error("PriceArb: failed to fetch markets: %s", exc)
            return []

        tasks = [self._evaluate_market(m) for m in markets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        opportunities = [r for r in results if isinstance(r, ArbOpportunity)]
        opportunities.sort(key=lambda o: o.net_profit, reverse=True)

        if opportunities:
            log.info(
                "PriceArb: found %d opportunities (best: %.4f USDC profit, %.2f%%)",
                len(opportunities),
                opportunities[0].net_profit,
                opportunities[0].profit_pct * 100,
            )
        return opportunities

    async def _evaluate_market(self, market: Market) -> Optional[ArbOpportunity]:
        """Check a single market for price arbitrage."""
        tokens = {t["outcome"].upper(): t["token_id"] for t in market.tokens}
        yes_id = tokens.get("YES")
        no_id = tokens.get("NO")
        if not yes_id or not no_id:
            return None

        # Fetch both order books concurrently
        (_, yes_ask), (_, no_ask) = await asyncio.gather(
            self.client.get_best_prices(yes_id),
            self.client.get_best_prices(no_id),
        )

        if yes_ask <= 0 or no_ask <= 0:
            return None

        combined = yes_ask + no_ask
        gross = 1.0 - combined
        # Fee is 2% of $1.00 winnings
        fee = config.POLYMARKET_FEE
        net = gross - fee

        if net < config.ARB_MIN_PROFIT_PCT * combined:
            return None

        return ArbOpportunity(
            market=market,
            yes_token_id=yes_id,
            no_token_id=no_id,
            yes_ask=yes_ask,
            no_ask=no_ask,
            combined_cost=combined,
            gross_profit=gross,
            net_profit=net,
            profit_pct=net / combined,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, opp: ArbOpportunity) -> ArbResult:
        """
        Execute both legs of an arbitrage simultaneously.

        We buy both YES and NO as market orders so they fill immediately.
        Total size is determined by the risk-based sizer (falls back to a
        fixed cap when no sizer is wired in).
        """
        if self._sizer is not None:
            # edge_bps: net profit as fraction of combined cost → basis points
            edge_bps = opp.profit_pct * 10_000
            health_level = self._health.level if self._health else None
            sr = self._sizer.compute(
                edge_bps=edge_bps,
                confidence=1.0,      # locked-profit arb — outcome is deterministic
                vol_regime="low",    # no price-vol risk (deterministic payoff)
                health_level=health_level,
                label="PriceArb",
            )
            if sr.size_usdc == 0:
                log.warning(
                    "PriceArb: sizer blocked execution for %s (reason=%s)",
                    market_label(opp.market), sr.reason,
                )
                result = ArbResult(opportunity=opp, error=f"sizer:{sr.reason}")
                self._executions.append(result)
                return result
            leg_usdc = sr.size_usdc / 2   # split total size across both legs
        else:
            # Fallback: fixed cap (no sizer wired yet)
            leg_usdc = min(config.MAX_POSITION_USDC / 2, 100)

        log.info(
            "PriceArb EXECUTE | %s | YES@%.4f + NO@%.4f = %.4f | "
            "net profit %.4f (%.2f%%) | leg=%.2f USDC",
            market_label(opp.market),
            opp.yes_ask, opp.no_ask, opp.combined_cost,
            opp.net_profit, opp.profit_pct * 100,
            leg_usdc,
        )

        yes_task = self.client.place_market_order(
            opp.yes_token_id, "BUY", leg_usdc
        )
        no_task = self.client.place_market_order(
            opp.no_token_id, "BUY", leg_usdc
        )
        yes_result, no_result = await asyncio.gather(yes_task, no_task)

        result = ArbResult(opportunity=opp)
        result.yes_order_id = yes_result.order_id
        result.no_order_id = no_result.order_id

        if yes_result.success and no_result.success:
            result.success = True
            log.info("PriceArb: both legs placed — YES %s / NO %s",
                     yes_result.order_id, no_result.order_id)
        else:
            errors = []
            if not yes_result.success:
                errors.append(f"YES: {yes_result.error}")
            if not no_result.success:
                errors.append(f"NO: {no_result.error}")
            result.error = "; ".join(errors)
            log.warning("PriceArb: partial failure — %s", result.error)

        self._executions.append(result)
        return result

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, poll_interval: float = 10.0):
        """
        Continuously scan for price arb opportunities.
        Executes the best one found each cycle.
        """
        log.info("PriceArbStrategy started (poll=%.0fs)", poll_interval)
        while True:
            try:
                opportunities = await self.scan_once()
                if opportunities:
                    await self.execute(opportunities[0])
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("PriceArb loop error: %s", exc, exc_info=True)
            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        total = len(self._executions)
        success = sum(1 for r in self._executions if r.success)
        return {"total": total, "success": success, "failures": total - success}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def market_label(market: Market) -> str:
    q = market.question
    return q[:60] + "…" if len(q) > 60 else q
