"""
preflight.py — Paper-readiness pre-flight check
================================================
Run before any bot session to verify:
  1. DRY_RUN safety (warns loudly if DRY_RUN=0)
  2. Required env vars present when DRY_RUN=0
  3. .env file loaded (optional but recommended)
  4. Critical config values are within sane ranges
  5. Active risk profile + limits printed for human review
  6. Python imports work (catches missing dependencies early)

Exit codes:
  0 — all checks passed (or passed with non-fatal warnings)
  1 — at least one FATAL check failed

Usage:
  python preflight.py              # quick check before any session
  python preflight.py --strict     # treat warnings as failures too
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Argument parsing (minimal, no argparse dependency)
# ---------------------------------------------------------------------------

STRICT = "--strict" in sys.argv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASS  = "\033[32m[PASS]\033[0m"
_WARN  = "\033[33m[WARN]\033[0m"
_FAIL  = "\033[31m[FAIL]\033[0m"
_INFO  = "\033[36m[INFO]\033[0m"

_failures = 0
_warnings = 0


def _ok(msg: str) -> None:
    print(f"  {_PASS}  {msg}")


def _warn(msg: str) -> None:
    global _warnings
    _warnings += 1
    print(f"  {_WARN}  {msg}")
    if STRICT:
        global _failures
        _failures += 1


def _fail(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"  {_FAIL}  {msg}")


def _info(msg: str) -> None:
    print(f"  {_INFO}  {msg}")


def _section(title: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


# ---------------------------------------------------------------------------
# Check 1 — .env file
# ---------------------------------------------------------------------------

def check_env_file() -> None:
    _section("1. Environment file")
    env_path = Path(".env")
    example_path = Path(".env.example")

    if env_path.exists():
        _ok(".env file found")
        # Load it so subsequent os.getenv() calls see the values
        try:
            from dotenv import load_dotenv
            load_dotenv(override=False)
            _ok("dotenv loaded successfully")
        except ImportError:
            _warn("python-dotenv not installed — values read from process env only")
    else:
        _warn(".env file not found — using environment variables directly")
        if example_path.exists():
            _info("Tip: cp .env.example .env  then fill in your values")


# ---------------------------------------------------------------------------
# Check 2 — DRY_RUN safety
# ---------------------------------------------------------------------------

def check_dry_run() -> None:
    _section("2. DRY_RUN safety")
    dry = os.getenv("DRY_RUN", "1")
    if dry == "1":
        _ok("DRY_RUN=1 — paper mode, no real orders will be placed")
    else:
        _warn(
            "DRY_RUN=0 — LIVE TRADING MODE.  "
            "Real orders will be placed and real funds are at risk."
        )
        print()
        print("  \033[31m  *** LIVE MODE WARNING ***\033[0m")
        print("  \033[31m  Ensure you have tested thoroughly in paper mode first.\033[0m")
        print()


# ---------------------------------------------------------------------------
# Check 3 — Required credentials (only fatal in live mode)
# ---------------------------------------------------------------------------

def check_credentials() -> None:
    _section("3. Credentials")
    dry = os.getenv("DRY_RUN", "1") == "1"

    pk = os.getenv("PRIVATE_KEY", "")
    wa = os.getenv("WALLET_ADDRESS", "")

    if pk and wa:
        _ok(f"PRIVATE_KEY set ({len(pk)} chars)")
        _ok(f"WALLET_ADDRESS set ({wa[:6]}...{wa[-4:]})")
    elif dry:
        _warn("PRIVATE_KEY / WALLET_ADDRESS not set — OK for DRY_RUN=1")
    else:
        if not pk:
            _fail("PRIVATE_KEY is required for live trading (DRY_RUN=0)")
        if not wa:
            _fail("WALLET_ADDRESS is required for live trading (DRY_RUN=0)")


# ---------------------------------------------------------------------------
# Check 4 — Config sanity ranges
# ---------------------------------------------------------------------------

def check_config_ranges() -> None:
    _section("4. Config sanity ranges")

    def _fenv(key: str, default: float) -> float:
        try:
            return float(os.getenv(key, str(default)))
        except ValueError:
            _fail(f"{key} is not a valid float: {os.getenv(key)!r}")
            return default

    risk_budget  = _fenv("RISK_BUDGET_PCT", 0.02)
    size_min     = _fenv("SIZE_MIN_USDC", 5.0)
    size_max     = _fenv("SIZE_MAX_USDC", 500.0)
    edge_floor   = _fenv("EDGE_FLOOR_BPS", 50.0)
    edge_ref     = _fenv("EDGE_REFERENCE_BPS", 100.0)
    conf_floor   = _fenv("CONFIDENCE_FLOOR", 0.20)
    daily_loss   = _fenv("RISK_DAILY_LOSS_LIMIT", 100.0)
    max_dd       = _fenv("RISK_MAX_DRAWDOWN", 200.0)
    max_pos      = _fenv("MAX_POSITION_USDC", 500.0)
    mm_cap_pct   = _fenv("MM_CAPITAL_PCT", 0.20)

    # Range checks
    if 0 < risk_budget <= 0.10:
        _ok(f"RISK_BUDGET_PCT={risk_budget:.3f}  (2% rec. for paper)")
    elif risk_budget > 0.10:
        _warn(f"RISK_BUDGET_PCT={risk_budget:.3f} is high (>10%); reduce for paper sessions")
    else:
        _fail(f"RISK_BUDGET_PCT={risk_budget} must be > 0")

    if size_min < size_max:
        _ok(f"SIZE_MIN_USDC={size_min}  SIZE_MAX_USDC={size_max}")
    else:
        _fail(f"SIZE_MIN_USDC ({size_min}) must be < SIZE_MAX_USDC ({size_max})")

    if edge_floor < edge_ref:
        _ok(f"EDGE_FLOOR_BPS={edge_floor}  EDGE_REFERENCE_BPS={edge_ref}")
    else:
        _warn(f"EDGE_FLOOR_BPS ({edge_floor}) >= EDGE_REFERENCE_BPS ({edge_ref}); "
              "few trades will pass the edge guard")

    if 0 < conf_floor < 1:
        _ok(f"CONFIDENCE_FLOOR={conf_floor}")
    else:
        _fail(f"CONFIDENCE_FLOOR={conf_floor} must be in (0, 1)")

    if daily_loss > 0:
        _ok(f"RISK_DAILY_LOSS_LIMIT={daily_loss} USDC")
    else:
        _fail(f"RISK_DAILY_LOSS_LIMIT={daily_loss} must be > 0")

    if max_dd >= daily_loss:
        _ok(f"RISK_MAX_DRAWDOWN={max_dd} USDC  (>= daily loss limit)")
    else:
        _warn(f"RISK_MAX_DRAWDOWN ({max_dd}) < RISK_DAILY_LOSS_LIMIT ({daily_loss}); "
              "drawdown CB will fire before daily-loss CB")

    if size_max <= max_pos:
        _ok(f"SIZE_MAX_USDC={size_max} <= MAX_POSITION_USDC={max_pos}")
    else:
        _warn(f"SIZE_MAX_USDC ({size_max}) > MAX_POSITION_USDC ({max_pos}); "
              "sizer cap may conflict with position cap")

    if 0 < mm_cap_pct <= 0.50:
        _ok(f"MM_CAPITAL_PCT={mm_cap_pct:.2f}")
    else:
        _warn(f"MM_CAPITAL_PCT={mm_cap_pct:.2f} is outside typical range (0–50%)")


# ---------------------------------------------------------------------------
# Check 5 — Active risk profile summary
# ---------------------------------------------------------------------------

def print_active_profile() -> None:
    _section("5. Active risk profile")

    quant = os.getenv("QUANT_MODE_ENABLED", "0") == "1"
    profile_name = os.getenv("PARAM_PROFILE", "auto")

    _info(f"QUANT_MODE_ENABLED : {'ON' if quant else 'OFF'}")
    _info(f"PARAM_PROFILE      : {profile_name}")

    if not quant:
        _info("(Signal engine, risk engine, kill-switch: all dormant — QUANT_MODE_ENABLED=0)")
        return

    try:
        import config as _cfg
        from risk.param_profiles import get_active_profile

        profile = get_active_profile(regime="unknown")
        pn = profile.get("profile_name", profile_name)
        _info(f"Resolved profile   : {pn}")
        _info(f"  signal_confidence_threshold : {profile['signal_confidence_threshold']}")
        _info(f"  exec_min_edge               : {profile['exec_min_edge']}")
        _info(f"  market_maker_spread         : {profile['market_maker_spread']}")
        _info(f"  mm_capital_pct_scale        : {profile['mm_capital_pct_scale']}")
        _info("")
        _info("Hard risk limits (from config):")
        _info(f"  RISK_DAILY_LOSS_LIMIT       : {_cfg.RISK_DAILY_LOSS_LIMIT} USDC")
        _info(f"  RISK_MAX_DRAWDOWN           : {_cfg.RISK_MAX_DRAWDOWN} USDC")
        _info(f"  RISK_MAX_MARKET_EXPOSURE    : {_cfg.RISK_MAX_MARKET_EXPOSURE} USDC")
        _info(f"  RISK_MAX_PORTFOLIO_EXPOSURE : {_cfg.RISK_MAX_PORTFOLIO_EXPOSURE} USDC")
        _info(f"  RISK_MAX_CONSECUTIVE_LOSSES : {_cfg.RISK_MAX_CONSECUTIVE_LOSSES}")
        _info(f"  RISK_COOLDOWN_SECS          : {_cfg.RISK_COOLDOWN_SECS} s")
        _info("")
        _info("Kill-switch thresholds:")
        _info(f"  API error rate  T1={_cfg.KS_TIER1_API_ERROR_RATE}  "
              f"T2={_cfg.KS_TIER2_API_ERROR_RATE}  T3={_cfg.KS_TIER3_API_ERROR_RATE}")
        _info(f"  Stale secs      T1={_cfg.KS_TIER1_STALE_SECS}s  "
              f"T2={_cfg.KS_TIER2_STALE_SECS}s  T3={_cfg.KS_TIER3_STALE_SECS}s")
        _info(f"  DD pace %       T1={_cfg.KS_TIER1_DD_PACE_PCT}  "
              f"T2={_cfg.KS_TIER2_DD_PACE_PCT}  T3={_cfg.KS_TIER3_DD_PACE_PCT}")
    except Exception as exc:
        _warn(f"Could not load profile details: {exc}")


# ---------------------------------------------------------------------------
# Check 6 — Critical imports
# ---------------------------------------------------------------------------

def check_imports() -> None:
    _section("6. Critical imports")

    required = [
        ("aiohttp",         "aiohttp"),
        ("dotenv",          "python-dotenv"),
        ("py_clob_client",  "py-clob-client"),
        ("websockets",      "websockets"),
    ]
    optional_quant = [
        ("numpy",   "numpy"),
        ("pandas",  "pandas"),
    ]
    internal = [
        "config", "client", "bot",
        "risk.sizing", "risk.kill_switch", "risk.risk_engine",
        "signals.signal_engine", "execution.execution_manager",
        "analytics.trade_logger",
    ]

    for mod, pkg in required:
        if importlib.util.find_spec(mod) is not None:
            _ok(f"{pkg}")
        else:
            _fail(f"{pkg} not found — run: pip install {pkg}")

    quant = os.getenv("QUANT_MODE_ENABLED", "0") == "1"
    for mod, pkg in optional_quant:
        if importlib.util.find_spec(mod) is not None:
            _ok(f"{pkg} (optional)")
        elif quant:
            _warn(f"{pkg} not found — may be needed by quant analytics")
        else:
            _info(f"{pkg} not installed (optional, not needed when QUANT_MODE_ENABLED=0)")

    for mod in internal:
        try:
            importlib.import_module(mod)
            _ok(f"{mod}")
        except ImportError as exc:
            _fail(f"{mod} import error: {exc}")
        except Exception as exc:
            _warn(f"{mod} loaded with warning: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    print("=" * 60)
    print("  POLYMARKET BOT — PRE-FLIGHT CHECK")
    print(f"  {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if STRICT:
        print("  Mode: strict (warnings are failures)")
    print("=" * 60)

    check_env_file()
    check_dry_run()
    check_credentials()
    check_config_ranges()
    print_active_profile()
    check_imports()

    print()
    print("=" * 60)
    if _failures == 0 and _warnings == 0:
        print("  \033[32mAll checks passed.\033[0m  Ready to run.")
    elif _failures == 0:
        print(f"  \033[33mPassed with {_warnings} warning(s).\033[0m  Review above before proceeding.")
    else:
        print(f"  \033[31m{_failures} check(s) FAILED.\033[0m  Fix before starting a session.")
    print("=" * 60)
    print()

    return 1 if _failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
