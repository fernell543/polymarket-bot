<#
.SYNOPSIS
    Polymarket Wallet Clone — HF Hedge Mode: Paper Execution

.DESCRIPTION
    Runs the bot with HF hedge mode ACTIVE in paper (DRY_RUN=1) mode.
    Places paired YES+NO orders on markets where combined_price falls within
    the target band.  Manages a per-position state machine: collecting →
    partially_filled → hedged/aborted.  No real money is used.

    Prerequisites:
      1. .\run_clone_extract.ps1 -Wallet <wallet>
      2. .\run_clone_profile.ps1 -Wallet <wallet>
      (profile JSON must exist at logs/clone_profile_<wallet>.json)

    Key metrics to watch in logs (all have structured reason codes):
      hf_pair_entry      — new paired position opened
      partial_fill       — one leg filled, hedge triggered
      hedge_maker_fill   — hedge completed as maker (low cost)
      hedge_taker_fill   — hedge completed as taker (urgency fallback)
      hedge_slippage_abort — hedge aborted (price moved > MAX_SLIPPAGE_BPS)
      hedge_timeout      — hedge aborted (timeout)
      hedge_both_filled  — both legs filled successfully
      collect_timeout    — neither leg filled within timeout

.PARAMETER Wallet
    Target Polymarket wallet address (0x...).

.PARAMETER CombinedPriceMin
    Minimum (YES_ask + NO_ask) to enter a pair.  Default: 0.85
    Lower = more trades, more edge, harder to fill.

.PARAMETER CombinedPriceMax
    Maximum (YES_ask + NO_ask) to enter a pair.  Default: 0.97
    Upper cap limits gross edge; set lower to be more selective.

.PARAMETER HedgeTimeoutSecs
    Abort unfilled leg after N seconds.  Default: 30

.PARAMETER HedgeTakerFallbackSecs
    Switch from maker to taker hedge after N seconds.  Default: 10

.PARAMETER MaxSlippageBps
    Abort hedge if price moved more than N bps from entry.  Default: 50

.PARAMETER CycleIntervalSecs
    Scan/monitor interval in seconds.  Default: 5

.PARAMETER MaxPositions
    Max concurrent paired positions.  Default: 5

.PARAMETER SimulatePartial
    Randomly force partial fills (30% chance) to exercise the hedge path.
    Useful for testing hedge logic in paper mode.  Default: off

.PARAMETER Aggressiveness
    Size multiplier passed to PositionSizer.  Default: 1.0

.EXAMPLE
    # Basic paper run
    .\run_clone_hf_paper.ps1 -Wallet 0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd

    # Narrower band (higher edge, fewer trades)
    .\run_clone_hf_paper.ps1 -Wallet 0x... -CombinedPriceMin 0.88 -CombinedPriceMax 0.95

    # Simulate partial fills to exercise the hedge path
    .\run_clone_hf_paper.ps1 -Wallet 0x... -SimulatePartial

    # Faster hedging (tighter taker fallback)
    .\run_clone_hf_paper.ps1 -Wallet 0x... -HedgeTakerFallbackSecs 5 -HedgeTimeoutSecs 20
#>

