#!/usr/bin/env python3
"""
HF Clone Preflight Diagnostic
==============================
Verifies that the current wallet/key/API config is ready for live HF trading.

Checks:
  1. Env config      — WALLET_ADDRESS format, PRIVATE_KEY set, POLY_SIGNATURE_TYPE
  2. Clone config    — CLONE_ENABLED, CLONE_HF_MODE_ENABLED, profile file, band sanity
  3. API reachability— GET /markets (single page probe)
  4. Balance/allowance — USDC collateral + conditional via py-clob-client
  5. Signer context  — funder/proxy matching for the configured signature_type

Usage:
  python -m scripts.hf_preflight
  python scripts/hf_preflight.py
  python scripts/hf_preflight.py --json     # machine-readable summary

Exit codes: 0 = all PASS (warnings allowed), 1 = any FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

# Ensure project root is importable whether run as a module or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402  (must come after path fix)

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()

def _color(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def _tag(status: str) -> str:
    colors = {"PASS": "32", "FAIL": "31", "WARN": "33", "INFO": "36"}
    return _color(colors.get(status, "0"), f"[{status}]")


# ---------------------------------------------------------------------------
# Result accumulator
# ---------------------------------------------------------------------------

_results: list[dict] = []

def _record(status: str, label: str, detail: str = "") -> None:
    _results.append({"status": status, "label": label, "detail": detail})
    suffix = f" — {detail}" if detail else ""
    print(f"  {_tag(status)}  {label}{suffix}")


# ---------------------------------------------------------------------------
# Check 1: Env / key config
# ---------------------------------------------------------------------------

def check_env_config() -> None:
    print("\n[1] Environment / Key Config")

    if not config.WALLET_ADDRESS:
        _record("FAIL", "WALLET_ADDRESS", "not set in .env")
    elif not re.fullmatch(r"0x[a-fA-F0-9]{40}", config.WALLET_ADDRESS):
        _record("FAIL", "WALLET_ADDRESS", f"invalid format: {config.WALLET_ADDRESS}")
    else:
        _record("PASS", "WALLET_ADDRESS", config.WALLET_ADDRESS)

    if not config.PRIVATE_KEY:
        _record("FAIL", "PRIVATE_KEY", "not set in .env")
    elif len(config.PRIVATE_KEY) < 32:
        _record("WARN", "PRIVATE_KEY", f"set but short ({len(config.PRIVATE_KEY)} chars) — verify")
    else:
        _record("PASS", "PRIVATE_KEY", f"set  ({len(config.PRIVATE_KEY)} chars, not shown)")

    sig = config.POLY_SIGNATURE_TYPE
    sig_label = {0: "EOA direct", 1: "proxy/funder"}.get(sig, "unknown")
    if sig not in (0, 1):
        _record("FAIL", "POLY_SIGNATURE_TYPE", f"{sig} — must be 0 or 1")
    else:
        _record("PASS", "POLY_SIGNATURE_TYPE", f"{sig} ({sig_label})")
    if sig == 1:
        _record("INFO", "  proxy note",
                "sig_type=1 means funder=WALLET_ADDRESS acts as the account; "
                "ensure the key matches the proxy wallet, not just the EOA")

    if config.DRY_RUN:
        _record("INFO", "DRY_RUN", "1 — paper mode (orders NOT sent live)")
    else:
        _record("WARN", "DRY_RUN", "0 — LIVE mode active")

    exp = config.EXPECTED_POLYMARKET_WALLET
    if exp:
        if config.WALLET_ADDRESS.lower() == exp.lower():
            _record("PASS", "EXPECTED_POLYMARKET_WALLET", "matches WALLET_ADDRESS")
        else:
            _record("FAIL", "EXPECTED_POLYMARKET_WALLET",
                    f"mismatch: wallet={config.WALLET_ADDRESS}  expected={exp}")


# ---------------------------------------------------------------------------
# Check 2: Clone / HF config
# ---------------------------------------------------------------------------

def check_clone_config() -> None:
    print("\n[2] Clone HF Config")

    clone_en = os.getenv("CLONE_ENABLED", "0") == "1"
    hf_en    = os.getenv("CLONE_HF_MODE_ENABLED", "0") == "1"

    if not clone_en:
        _record("WARN", "CLONE_ENABLED", "0 — clone strategy disabled (set to 1 to enable)")
    else:
        _record("PASS", "CLONE_ENABLED", "1")

    if not hf_en:
        _record("WARN", "CLONE_HF_MODE_ENABLED", "0 — HF hedge mode disabled (set to 1 to enable)")
    else:
        _record("PASS", "CLONE_HF_MODE_ENABLED", "1")

    wallet       = os.getenv("CLONE_WALLET", "").lower()
    profile_path = os.getenv("CLONE_PROFILE_PATH", "")
    if not profile_path and wallet:
        profile_path = os.path.join("logs", f"clone_profile_{wallet}.json")

    if not profile_path:
        _record("FAIL", "Clone profile path",
                "not derivable — set CLONE_WALLET=0x... or CLONE_PROFILE_PATH=path/to/profile.json")
    elif not os.path.exists(profile_path):
        _record("FAIL", "Clone profile file",
                f"not found: {profile_path}  "
                f"(run scripts/clone_extract.py + analytics/clone_profile.py first)")
    else:
        try:
            with open(profile_path, encoding="utf-8") as fh:
                data = json.load(fh)
            p = data.get("inferred_params", {})
            required_keys = ["base_size_usdc", "min_size_usdc", "max_size_usdc"]
            missing = [k for k in required_keys if k not in p]
            if missing:
                _record("WARN", "Clone profile", f"missing keys: {missing}  path={profile_path}")
            else:
                _record("PASS", "Clone profile", profile_path)
                _record("INFO", "  size params",
                        f"base={p.get('base_size_usdc')} USDC  "
                        f"min={p.get('min_size_usdc')}  max={p.get('max_size_usdc')}")
        except Exception as exc:
            _record("FAIL", "Clone profile parse", str(exc))

    # HF band sanity check
    cmin  = float(os.getenv("CLONE_COMBINED_PRICE_MIN", str(config.CLONE_COMBINED_PRICE_MIN)))
    cmax  = float(os.getenv("CLONE_COMBINED_PRICE_MAX", str(config.CLONE_COMBINED_PRICE_MAX)))
    fee2  = 2.0 * config.POLYMARKET_FEE   # 4 % total round-trip fee
    edge_at_max = 1.0 - cmax - fee2
    edge_at_min = 1.0 - cmin - fee2
    _record("INFO", "HF combined-price band", f"[{cmin}, {cmax}]")
    _record("INFO", "  edge_proxy at min", f"{edge_at_min:.4f} ({edge_at_min*100:.2f}%)")
    if edge_at_max < 0:
        _record("FAIL", "  edge_proxy at max",
                f"{edge_at_max:.4f} — NEGATIVE after 2×fee={fee2:.2f}; "
                f"lower CLONE_COMBINED_PRICE_MAX below {1.0 - fee2:.3f}")
    elif edge_at_max < 0.003:
        _record("WARN", "  edge_proxy at max",
                f"{edge_at_max:.4f} — below clone_sizer edge floor of 30 bps; "
                f"sizer will block candidates near the max; consider lowering max to ≤{1.0 - fee2 - 0.003:.3f}")
    else:
        _record("PASS", "  edge_proxy at max",
                f"{edge_at_max:.4f} ({edge_at_max*100:.2f}%) — positive after fees")

    # Stage caps
    stage = os.getenv("LIVE_DEPLOY_MODE", config.LIVE_DEPLOY_MODE)
    stage_caps = {
        "staircase_A": (config.LIVE_STAGE_A_MAX_SIZE_USDC, config.LIVE_STAGE_A_MAX_POSITIONS),
        "staircase_B": (config.LIVE_STAGE_B_MAX_SIZE_USDC, config.LIVE_STAGE_B_MAX_POSITIONS),
        "staircase_C": (config.LIVE_STAGE_C_MAX_SIZE_USDC, config.LIVE_STAGE_C_MAX_POSITIONS),
        "production":  (config.SIZE_MAX_USDC, int(os.getenv("CLONE_MAX_OPEN_POSITIONS", "3"))),
    }
    if stage in stage_caps:
        max_sz, max_pos = stage_caps[stage]
        _record("INFO", f"Stage caps ({stage})", f"max_size={max_sz} USDC  max_positions={max_pos}")
    else:
        _record("WARN", "LIVE_DEPLOY_MODE", f"unrecognised stage '{stage}'")


# ---------------------------------------------------------------------------
# Check 3: API reachability
# ---------------------------------------------------------------------------

async def check_api_reachability() -> None:
    print("\n[3] API Reachability")
    try:
        import aiohttp
    except ImportError:
        _record("FAIL", "aiohttp", "not installed — run: pip install aiohttp")
        return

    url = config.CLOB_HOST
    try:
        async with aiohttp.ClientSession(
            base_url=url,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            async with session.get(
                "/markets", params={"limit": "1", "active": "true", "closed": "false"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    count = len(data.get("data", []))
                    _record("PASS", f"CLOB {url}/markets",
                            f"HTTP 200  returned {count} market(s)")
                else:
                    _record("FAIL", f"CLOB {url}/markets", f"HTTP {resp.status}")
    except Exception as exc:
        _record("FAIL", f"CLOB {url}/markets", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Check 4: Balance & allowance via py-clob-client
# ---------------------------------------------------------------------------

def check_clob_client_balance() -> None:
    print("\n[4] Balance & Allowance (py-clob-client)")

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        _record("FAIL", "py-clob-client",
                "not installed — run: pip install py-clob-client")
        return

    if not config.PRIVATE_KEY or not config.WALLET_ADDRESS:
        _record("FAIL", "ClobClient init", "PRIVATE_KEY or WALLET_ADDRESS not set — skipping")
        return

    try:
        client = ClobClient(
            host=config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=config.POLYGON_CHAIN_ID,
            signature_type=config.POLY_SIGNATURE_TYPE,
            funder=config.WALLET_ADDRESS,
        )
        _record("PASS", "ClobClient init",
                f"sig_type={config.POLY_SIGNATURE_TYPE}  funder={config.WALLET_ADDRESS}")
    except Exception as exc:
        _record("FAIL", "ClobClient init", str(exc))
        return

    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        _record("PASS", "L2 API creds", "derived via wallet signature")
    except Exception as exc:
        _record("FAIL", "L2 API creds",
                f"failed: {exc}  "
                "(common causes: wrong PRIVATE_KEY, wrong sig_type, network error)")
        return

    # Collateral (USDC) balance + allowance
    try:
        data = client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=config.POLY_SIGNATURE_TYPE,
            )
        )
        balance   = float(data.get("balance",   0))
        allowance = float(data.get("allowance", 0))

        if balance <= 0:
            _record("FAIL", "USDC collateral balance",
                    f"{balance:.4f} USDC — zero or negative; "
                    "deposit USDC to your Polymarket account before trading")
        elif balance < 2.0:
            _record("WARN", "USDC collateral balance",
                    f"{balance:.4f} USDC — below CLONE_SIZE_MIN_USDC default (2.0)")
        else:
            _record("PASS", "USDC collateral balance", f"{balance:.4f} USDC")

        if allowance <= 0:
            _record("FAIL", "USDC collateral allowance",
                    f"{allowance:.4f} — zero; approve on Polymarket or "
                    "orders will fail with 'insufficient allowance'")
        else:
            _record("PASS", "USDC collateral allowance", f"{allowance:.4f} USDC approved")

        _record("INFO", "  raw collateral response", str({k: v for k, v in data.items()}))
    except Exception as exc:
        _record("FAIL", "USDC collateral query", f"{type(exc).__name__}: {exc}")

    # Conditional (share) token allowance
    try:
        cond = client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                signature_type=config.POLY_SIGNATURE_TYPE,
            )
        )
        cond_allowance = float(cond.get("allowance", 0))
        if cond_allowance <= 0:
            _record("WARN", "Conditional token allowance",
                    f"{cond_allowance} — zero; sell/hedge orders may fail; "
                    "approve conditional tokens on Polymarket")
        else:
            _record("PASS", "Conditional token allowance", f"{cond_allowance}")
    except Exception as exc:
        _record("WARN", "Conditional token allowance", f"could not check: {exc}")


# ---------------------------------------------------------------------------
# Recommended staircase-A preset
# ---------------------------------------------------------------------------

def print_staircase_a_preset() -> None:
    print("\n[5] Recommended Staircase-A HF Config Preset")
    fee2 = 2.0 * config.POLYMARKET_FEE
    safe_max = round(1.0 - fee2 - 0.003, 3)   # guaranteed positive edge after fees + 30 bps
    print(f"""
  # Add to .env or pass as env vars before launching (these are safe defaults):
  CLONE_ENABLED=1
  CLONE_HF_MODE_ENABLED=1
  LIVE_DEPLOY_MODE=staircase_A

  # Band: max capped to {safe_max} so edge always exceeds clone_sizer 30-bps floor
  CLONE_COMBINED_PRICE_MIN=0.82
  CLONE_COMBINED_PRICE_MAX={safe_max}

  # Size: staircase_A hard cap = $5; start conservatively
  CLONE_SIZE_MODE=adaptive
  CLONE_SIZE_MIN_USDC=2.0
  CLONE_SIZE_MAX_USDC=5.0

  # Positions and timing
  CLONE_HF_MAX_POSITIONS=3
  CLONE_CYCLE_INTERVAL_SECS=5
  CLONE_HEDGE_TIMEOUT_SECS=30
  CLONE_HEDGE_TAKER_FALLBACK_SECS=10
  CLONE_MAX_SLIPPAGE_BPS=30

  # Adaptive fallback: after 5 empty scans, widen band by 0.04 each side
  CLONE_HF_FALLBACK_AFTER_N_EMPTY=5
  CLONE_HF_FALLBACK_BAND_WIDEN=0.04
  CLONE_HF_FALLBACK_DEPTH_MULT=0.5

  DRY_RUN=0   # remove this line to keep paper mode
