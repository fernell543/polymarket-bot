param(
    [string]$Csv       = "logs/quant_trades.csv",
    [string]$Output    = "logs/opt_results.csv",
    [string]$BestJson  = "logs/opt_best.json",
    [string]$Mode      = "grid",
    [int]   $NSamples  = 500,
    [double]$Fee       = 0.02,
    [double]$Slip      = 0.002,
    [double]$DdPenalty = 0.5
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET BOT - PARAMETER OPTIMIZER" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  CSV:        $Csv"
Write-Host "  Mode:       $Mode"
Write-Host "  Fee:        $Fee   Slip: $Slip   DD-penalty: $DdPenalty"
Write-Host ""

$venvActivate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
}

if (-not (Test-Path $Csv)) {
    Write-Host "[ERROR] Trade log not found: $Csv" -ForegroundColor Red
    Write-Host "        Run .\run_paper_super.ps1 first to generate paper trades." -ForegroundColor Red
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
    Write-Host "  2. Run .\run_walkforward.ps1 to check for overfitting"
    Write-Host "  3. Apply best params to .\run_paper_super.ps1 env overrides"
} else {
    Write-Host "[WARN] Optimizer exited with code $exit_code" -ForegroundColor Yellow
}

exit $exit_code
