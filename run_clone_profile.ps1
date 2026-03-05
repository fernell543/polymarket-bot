param(
    [string]$Wallet = $env:CLONE_WALLET,
    [double]$Threshold = 0.40,
    [double]$Aggressiveness = 1.0
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

$walletLower = $Wallet.ToLower()
$csvPath = "logs/clone_source_$walletLower.csv"

if (-not (Test-Path $csvPath)) {
    Write-Host "ERROR: Missing source CSV: $csvPath" -ForegroundColor Red
    Write-Host "Run .\run_clone_extract.ps1 -Wallet $Wallet first." -ForegroundColor Yellow
    exit 1
}

$venvActivate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET WALLET CLONE - BEHAVIOR PROFILING" -ForegroundColor Cyan
Write-Host "  Wallet:         $Wallet" -ForegroundColor Cyan
Write-Host "  Score threshold: $Threshold" -ForegroundColor Cyan
Write-Host "  Aggressiveness:  $Aggressiveness" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

python -m analytics.clone_profile --wallet $Wallet
$exitA = $LASTEXITCODE
if ($exitA -ne 0) {
    Write-Host "clone_profile failed (exit $exitA)" -ForegroundColor Red
    exit $exitA
}

python -m analytics.clone_backtest --wallet $Wallet --threshold $Threshold --aggressiveness $Aggressiveness
$exitB = $LASTEXITCODE
if ($exitB -ne 0) {
    Write-Host "clone_backtest failed (exit $exitB)" -ForegroundColor Red
    exit $exitB
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Profiling complete." -ForegroundColor Green
Write-Host "  Standard profile:  logs/clone_profile_$walletLower.json" -ForegroundColor Green
Write-Host "  Detailed profile:  logs/clone_profile_${walletLower}_detailed.json" -ForegroundColor Green
Write-Host "  Markdown report:   logs/clone_profile_${walletLower}_report.md" -ForegroundColor Green
Write-Host "  Backtest:          logs/clone_backtest_$walletLower.json" -ForegroundColor Green
Write-Host "  Next: .\run_clone_paper.ps1 -Wallet $Wallet" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
