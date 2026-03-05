<#
.SYNOPSIS
    Polymarket Wallet Clone — Step 3: Paper-Mode Clone Execution

.DESCRIPTION
    Runs the bot with the clone strategy ACTIVE in paper (DRY_RUN=1) mode.
    The bot will scan live markets against the inferred profile and log
    simulated orders to logs/orders.csv — no real money is used.

    Prerequisites:
      1. .\run_clone_extract.ps1 -Wallet <wallet>
      2. .\run_clone_profile.ps1 -Wallet <wallet>
      (profile JSON must exist at logs/clone_profile_<wallet>.json)

.PARAMETER Wallet
    Target Polymarket wallet address (0x...).

.PARAMETER Aggressiveness
    Size multiplier: 0.5 = conservative, 1.0 = match profile, 2.0 = aggressive.
    Default: 1.0

.PARAMETER Threshold
    Minimum clone score to execute a trade (0-1).
    Higher = fewer, higher-confidence trades.  Default: 0.40

.PARAMETER MaxPositions
    Max concurrent open clone positions.  Default: 3

.PARAMETER PollInterval
    Seconds between market scans.  Default: 60

.PARAMETER Quant
    Also enable QUANT_MODE_ENABLED (signal/risk engine).  Default: off

.EXAMPLE
    .\run_clone_paper.ps1 -Wallet 0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd
    .\run_clone_paper.ps1 -Wallet 0x... -Aggressiveness 0.5 -Threshold 0.50
    .\run_clone_paper.ps1 -Wallet 0x... -Quant
#>

param(
    [string]$Wallet = $env:CLONE_WALLET,
    [double]$Aggressiveness = 1.0,
    [double]$Threshold = 0.40,
    [int]$MaxPositions = 3,
    [int]$PollInterval = 60,
    [switch]$Quant
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $Wallet) {
    Write-Host "ERROR: Provide -Wallet 0x... or set CLONE_WALLET env var" -ForegroundColor Red
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

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET WALLET CLONE — PAPER EXECUTION" -ForegroundColor Cyan
Write-Host "  Target wallet:  $Wallet" -ForegroundColor Cyan
Write-Host "  Aggressiveness: $Aggressiveness" -ForegroundColor Cyan
Write-Host "  Score threshold: $Threshold" -ForegroundColor Cyan
Write-Host "  Max positions:  $MaxPositions" -ForegroundColor Cyan
Write-Host "  Poll interval:  ${PollInterval}s" -ForegroundColor Cyan
Write-Host "  Mode:           DRY_RUN=1 (paper, no real orders)" -ForegroundColor Cyan
if ($Quant) {
    Write-Host "  Quant mode:     ENABLED" -ForegroundColor Yellow
}
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor Gray
Write-Host ""

# Set environment and launch
$env:DRY_RUN                  = "1"
$env:CLONE_ENABLED            = "1"
$env:CLONE_WALLET             = $Wallet
$env:CLONE_PROFILE_PATH       = $profilePath
$env:CLONE_AGGRESSIVENESS     = "$Aggressiveness"
$env:CLONE_SCORE_THRESHOLD    = "$Threshold"
$env:CLONE_MAX_OPEN_POSITIONS = "$MaxPositions"
$env:CLONE_POLL_INTERVAL      = "$PollInterval"
$env:CLONE_BIAS_ENABLED       = "1"
$env:QUANT_MODE_ENABLED       = if ($Quant) { "1" } else { "0" }

try {
    python bot.py
} finally {
    # Clean up env vars after exit
    Remove-Item Env:\CLONE_ENABLED           -ErrorAction SilentlyContinue
    Remove-Item Env:\CLONE_WALLET            -ErrorAction SilentlyContinue
    Remove-Item Env:\CLONE_PROFILE_PATH      -ErrorAction SilentlyContinue
    Remove-Item Env:\CLONE_AGGRESSIVENESS    -ErrorAction SilentlyContinue
    Remove-Item Env:\CLONE_SCORE_THRESHOLD   -ErrorAction SilentlyContinue
    Remove-Item Env:\CLONE_MAX_OPEN_POSITIONS -ErrorAction SilentlyContinue
    Remove-Item Env:\CLONE_POLL_INTERVAL     -ErrorAction SilentlyContinue
    Remove-Item Env:\CLONE_BIAS_ENABLED      -ErrorAction SilentlyContinue
    Remove-Item Env:\QUANT_MODE_ENABLED      -ErrorAction SilentlyContinue
}
