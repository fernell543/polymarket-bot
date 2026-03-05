<#
.SYNOPSIS
    Polymarket Wallet Clone — Step 1: Data Extraction

.DESCRIPTION
    Fetches publicly available trade history and positions for a target wallet
    from Polymarket's public data APIs.

    Output files:
      logs/clone_source_<wallet>.csv   — flat CSV for profiling/backtest
      logs/clone_source_<wallet>.json  — raw JSON archive

.PARAMETER Wallet
    Target Polymarket wallet address (0x...).
    Defaults to CLONE_WALLET env var if set.

.PARAMETER Limit
    Maximum trade records to fetch (default: 2000).

.PARAMETER NoEnrich
    Skip Gamma API market metadata enrichment (faster).

.EXAMPLE
    .\run_clone_extract.ps1 -Wallet 0x288cfa8daae64e2e1d3ab118a9261b24f70d23bd
    .\run_clone_extract.ps1 -Wallet 0x... -Limit 5000
    .\run_clone_extract.ps1 -Wallet 0x... -NoEnrich
#>

param(
    [string]$Wallet = $env:CLONE_WALLET,
    [int]$Limit = 2000,
    [switch]$NoEnrich
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Resolve wallet
if (-not $Wallet) {
    Write-Host "ERROR: Provide -Wallet 0x... or set CLONE_WALLET env var" -ForegroundColor Red
    exit 1
}

if (-not ($Wallet -match "^0x[a-fA-F0-9]{40}$")) {
    Write-Host "ERROR: Invalid wallet address format: $Wallet" -ForegroundColor Red
    exit 1
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET WALLET CLONE — DATA EXTRACTION" -ForegroundColor Cyan
Write-Host "  Target wallet: $Wallet" -ForegroundColor Cyan
Write-Host "  Trade limit:   $Limit" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# Build argument list
$args_list = @(
    "scripts/clone_extract.py",
    "--wallet", $Wallet,
    "--limit", $Limit
)

if ($NoEnrich) {
    $args_list += "--no-enrich"
}

# Run extraction
python @args_list
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Host "Extraction failed (exit $exitCode)" -ForegroundColor Red
    exit $exitCode
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Extraction complete." -ForegroundColor Green
Write-Host "  Next: .\run_clone_profile.ps1 -Wallet $Wallet" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
