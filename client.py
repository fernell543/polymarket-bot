"""
Thin async wrapper around Polymarket's CLOB API.

Uses py-clob-client for authentication and order signing,
and aiohttp for non-blocking HTTP calls.
"""

import asyncio
import csv
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp

import config

# ---------------------------------------------------------------------------
# Order CSV logger
# ---------------------------------------------------------------------------

os.makedirs("logs", exist_ok=True)
_ORDER_LOG = "logs/orders.csv"

def _log_order_csv(order_type, side, token_id, price, size_usdc, size_shares, order_id, status, dry_run):
    file_exists = os.path.isfile(_ORDER_LOG)
    with open(_ORDER_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "type", "side", "token_id", "price", "size_usdc", "size_shares", "order_id", "status", "dry_run"])
        writer.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            order_type, side, token_id, price, size_usdc, size_shares, order_id, status, dry_run
        ])

log = logging.getLogger(__name__)


def _normalize_api_creds(x):
    """Coerce whatever create_or_derive_api_creds() returns into ApiCreds.

    Handles three shapes returned by different py-clob-client versions:
      • already an ApiCreds object  → return as-is
      • dict with various key names → map to ApiCreds fields
      • generic object with attrs   → map attrs to ApiCreds fields
    Raises ValueError if required fields cannot be found.
    """
    try:
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        raise RuntimeError("py-clob-client not installed")

    if isinstance(x, ApiCreds):
        return x

    if isinstance(x, dict):
        api_key = (
            x.get("api_key") or x.get("apiKey")
            or x.get("key") or x.get("apiKeyId")
        )
        api_secret = (
            x.get("api_secret") or x.get("apiSecret") or x.get("secret")
        )
        api_passphrase = (
            x.get("api_passphrase") or x.get("apiPassphrase") or x.get("passphrase")
        )
        if not (api_key and api_secret and api_passphrase):
            raise ValueError(
                f"_normalize_api_creds: missing fields in dict keys={list(x.keys())}"
            )
        return ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

    # Generic object fallback
    api_key = getattr(x, "api_key", None) or getattr(x, "apiKey", None)
    api_secret = getattr(x, "api_secret", None) or getattr(x, "apiSecret", None)
    api_passphrase = (
        getattr(x, "api_passphrase", None) or getattr(x, "apiPassphrase", None)
    )
    if not (api_key and api_secret and api_passphrase):
        raise ValueError(
            "_normalize_api_creds: could not extract fields from returned object"
        )
    return ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )


@dataclass
class Market:
    condition_id: str
    question: str
    end_date_iso: str
    active: bool
    closed: bool
    tokens: list[dict]   # [{"token_id": ..., "outcome": "Yes"/"No"}, ...]
    rewards: Optional[dict] = None


@dataclass
class OrderBook:
    token_id: str
    bids: list[tuple[float, float]]  # [(price, size), ...]
    asks: list[tuple[float, float]]


@dataclass
class PlacedOrder:
    order_id: str
    status: str
    success: bool
    error: Optional[str] = None


