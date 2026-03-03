# run_walkforward.ps1
# =====================
# Step 2 of daily tuning workflow.
# Runs walk-forward validation on the paper trade log to measure
# strategy consistency, overfit risk, and regime stability.
#
# Run AFTER run_paper_optimize.ps1 to evaluate whether the best
# params generalise across time windows.
#
# Usage:
#   .\run_walkforward.ps1
#   .\run_walkforward.ps1 -Windows 6
#   .\run_walkforward.ps1 -Csv logs/quant_trades.csv -Verbose

param(
    [string]$Csv     = "logs/quant_trades.csv",
    [int]   $Windows = 5,
    [double]$Fee     = 0.02,
    [double]$Slip    = 0.002,
    [switch]$Verbose
)

Write-Host ""
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET BOT — WALK-FORWARD VALIDATION" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  CSV:     $Csv"
Write-Host "  Windows: $Windows"
Write-Host ""

if (-not (Test-Path $Csv)) {
    Write-Host "[ERROR] Trade log not found: $Csv" -ForegroundColor Red
    Write-Host "        Run run_paper_super.ps1 first to generate paper trades." -ForegroundColor Red
    exit 1
}

$args_list = @(
    "-m", "analytics.walk_forward",
    "--csv",     $Csv,
    "--windows", $Windows,
    "--fee",     $Fee,
    "--slip",    $Slip
)
if ($Verbose) { $args_list += "--verbose" }

python @args_list
$exit_code = $LASTEXITCODE

if ($exit_code -eq 0) {
    Write-Host ""
    Write-Host "Interpretation guide:" -ForegroundColor Green
    Write-Host "  Stability score >= 0.7  → consistent edge across time (good)"
    Write-Host "  Overfit ratio   < 2.0   → params generalise well (good)"
    Write-Host "  Val Sharpe      >= 0.3  → validation set edge is real (good)"
    Write-Host ""
    Write-Host "Go-live gate (ALL must pass before DRY_RUN=0):"
    Write-Host "  [ ] Stability >= 0.7"
    Write-Host "  [ ] Overfit ratio < 2.0"
    Write-Host "  [ ] Val Sharpe >= 0.3"
    Write-Host "  [ ] Win rate >= 52% in val window"
} else {
    Write-Host "[WARN] Walk-forward exited with code $exit_code" -ForegroundColor Yellow
}
