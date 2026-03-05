"""
Polymarket Wallet Clone — Data Extraction
==========================================
Fetches publicly available trade history and positions for a target wallet
from Polymarket's public data APIs (no authentication required).

Public endpoints used:
  data-api.polymarket.com/activity   — paginated trade history
  data-api.polymarket.com/positions  — current/closed position data
  gamma-api.polymarket.com/markets   — supplemental market metadata

Output:
  logs/clone_source_<wallet>.json   — raw merged records
  logs/clone_source_<wallet>.csv    — flat CSV for profiling/backtest

Usage:
  python scripts/clone_extract.py --wallet 0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd
  python scripts/clone_extract.py --wallet 0x... --limit 2000 --out-dir logs/
  python scripts/clone_extract.py --wallet 0x... --only-trades
  python scripts/clone_extract.py --wallet 0x... --only-positions
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import sys
import time
from typing import Any, Optional

import aiohttp

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/clone_extract.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("clone_extract")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_API   = "https://data-api.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"

# Respectful rate limit: max 2 requests per second
RATE_LIMIT_DELAY = 0.55   # seconds between requests
MAX_RETRIES      = 5
RETRY_BASE_DELAY = 1.0
PAGE_SIZE        = 100    # records per page

# CSV columns — union of activity + position fields we care about
CSV_COLUMNS = [
    "record_type",        # "trade" | "position"
    "timestamp",
    "transaction_hash",
    "condition_id",
    "question",
    "outcome",            # "Yes" | "No"
    "side",               # "BUY" | "SELL" | ""
    "price",
    "size_shares",
    "size_usdc",
    "avg_price",          # positions: avg entry price
    "current_value",      # positions: current market value
    "pnl_usdc",           # positions: realised/unrealised P&L
    "end_date",           # market end/resolution date
    "start_date",         # market start/creation date
    "market_duration_days",  # end_date - start_date in days (market lifetime)
    "liquidity_usdc",     # total USDC liquidity in market at snapshot time
    "category",           # market category tag
    "market_slug",
    "raw_json",           # full JSON for archival
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: Optional[dict] = None,
    *,
    max_retries: int = MAX_RETRIES,
) -> Any:
    """GET with exponential-backoff retry on 429/502/503/504."""
    delay = RETRY_BASE_DELAY
    for attempt in range(max_retries):
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", delay))
                    jitter = random.uniform(0, retry_after * 0.2)
                    log.warning("Rate-limited (429) — waiting %.1fs", retry_after + jitter)
                    await asyncio.sleep(retry_after + jitter)
                    continue
                if resp.status in (502, 503, 504) and attempt < max_retries - 1:
                    jitter = random.uniform(0, delay * 0.3)
                    log.warning("HTTP %d — retry %d in %.1fs", resp.status, attempt + 1, delay + jitter)
                    await asyncio.sleep(delay + jitter)
                    delay *= 2
                    continue
                if resp.status == 404:
                    log.debug("404 at %s — endpoint not supported", url)
                    return None
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientResponseError as exc:
            if exc.status in (400, 401, 403, 404):
                log.debug("Client error %d at %s — skipping", exc.status, url)
                return None
            if attempt < max_retries - 1:
                jitter = random.uniform(0, delay * 0.3)
                await asyncio.sleep(delay + jitter)
                delay *= 2
                continue
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < max_retries - 1:
                jitter = random.uniform(0, delay * 0.3)
                log.warning("Network error (attempt %d): %s — retrying in %.1fs", attempt + 1, exc, delay + jitter)
                await asyncio.sleep(delay + jitter)
                delay *= 2
                continue
            raise
    return None


async def _paginate(
    session: aiohttp.ClientSession,
    base_url: str,
    params: dict,
    *,
    limit: int = PAGE_SIZE,
    max_records: int = 5000,
) -> list[dict]:
    """Paginate an endpoint using offset-based pagination."""
    all_records: list[dict] = []
    offset = 0
    page_size = min(limit, PAGE_SIZE)

    while len(all_records) < max_records:
        page_params = {**params, "limit": page_size, "offset": offset}
        log.debug("Fetching %s offset=%d", base_url, offset)

        data = await _get_json(session, base_url, page_params)
        await asyncio.sleep(RATE_LIMIT_DELAY)   # polite rate limiting

        if data is None:
            log.warning("Null response at offset %d — stopping pagination", offset)
            break

        # Some endpoints return a list directly, others wrap in {"data": [...]}
        if isinstance(data, list):
            page_records = data
        elif isinstance(data, dict):
            page_records = data.get("data", data.get("results", data.get("items", [])))
            if not isinstance(page_records, list):
                log.warning("Unexpected response shape at offset %d: %s", offset, type(page_records))
                break
        else:
            break

        if not page_records:
            log.debug("Empty page at offset %d — done", offset)
            break

        all_records.extend(page_records)
        log.debug("  +%d records (total %d)", len(page_records), len(all_records))

        if len(page_records) < page_size:
            # Last page
            break

        offset += page_size

    return all_records


# ---------------------------------------------------------------------------
# Extraction functions
# ---------------------------------------------------------------------------

async def fetch_trades(
    session: aiohttp.ClientSession,
    wallet: str,
    max_records: int = 5000,
) -> list[dict]:
    """Fetch trade activity from data-api.polymarket.com/activity."""
    log.info("Fetching trade activity for wallet %s …", wallet)
    url = f"{DATA_API}/activity"
    params = {"user": wallet, "type": "TRADE"}
    records = await _paginate(session, url, params, max_records=max_records)
    log.info("  -> %d trade records fetched", len(records))
    return records


async def fetch_positions(
    session: aiohttp.ClientSession,
    wallet: str,
    max_records: int = 2000,
) -> list[dict]:
    """Fetch position data from data-api.polymarket.com/positions."""
    log.info("Fetching positions for wallet %s …", wallet)
    url = f"{DATA_API}/positions"
    params = {"user": wallet, "sizeThreshold": "0"}
    records = await _paginate(session, url, params, max_records=max_records)
    log.info("  -> %d position records fetched", len(records))
    return records


async def enrich_with_market_metadata(
    session: aiohttp.ClientSession,
    records: list[dict],
    condition_id_field: str = "market",
) -> None:
    """Supplement records with market question/end_date/category from Gamma API.

    Modifies records in-place.  Missing fields are left as empty string.
    """
    # Collect unique condition IDs
    cids = {r.get(condition_id_field, "") for r in records if r.get(condition_id_field)}
    cids.discard("")
    log.info("Enriching %d unique markets from Gamma API …", len(cids))

    meta_cache: dict[str, dict] = {}
    for cid in cids:
        if not cid:
            continue
        url = f"{GAMMA_API}/markets"
        data = await _get_json(session, url, {"condition_id": cid})
        await asyncio.sleep(RATE_LIMIT_DELAY)

        if data is None:
            continue

        markets = data if isinstance(data, list) else data.get("data", [])
        if markets:
            m = markets[0]
            start_date = (m.get("startDate") or m.get("start_date") or
                          m.get("startTime") or m.get("createdAt") or "")
            end_date   = m.get("endDate", m.get("end_date", ""))
            # Market duration in days (end - start)
            dur_days = _market_duration_days(start_date, end_date)
            # Liquidity: try several field names Gamma uses
            liquidity = _coerce_float(
                m.get("liquidity") or m.get("liquidityNum") or
                m.get("volume24hr") or m.get("liquidityClob") or 0
            )
            meta_cache[cid] = {
                "question":            m.get("question", ""),
                "end_date":            end_date,
                "start_date":          start_date,
                "market_duration_days": dur_days,
                "liquidity_usdc":      liquidity,
                "category":            _extract_category(m),
                "market_slug":         m.get("slug", m.get("marketSlug", "")),
            }

    enriched = 0
    for r in records:
        cid = r.get(condition_id_field, "")
        if cid in meta_cache:
            r.setdefault("_meta", {}).update(meta_cache[cid])
            enriched += 1

    log.info("  -> enriched %d / %d records", enriched, len(records))


def _extract_category(market: dict) -> str:
    """Pull category tag from various Polymarket market schemas."""
    for field in ("category", "tags", "groupItemTitle", "eventSlug"):
        val = market.get(field)
        if isinstance(val, str) and val:
            return val.lower()
        if isinstance(val, list) and val:
            return str(val[0]).lower()
    return "unknown"


def _market_duration_days(start_date: str, end_date: str) -> float:
    """Compute market lifetime in days from start_date to end_date strings."""
    if not start_date or not end_date:
        return 0.0
    import datetime as _dt
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            s = _dt.datetime.strptime(start_date[:19], fmt[:len(fmt)])
            e = _dt.datetime.strptime(end_date[:19],   fmt[:len(fmt)])
            return max(0.0, (e - s).total_seconds() / 86400.0)
        except (ValueError, TypeError):
            continue
    return 0.0


# ---------------------------------------------------------------------------
# Normalisation → CSV rows
# ---------------------------------------------------------------------------

def _normalise_trade(r: dict) -> dict:
    """Map a raw trade record to CSV schema."""
    meta = r.get("_meta", {})
    # Handle both camelCase and snake_case field names
    size_usdc = _coerce_float(r.get("usdcSize") or r.get("amount") or r.get("usdc_size"))
    size_shares = _coerce_float(r.get("size") or r.get("shares"))
    price = _coerce_float(r.get("price"))
    # Infer missing size if possible
    if size_usdc == 0.0 and size_shares > 0 and price > 0:
        size_usdc = size_shares * price
    if size_shares == 0.0 and size_usdc > 0 and price > 0:
        size_shares = size_usdc / price

    end_date   = r.get("endDate") or r.get("end_date") or meta.get("end_date", "")
    start_date = r.get("startDate") or r.get("start_date") or meta.get("start_date", "")
    return {
        "record_type":          "trade",
        "timestamp":            r.get("timestamp") or r.get("createdAt") or r.get("created_at") or "",
        "transaction_hash":     r.get("transactionHash") or r.get("transaction_hash") or "",
        "condition_id":         r.get("market") or r.get("conditionId") or r.get("condition_id") or "",
        "question":             r.get("question") or meta.get("question", ""),
        "outcome":              r.get("outcome") or r.get("side_outcome") or "",
        "side":                 (r.get("side") or "").upper(),
        "price":                price,
        "size_shares":          size_shares,
        "size_usdc":            size_usdc,
        "avg_price":            "",
        "current_value":        "",
        "pnl_usdc":             "",
        "end_date":             end_date,
        "start_date":           start_date,
        "market_duration_days": meta.get("market_duration_days", ""),
        "liquidity_usdc":       meta.get("liquidity_usdc", ""),
        "category":             r.get("category") or meta.get("category", "unknown"),
        "market_slug":          r.get("slug") or r.get("marketSlug") or meta.get("market_slug", ""),
        "raw_json":             json.dumps(r, default=str),
    }


def _normalise_position(r: dict) -> dict:
    """Map a raw position record to CSV schema."""
    meta = r.get("_meta", {})
    end_date   = r.get("endDate") or r.get("end_date") or meta.get("end_date", "")
    start_date = r.get("startDate") or r.get("start_date") or meta.get("start_date", "")
    return {
        "record_type":          "position",
        "timestamp":            r.get("startDate") or r.get("start_date") or r.get("createdAt") or "",
        "transaction_hash":     "",
        "condition_id":         r.get("conditionId") or r.get("condition_id") or r.get("market") or "",
        "question":             r.get("title") or r.get("question") or meta.get("question", ""),
        "outcome":              r.get("outcome") or "",
        "side":                 "",
        "price":                _coerce_float(r.get("curPrice") or r.get("cur_price") or r.get("price")),
        "size_shares":          _coerce_float(r.get("size") or r.get("shares")),
        "size_usdc":            _coerce_float(r.get("initialValue") or r.get("usdcSize") or r.get("amount")),
        "avg_price":            _coerce_float(r.get("avgPrice") or r.get("avg_price")),
        "current_value":        _coerce_float(r.get("currentValue") or r.get("value")),
        "pnl_usdc":             _coerce_float(r.get("pnlPerShare") or r.get("pnl") or r.get("unrealized")),
        "end_date":             end_date,
        "start_date":           start_date,
        "market_duration_days": meta.get("market_duration_days", ""),
        "liquidity_usdc":       meta.get("liquidity_usdc", ""),
        "category":             r.get("category") or meta.get("category", "unknown"),
        "market_slug":          r.get("slug") or r.get("marketSlug") or meta.get("market_slug", ""),
        "raw_json":             json.dumps(r, default=str),
    }


def _coerce_float(val: Any) -> float:
    """Best-effort float conversion; returns 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