param(
    [string]$Wallet                  = $env:CLONE_WALLET,
    [double]$CombinedPriceMin        = 0.85,
    [double]$CombinedPriceMax        = 0.97,
    [int]   $HedgeTimeoutSecs        = 30,
    [int]   $HedgeTakerFallbackSecs  = 10,
    [double]$MaxSlippageBps          = 50.0,
    [int]   $CycleIntervalSecs       = 5,
    [int]   $MaxPositions            = 5,
    [switch]$SimulatePartial,
    [double]$Aggressiveness          = 1.0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Validation -----------------------------------------------------------

if (-not $Wallet) {
    Write-Host "ERROR: Provide -Wallet 0x... or set CLONE_WALLET env var" -ForegroundColor Red
    exit 1
}
if ($Wallet -notmatch '^0x[0-9a-fA-F]{40}$') {
    Write-Host "ERROR: Wallet address must be 0x followed by 40 hex characters" -ForegroundColor Red
    exit 1
}

$walletLower = $Wallet.ToLower()
$profilePath = "logs/clone_profile_$walletLower.json"

if (-not (Test-Path $profilePath)) {
    Write-Host "ERROR: Profile not found: $profilePath" -ForegroundColor Red
    Write-Host "Run the following first:" -ForegroundColor Yellow
    Write-Host "  .\run_clone_extract.ps1 -Wallet $Wallet" -ForegroundColor Yellow
    Write-Host "  .\run_clone_profile.ps1 -Wallet $Wallet" -ForegroundColor Yellow
    exit 1
}

if ($HedgeTakerFallbackSecs -ge $HedgeTimeoutSecs) {
    Write-Host "WARNING: HedgeTakerFallbackSecs ($HedgeTakerFallbackSecs) >= HedgeTimeoutSecs ($HedgeTimeoutSecs)" -ForegroundColor Yellow
    Write-Host "         Taker fallback will fire at/after the abort deadline — adjusting timeout to $($HedgeTakerFallbackSecs + 5)s" -ForegroundColor Yellow
    $HedgeTimeoutSecs = $HedgeTakerFallbackSecs + 5
}

# --- Banner ---------------------------------------------------------------

$simLabel = if ($SimulatePartial) { "YES (hedge path exercised)" } else { "NO" }
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET WALLET CLONE — HF HEDGE MODE (PAPER)" -ForegroundColor Cyan
Write-Host "  Target wallet:        $Wallet" -ForegroundColor Cyan
Write-Host "  Combined price band:  [$CombinedPriceMin, $CombinedPriceMax]" -ForegroundColor Cyan
Write-Host "  Hedge timeout:        ${HedgeTimeoutSecs}s" -ForegroundColor Cyan
Write-Host "  Taker fallback:       ${HedgeTakerFallbackSecs}s" -ForegroundColor Cyan
Write-Host "  Max slippage:         ${MaxSlippageBps} bps" -ForegroundColor Cyan
Write-Host "  Cycle interval:       ${CycleIntervalSecs}s" -ForegroundColor Cyan
Write-Host "  Max positions:        $MaxPositions" -ForegroundColor Cyan
Write-Host "  Simulate partial:     $simLabel" -ForegroundColor Cyan
Write-Host "  Aggressiveness:       $Aggressiveness" -ForegroundColor Cyan
Write-Host "  Mode:                 DRY_RUN=1 (paper, no real orders)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Key metrics to monitor:" -ForegroundColor Gray
Write-Host "  grep 'CloneHF' logs to see reason codes" -ForegroundColor Gray
Write-Host "  hf_stats() shows hedge_success_rate, avg_time_to_hedge, avg_net_edge_proxy" -ForegroundColor Gray
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor Gray
Write-Host ""

# --- Activate venv if present ---------------------------------------------

if (Test-Path ".venv/Scripts/Activate.ps1") {
    . .venv/Scripts/Activate.ps1
} elseif (Test-Path "venv/Scripts/Activate.ps1") {
    . venv/Scripts/Activate.ps1
}

# --- Set environment and launch -------------------------------------------

$env:DRY_RUN                         = "1"
$env:CLONE_ENABLED                   = "1"
$env:CLONE_HF_MODE_ENABLED           = "1"
$env:CLONE_WALLET                    = $Wallet
$env:CLONE_PROFILE_PATH              = $profilePath
$env:CLONE_AGGRESSIVENESS            = "$Aggressiveness"
$env:CLONE_COMBINED_PRICE_MIN        = "$CombinedPriceMin"
$env:CLONE_COMBINED_PRICE_MAX        = "$CombinedPriceMax"
$env:CLONE_HEDGE_TIMEOUT_SECS        = "$HedgeTimeoutSecs"
$env:CLONE_HEDGE_TAKER_FALLBACK_SECS = "$HedgeTakerFallbackSecs"
$env:CLONE_MAX_SLIPPAGE_BPS          = "$MaxSlippageBps"
$env:CLONE_CYCLE_INTERVAL_SECS       = "$CycleIntervalSecs"
$env:CLONE_HF_MAX_POSITIONS          = "$MaxPositions"
$env:CLONE_HF_PAPER_SIMULATE_PARTIAL = if ($SimulatePartial) { "1" } else { "0" }

try {
    python bot.py
} finally {
    # Clean up env vars
    $hfVars = @(
        "CLONE_ENABLED", "CLONE_HF_MODE_ENABLED", "CLONE_WALLET", "CLONE_PROFILE_PATH",
        "CLONE_AGGRESSIVENESS", "CLONE_COMBINED_PRICE_MIN", "CLONE_COMBINED_PRICE_MAX",
        "CLONE_HEDGE_TIMEOUT_SECS", "CLONE_HEDGE_TAKER_FALLBACK_SECS",
        "CLONE_MAX_SLIPPAGE_BPS", "CLONE_CYCLE_INTERVAL_SECS",
        "CLONE_HF_MAX_POSITIONS", "CLONE_HF_PAPER_SIMULATE_PARTIAL"
    )
    foreach ($v in $hfVars) {
        Remove-Item "Env:\$v" -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "Session ended.  Review logs/orders.csv and run:" -ForegroundColor Gray
    Write-Host "  python -m analytics.performance_report" -ForegroundColor Gray
}