class PolymarketClient:
    """
    Async Polymarket CLOB client.

    Handles:
      - Fetching markets and order books (unauthenticated)
      - Placing / cancelling orders (authenticated via EIP-712)
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._clob_client = None  # py-clob-client instance
        self._rewards_available: bool = True  # latched False on first 405

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._session = aiohttp.ClientSession(
            base_url=config.CLOB_HOST,
            timeout=aiohttp.ClientTimeout(total=10),
        )
        self._init_clob_client()
        log.info("PolymarketClient started (dry_run=%s)", config.DRY_RUN)

    async def stop(self):
        if self._session:
            await self._session.close()

    def _init_clob_client(self):
        """Initialise the signing client from py-clob-client.

        Uses the auth flow proven by the steroid bot:
          1. ClobClient with signature_type=2 + explicit funder address
          2. Derive L2 API key via create_or_derive_api_creds()
          3. Normalize the result (handles dict / object / ApiCreds shapes)
          4. set_api_creds(); fall back to direct .api_creds assignment on error
        """
        try:
            from py_clob_client.client import ClobClient

            clob_kwargs = dict(
                host=config.CLOB_HOST,
                key=config.PRIVATE_KEY,
                chain_id=config.POLYGON_CHAIN_ID,
                signature_type=config.POLY_SIGNATURE_TYPE,
            )
            if config.POLY_SIGNATURE_TYPE == 2 and config.POLY_FUNDER:
                clob_kwargs["funder"] = config.POLY_FUNDER

            self._clob_client = ClobClient(**clob_kwargs)

            # Derive L2 API key; normalize whichever shape is returned.
            raw_creds = self._clob_client.create_or_derive_api_creds()
            creds = _normalize_api_creds(raw_creds)

            # Preferred: set_api_creds(); fallback: direct attribute (older SDK versions).
            try:
                self._clob_client.set_api_creds(creds)
            except Exception as exc:
                log.debug("set_api_creds() raised %s — assigning directly", exc)
                self._clob_client.api_creds = creds

            # Log the derived bot address (safe — no secrets in output).
            try:
                from eth_account import Account
                bot_addr = (
                    Account.from_key(config.PRIVATE_KEY).address
                    if config.PRIVATE_KEY else "MISSING_PRIVATE_KEY"
                )
            except Exception:
                bot_addr = "UNKNOWN"
            log.info("Bot wallet address: %s", bot_addr)
            log.info(
                "Authenticated with Polymarket CLOB API (signature_type=%s, creds=%s)",
                config.POLY_SIGNATURE_TYPE, bool(getattr(self._clob_client, "api_creds", None)),
            )
        except ImportError:
            log.warning(
                "py-clob-client not installed — order placement disabled. "
                "Run: pip install py-clob-client"
            )

    # ------------------------------------------------------------------
    # Market data (no auth required)
    # ------------------------------------------------------------------

    async def get_markets(
        self, active_only: bool = True, limit: int = 500
    ) -> list[Market]:
        """Return a list of markets, paginating until exhausted.

        If a single page fetch fails after retries, pagination stops and
        whatever markets have been collected so far are returned (graceful
        degradation — callers should treat a short list as a warning, not
        an error).
        """
        params = {"limit": limit}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"

        markets = []
        next_cursor = None
        page = 0

        while True:
            page += 1
            if next_cursor:
                params["next_cursor"] = next_cursor

            try:
                data = await self._get_json("/markets", params)
            except Exception as exc:
                log.warning(
                    "get_markets: page %d fetch failed — returning %d markets collected so far: %s",
                    page, len(markets), exc,
                )
                break   # graceful degrade: don't wipe what we have

            for m in data.get("data", []):
                markets.append(
                    Market(
                        condition_id=m["condition_id"],
                        question=m.get("question", ""),
                        end_date_iso=m.get("end_date_iso", ""),
                        active=m.get("active", False),
                        closed=m.get("closed", True),
                        tokens=m.get("tokens", []),
                    )
                )

            next_cursor = data.get("next_cursor")
            if not next_cursor or next_cursor == "LTE=":
                break

        return markets

    async def get_order_book(self, token_id: str) -> OrderBook:
        """Return bids and asks for a single token."""
        async with self._session.get(
            "/book", params={"token_id": token_id}
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        def parse_side(entries):
            result = []
            for e in entries or []:
                try:
                    result.append((float(e["price"]), float(e["size"])))
                except (KeyError, ValueError):
                    pass
            return result

        return OrderBook(
            token_id=token_id,
            bids=parse_side(data.get("bids")),
            asks=parse_side(data.get("asks")),
        )

    async def get_best_prices(self, token_id: str) -> tuple[float, float]:
        """Return (best_bid, best_ask) for a token. Returns (0, 0) on failure.

        IMPORTANT: Returns (0.0, 0.0) — NOT (0.0, 1.0) — on failure.
        ask=1.0 would pass the discount check and trigger a zero-profit buy.
        Callers must treat ask=0 as "no liquidity, skip trade" (Bug 7 fix).
        """
        try:
            book = await self.get_order_book(token_id)
            best_bid = max((p for p, _ in book.bids), default=0.0)
            best_ask = min((p for p, _ in book.asks), default=0.0)
            return best_bid, best_ask
        except Exception as exc:
            log.debug("get_best_prices(%s) failed: %s", token_id, exc)
            return 0.0, 0.0

    async def get_midpoint(self, token_id: str) -> float:
        bid, ask = await self.get_best_prices(token_id)
        return (bid + ask) / 2

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        max_retries: int = 3,
    ):
        """
        GET a JSON endpoint with exponential-backoff retry on transient
        5xx errors and network timeouts.  4xx errors are re-raised immediately.
        """
        params = params or {}
        delay = 1.0
        for attempt in range(max_retries):
            try:
                async with self._session.get(path, params=params) as resp:
                    if resp.status in (502, 503, 504) and attempt < max_retries - 1:
                        jitter = random.uniform(0, delay * 0.3)
                        log.warning(
                            "HTTP %d on %s — retry %d/%d in %.1fs",
                            resp.status, path, attempt + 1, max_retries - 1, delay + jitter,
                        )
                        await asyncio.sleep(delay + jitter)
                        delay *= 2
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientResponseError:
                raise   # 4xx or exhausted 5xx — propagate to caller
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < max_retries - 1:
                    jitter = random.uniform(0, delay * 0.3)
                    log.warning(
                        "Network error on %s (attempt %d/%d): %s — retrying in %.1fs",
                        path, attempt + 1, max_retries, exc, delay + jitter,
                    )
                    await asyncio.sleep(delay + jitter)
                    delay *= 2
                    continue
                raise

    async def get_rewards_markets(self) -> list[dict]:
        """Return markets currently earning liquidity rewards.

        If the endpoint returns 405 (not supported on this env) the flag
        ``_rewards_available`` is latched to False and subsequent calls
        return [] immediately without hitting the network.
        """
        if not self._rewards_available:
            return []
        try:
            async with self._session.get("/rewards/markets_earning_daily") as resp:
                if resp.status == 405:
                    log.info(
                        "Rewards endpoint returned 405 — feature unavailable, disabling."
                    )
                    self._rewards_available = False
                    return []
                resp.raise_for_status()
                return await resp.json()
        except Exception as exc:
            log.warning("Could not fetch rewards markets: %s", exc)
            return []

    async def get_balance_usdc(self) -> tuple[float, str]:
        """Return (balance_usdc, note) where note explains the source or failure reason."""
        if not config.WALLET_ADDRESS:
            return 0.0, "WALLET_ADDRESS not set"

        # First try legacy HTTP endpoint.
        try:
            async with self._session.get(
                "/balance", params={"address": config.WALLET_ADDRESS}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("balance", 0)), "live"
                http_note = f"HTTP {resp.status}"
        except Exception as exc:
            http_note = f"API unavailable ({type(exc).__name__})"

        # Fallback: ask py-clob-client directly for collateral balance.
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            if self._clob_client is not None:
                data = self._clob_client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.COLLATERAL,
                        signature_type=config.POLY_SIGNATURE_TYPE,
                    )
                )
                bal = float(data.get("balance", 0))
                return bal, f"clob_client ({http_note})"
        except Exception as exc:
            log.debug("get_balance_allowance: %s", exc)

        return 0.0, http_note

    async def get_open_orders(self) -> list[dict]:
        """Return open orders for this wallet."""
        try:
            async with self._session.get(
                "/orders", params={"maker_address": config.WALLET_ADDRESS}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as exc:
            log.debug("get_open_orders: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Order execution (requires auth)
    # ------------------------------------------------------------------

    async def place_limit_order(
        self,
        token_id: str,
        side: str,       # "BUY" or "SELL"
        price: float,    # 0.01 – 0.99
        size_usdc: float,
    ) -> PlacedOrder:
        """
        Place a GTC limit order.

        In dry-run mode the order is logged but never sent.
        """
        size_shares = round(size_usdc / price, 4)
        log.info(
            "[ORDER] %s %s @ %.4f  (%.2f USDC = %.4f shares) dry=%s",
            side, token_id[:8], price, size_usdc, size_shares, config.DRY_RUN,
        )

        if config.DRY_RUN:
            _log_order_csv("LIMIT", side, token_id, price, size_usdc, size_shares, "DRY-RUN", "simulated", True)
            return PlacedOrder(
                order_id="DRY-RUN", status="simulated", success=True
            )

        if self._clob_client is None:
            return PlacedOrder(
                order_id="", status="error", success=False,
                error="CLOB client not initialised",
            )

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=size_shares,
                side=("BUY" if side == "BUY" else "SELL"),
            )
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed, OrderType.GTC)

            success = resp.get("success", False)
            placed = PlacedOrder(
                order_id=resp.get("orderID", ""),
                status=resp.get("status", "unknown"),
                success=success,
                error=None if success else str(resp),
            )
            _log_order_csv("LIMIT", side, token_id, price, size_usdc, size_shares, placed.order_id, placed.status, False)
            return placed
        except Exception as exc:
            log.error("place_limit_order failed: %s", exc)
            return PlacedOrder(order_id="", status="error", success=False, error=str(exc))

    async def place_market_order(
        self,
        token_id: str,
        side: str,
        size_usdc: float,
    ) -> PlacedOrder:
        """Place a market (FOK) order. Uses best ask/bid as price limit."""
        bid, ask = await self.get_best_prices(token_id)
        price = ask if side == "BUY" else bid

        if price <= 0 or price >= 1:
            return PlacedOrder(
                order_id="", status="error", success=False,
                error=f"No liquidity for market order (bid={bid}, ask={ask})"
            )

        log.info(
            "[MARKET] %s %s @ market (%.4f)  %.2f USDC dry=%s",
            side, token_id[:8], price, size_usdc, config.DRY_RUN,
        )

        size_shares = round(size_usdc / price, 4)

        if config.DRY_RUN:
            _log_order_csv("MARKET", side, token_id, price, size_usdc, size_shares, "DRY-RUN", "simulated", True)
            return PlacedOrder(order_id="DRY-RUN", status="simulated", success=True)

        if self._clob_client is None:
            return PlacedOrder(
                order_id="", status="error", success=False,
                error="CLOB client not initialised",
            )

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=size_shares,
                side=("BUY" if side == "BUY" else "SELL"),
            )
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed, OrderType.FOK)

            success = resp.get("success", False)
            placed = PlacedOrder(
                order_id=resp.get("orderID", ""),
                status=resp.get("status", "unknown"),
                success=success,
                error=None if success else str(resp),
            )
            _log_order_csv("MARKET", side, token_id, price, size_usdc, size_shares, placed.order_id, placed.status, False)
            return placed
        except Exception as exc:
            log.error("place_market_order failed: %s", exc)
            return PlacedOrder(order_id="", status="error", success=False, error=str(exc))

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        if config.DRY_RUN:
            log.info("[CANCEL] %s (dry run)", order_id)
            return True

        if self._clob_client is None:
            return False

        try:
            self._clob_client.cancel(order_id)
            return True
        except Exception as exc:
            log.error("cancel_order(%s) failed: %s", order_id, exc)
            return False

    async def cancel_all_orders(self) -> bool:
        """Cancel all open orders for this wallet."""
        if config.DRY_RUN:
            log.info("[CANCEL ALL] (dry run)")
            return True

        if self._clob_client is None:
            return False

        try:
            self._clob_client.cancel_all()
            return True
        except Exception as exc:
            log.error("cancel_all_orders failed: %s", exc)
            return False
