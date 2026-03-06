"""
auth_probe.py — Polymarket auth + connectivity diagnostic.

Usage:
    python auth_probe.py                  # read-only probe (safe, no orders)
    python auth_probe.py --place-tiny     # place $0.10 DRY-RUN limit order
    DRY_RUN=0 python auth_probe.py        # live auth check, still no orders placed

Checks:
  1. Private key → wallet address derivation
  2. ClobClient init + create_or_derive_api_creds()
  3. _normalize_api_creds() path (robust shape handling)
  4. set_api_creds() or direct assignment fallback
  5. Balance fetch (HTTP endpoint then clob fallback)
  6. Fetch 1 page of active markets (connectivity)
  7. Fetch order book for the first market found

Nothing is printed about key/secret/passphrase values.
"""

import asyncio
import os
import sys

# ---------------------------------------------------------------------------
# Ensure project root is on path when run as a script.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


def probe_key_derivation() -> str | None:
    """Return derived wallet address from PRIVATE_KEY, or None on failure."""
    _section("1. Key → address derivation")
    pk = os.getenv("PRIVATE_KEY", "")
    if not pk:
        print("  FAIL: PRIVATE_KEY not set in environment / .env")
        return None
    try:
        from eth_account import Account
        addr = Account.from_key(pk).address
        print(f"  OK — derived address: {addr}")
        return addr
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return None


def probe_clob_auth() -> bool:
    """Initialise ClobClient, derive creds, normalise and set them."""
    _section("2. ClobClient init + cred derivation")

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("  FAIL: py-clob-client not installed (pip install py-clob-client)")
        return False

    import config
    from client import _normalize_api_creds  # reuse the hardened normalizer

    pk = config.PRIVATE_KEY
    if not pk:
        print("  FAIL: PRIVATE_KEY missing in config")
        return False

    funder = config.POLY_FUNDER or config.WALLET_ADDRESS
    sig_type = config.POLY_SIGNATURE_TYPE
    print(f"  host           : {config.CLOB_HOST}")
    print(f"  chain_id       : {config.POLYGON_CHAIN_ID}")
    print(f"  signature_type : {sig_type}")
    print(f"  funder         : {funder or '(not set — will use signing address)'}")

    try:
        client = ClobClient(
            host=config.CLOB_HOST,
            key=pk,
            chain_id=config.POLYGON_CHAIN_ID,
            signature_type=sig_type,
            funder=funder,
        )
        print("  ClobClient created OK")

        raw = client.create_or_derive_api_creds()
        print(f"  create_or_derive_api_creds() returned type: {type(raw).__name__}")

        creds = _normalize_api_creds(raw)
        print("  _normalize_api_creds() OK — api_key present:", bool(creds.api_key))

        try:
            client.set_api_creds(creds)
            print("  set_api_creds() OK")
        except Exception as exc:
            client.api_creds = creds
            print(f"  set_api_creds() raised ({exc}) — used direct assignment OK")

        confirmed = bool(getattr(client, "api_creds", None))
        print(f"  api_creds set on client: {confirmed}")
        return confirmed

    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


async def probe_balance() -> None:
    _section("3. Balance fetch")
    from client import PolymarketClient
    c = PolymarketClient()
    await c.start()
    try:
        bal, note = await c.get_balance_usdc()
        print(f"  balance: ${bal:.4f} USDC  ({note})")
    finally:
        await c.stop()


async def probe_markets() -> str | None:
    """Fetch 1 page of active markets; return first token_id found."""
    _section("4. Market list fetch (1 page)")
    from client import PolymarketClient
    c = PolymarketClient()
    await c.start()
    first_token = None
    try:
        markets = await c.get_markets(active_only=True, limit=10)
        print(f"  fetched {len(markets)} markets from first page")
        for m in markets:
            toks = m.tokens or []
            for t in toks:
                tid = t.get("token_id", "")
                if tid:
                    first_token = tid
                    print(f"  first token_id: {tid[:32]}…  (market: {m.question[:60]})")
                    break
            if first_token:
                break
        if not first_token:
            print("  WARNING: no token_ids found in returned markets")
    finally:
        await c.stop()
    return first_token


async def probe_order_book(token_id: str) -> None:
    _section(f"5. Order book for token …{token_id[-12:]}")
    from client import PolymarketClient
    c = PolymarketClient()
    await c.start()
    try:
        bid, ask = await c.get_best_prices(token_id)
        print(f"  best_bid={bid:.4f}  best_ask={ask:.4f}  mid={(bid+ask)/2:.4f}")
    finally:
        await c.stop()


async def async_main(place_tiny: bool) -> None:
    # Step 1-2 are sync
    addr = probe_key_derivation()
    auth_ok = probe_clob_auth()

    # Steps 3-5 need the async client
    await probe_balance()
    token_id = await probe_markets()
    if token_id:
        await probe_order_book(token_id)

    _section("Summary")
    print(f"  address derived : {'OK  ' + addr if addr else 'FAIL'}")
    print(f"  auth / creds    : {'OK' if auth_ok else 'FAIL'}")

    if place_tiny:
        _section("6. Dry-run limit order (--place-tiny)")
        if not token_id:
            print("  SKIP — no token_id available")
        else:
            from client import PolymarketClient
            import config
            orig = config.DRY_RUN
            config.DRY_RUN = True  # force dry regardless of env
            c = PolymarketClient()
            await c.start()
            try:
                result = await c.place_limit_order(token_id, "BUY", 0.50, 0.10)
                print(f"  order result: {result}")
            finally:
                await c.stop()
                config.DRY_RUN = orig

    print("\nProbe complete.\n")


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    place_tiny = "--place-tiny" in sys.argv
    asyncio.run(async_main(place_tiny))


if __name__ == "__main__":
    main()