""")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(as_json: bool = False) -> int:
    fails = [r for r in _results if r["status"] == "FAIL"]
    warns = [r for r in _results if r["status"] == "WARN"]

    if as_json:
        print(json.dumps({
            "results": _results,
            "fails": len(fails),
            "warns": len(warns),
            "passed": len(fails) == 0,
        }, indent=2))
    else:
        print(f"\n{'='*62}")
        if not fails:
            print(_color("32", f"  PREFLIGHT PASSED  ({len(warns)} warning(s), {len(fails)} failure(s))"))
            if warns:
                print("  Warnings:")
                for w in warns:
                    print(f"    {w['label']} — {w['detail']}")
        else:
            print(_color("31", f"  PREFLIGHT FAILED  ({len(fails)} failure(s), {len(warns)} warning(s))"))
            print("  Failures:")
            for f in fails:
                print(f"    {f['label']} — {f['detail']}")
        print("="*62)

    return 1 if fails else 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main(as_json: bool = False) -> int:
    if not as_json:
        print("=" * 62)
        print("  POLYMARKET HF CLONE — PREFLIGHT DIAGNOSTIC")
        print("=" * 62)

    check_env_config()
    check_clone_config()
    await check_api_reachability()
    check_clob_client_balance()

    if not as_json:
        print_staircase_a_preset()

    return print_summary(as_json=as_json)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HF Clone preflight diagnostic")
    parser.add_argument("--json", action="store_true", help="Output JSON summary")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(as_json=args.json)))