async def extract(
    wallet: str,
    out_dir: str = "logs",
    max_trades: int = 5000,
    max_positions: int = 2000,
    only_trades: bool = False,
    only_positions: bool = False,
    enrich: bool = True,
) -> tuple[str, str]:
    """Run full extraction pipeline.

    Returns (csv_path, json_path) of saved files.
    """
    os.makedirs(out_dir, exist_ok=True)
    wallet_lower = wallet.lower()
    slug = wallet_lower[:10]  # short label for file names
    csv_path  = os.path.join(out_dir, f"clone_source_{wallet_lower}.csv")
    json_path = os.path.join(out_dir, f"clone_source_{wallet_lower}.json")

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=4)
    headers = {"User-Agent": "polymarket-clone-extractor/1.0 (research)"}

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        # --- Fetch ---
        raw_trades: list[dict] = []
        raw_positions: list[dict] = []

        if not only_positions:
            raw_trades = await fetch_trades(session, wallet, max_records=max_trades)

        if not only_trades:
            raw_positions = await fetch_positions(session, wallet, max_records=max_positions)

        # --- Enrich with Gamma market metadata ---
        if enrich:
            if raw_trades:
                await enrich_with_market_metadata(session, raw_trades, condition_id_field="market")
            if raw_positions:
                await enrich_with_market_metadata(session, raw_positions, condition_id_field="conditionId")

        # --- Normalise ---
        rows: list[dict] = []
        for r in raw_trades:
            rows.append(_normalise_trade(r))
        for r in raw_positions:
            rows.append(_normalise_position(r))

        log.info("Normalised %d total records (%d trades + %d positions)",
                 len(rows), len(raw_trades), len(raw_positions))

        # --- Save JSON (raw) ---
        raw_all = {"wallet": wallet, "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "trades": raw_trades, "positions": raw_positions}
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(raw_all, fh, indent=2, default=str)
        log.info("Saved raw JSON -> %s", json_path)

        # --- Save CSV ---
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        log.info("Saved CSV (%d rows) -> %s", len(rows), csv_path)

    return csv_path, json_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Polymarket wallet trade/position history")
    parser.add_argument("--wallet", required=True, help="Polygon wallet address (0x...)")
    parser.add_argument("--limit", type=int, default=5000, help="Max trade records to fetch (default 5000)")
    parser.add_argument("--position-limit", type=int, default=2000, help="Max position records (default 2000)")
    parser.add_argument("--out-dir", default="logs", help="Output directory (default: logs/)")
    parser.add_argument("--only-trades", action="store_true", help="Skip positions fetch")
    parser.add_argument("--only-positions", action="store_true", help="Skip trades fetch")
    parser.add_argument("--no-enrich", action="store_true", help="Skip Gamma market metadata enrichment")
    args = parser.parse_args()

    if not args.wallet.startswith("0x") or len(args.wallet) != 42:
        parser.error("--wallet must be a 42-char 0x... EVM address")

    log.info("=" * 60)
    log.info("  POLYMARKET WALLET DATA EXTRACTION")
    log.info("  Target wallet: %s", args.wallet)
    log.info("  Trade limit:   %d", args.limit)
    log.info("  Output dir:    %s", args.out_dir)
    log.info("=" * 60)

    csv_path, json_path = asyncio.run(
        extract(
            wallet=args.wallet,
            out_dir=args.out_dir,
            max_trades=args.limit,
            max_positions=args.position_limit,
            only_trades=args.only_trades,
            only_positions=args.only_positions,
            enrich=not args.no_enrich,
        )
    )

    log.info("=" * 60)
    log.info("  EXTRACTION COMPLETE")
    log.info("  CSV:  %s", csv_path)
    log.info("  JSON: %s", json_path)
    log.info("  Next: python analytics/clone_profile.py --wallet %s", args.wallet)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
