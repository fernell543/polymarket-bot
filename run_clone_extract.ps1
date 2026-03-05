param(
    [string]$Wallet = $env:CLONE_WALLET,
    [int]$Limit = 2000,
    [switch]$NoEnrich
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Wallet) {
    Write-Host "ERROR: Provide -Wallet 0x... or set CLONE_WALLET env var" -ForegroundColor Red
    exit 1
}

if (-not ($Wallet -match "^0x[a-fA-F0-9]{40}$")) {
    Write-Host "ERROR: Invalid wallet address format: $Wallet" -ForegroundColor Red
    exit 1
}

$venvActivate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET WALLET CLONE - DATA EXTRACTION" -ForegroundColor Cyan
Write-Host "  Target wallet: $Wallet" -ForegroundColor Cyan
Write-Host "  Trade limit:   $Limit" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$args_list = @(
    "scripts/clone_extract.py",
    "--wallet", $Wallet,
    "--limit", $Limit
)

if ($NoEnrich) {
    $args_list += "--no-enrich"
}

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
