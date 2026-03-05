<#
.SYNOPSIS
    Polymarket Wallet Clone — Step 2: Behavior Profiling

.DESCRIPTION
    Reads the extracted CSV from run_clone_extract.ps1 and computes:
      - Trade frequency, size distribution, entry price ranges
      - YES/NO directional bias, market category preferences
      - Timing analysis, win/loss proxy

    Deep analysis (new):
      - Temporal patterns (time-of-day, day-of-week)
      - Market duration buckets (intraday/short/medium/long/very-long)
      - Size-scaling analysis (price vs size correlation)
      - Streak analysis (win/loss sequences, adaptation signal)
      - Holding-period distribution (median/p90 proxy)
      - Archetype clustering (6 rules-based archetypes + allocation weights)
      - Confidence-scored parameter bands (high/medium/speculative)
      - Experiment matrix (5 ranked param sets for paper trading)

    Also runs the backtest evaluator to compare clone vs target metrics.

    Output files:
      logs/clone_profile_<wallet>.json          — standard inferred parameter profile
      logs/clone_profile_<wallet>_detailed.json — full deep analysis (new)
      logs/clone_profile_<wallet>_report.md     — human-readable markdown report (new)
      logs/clone_backtest_<wallet>.json         — per-trade replay comparison

.PARAMETER Wallet
    Target Polymarket wallet address (0x...).

.PARAMETER Threshold
    Clone score threshold for backtest (0-1, default 0.40).

.PARAMETER Aggressiveness
    Size multiplier for backtest simulation (default 1.0).

.EXAMPLE
    .\run_clone_profile.ps1 -Wallet 0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd
    .\run_clone_profile.ps1 -Wallet 0x... -Threshold 0.35 -Aggressiveness 0.8
#>

param(
    [string]$Wallet = $env:CLONE_WALLET,
    [double]$Threshold = 0.40,
    [double]$Aggressiveness = 1.0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $Wallet) {
    Write-Host "ERROR: Provide -Wallet 0x... or set CLONE_WALLET env var" -ForegroundColor Red
    exit 1
}

$walletLower = $Wallet.ToLower()
$csvPath = "logs/clone_source_$walletLower.csv"

if (-not (Test-Path $csvPath)) {
    Write-Host "ERROR: CSV not found: $csvPath" -ForegroundColor Red
    Write-Host "Run .\run_clone_extract.ps1 -Wallet $Wallet first." -ForegroundColor Yellow
    exit 1
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET WALLET CLONE — BEHAVIOR PROFILING" -ForegroundColor Cyan
Write-Host "  Wallet:    $Wallet" -ForegroundColor Cyan
Write-Host "  Threshold: $Threshold" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# Step 1: Build profile
Write-Host "`n[1/2] Building behavioral profile..." -ForegroundColor Yellow
python -m analytics.clone_profile --wallet $Wallet
if ($LASTEXITCODE -ne 0) {
    Write-Host "Profile build failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# Step 2: Run backtest
Write-Host "`n[2/2] Running replay backtest..." -ForegroundColor Yellow
python -m analytics.clone_backtest `
    --wallet $Wallet `
    --threshold $Threshold `
    --aggressiveness $Aggressiveness
if ($LASTEXITCODE -ne 0) {
    Write-Host "Backtest failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Profile and backtest complete." -ForegroundColor Green
Write-Host "  Standard profile:  logs/clone_profile_$walletLower.json" -ForegroundColor Green
Write-Host "  Detailed profile:  logs/clone_profile_${walletLower}_detailed.json" -ForegroundColor Green
Write-Host "  Markdown report:   logs/clone_profile_${walletLower}_report.md" -ForegroundColor Green
Write-Host "  Backtest:          logs/clone_backtest_$walletLower.json" -ForegroundColor Green
Write-Host ""
Write-Host "  Next: .\run_clone_paper.ps1 -Wallet $Wallet" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
