# run_paper_optimize.ps1
# ========================
# Step 1 of daily tuning workflow.
# Runs parameter grid search over today's paper trade log and outputs
# ranked configs to logs/opt_results.csv and logs/opt_best.json.
#
# Prerequisites: have run run_paper_super.ps1 for at least a few hours
# to generate enough trades (>=50 recommended, >=200 for reliable results).
#
# Usage:
#   .\run_paper_optimize.ps1
#   .\run_paper_optimize.ps1 -Mode random -NSamples 1000
#   .\run_paper_optimize.ps1 -Csv logs/quant_trades.csv -DdPenalty 1.0

param(
    [string]$Csv       = "logs/quant_trades.csv",
    [string]$Output    = "logs/opt_results.csv",
    [string]$BestJson  = "logs/opt_best.json",
    [string]$Mode      = "grid",          # "grid" or "random"
    [int]   $NSamples  = 500,             # used only in random mode
    [double]$Fee       = 0.02,
    [double]$Slip      = 0.002,
    [double]$DdPenalty = 0.5
)

Write-Host ""
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET BOT — PARAMETER OPTIMIZER" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  CSV:        $Csv"
Write-Host "  Mode:       $Mode"
Write-Host "  Fee:        $Fee   Slip: $Slip   DD-penalty: $DdPenalty"
Write-Host ""

if (-not (Test-Path $Csv)) {
    Write-Host "[ERROR] Trade log not found: $Csv" -ForegroundColor Red
    Write-Host "        Run run_paper_super.ps1 first to generate paper trades." -ForegroundColor Red
    exit 1
}

$args_list = @(
    "-m", "analytics.param_optimizer",
    "--csv",        $Csv,
    "--output",     $Output,
    "--best-json",  $BestJson,
    "--mode",       $Mode,
    "--n-samples",  $NSamples,
    "--fee",        $Fee,
    "--slip",       $Slip,
    "--dd-penalty", $DdPenalty
)

python @args_list
$exit_code = $LASTEXITCODE

if ($exit_code -eq 0) {
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Green
    Write-Host "  1. Review logs/opt_best.json for best param config"
    Write-Host "  2. Run run_walkforward.ps1 to check for overfitting"
    Write-Host "  3. Apply best params to run_paper_super.ps1 env overrides"
} else {
    Write-Host "[WARN] Optimizer exited with code $exit_code" -ForegroundColor Yellow
}
