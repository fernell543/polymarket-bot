param(
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Polymarket Bot - Supervised Improvement Cycle" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Safety: no auto-deploy to live. Operator review required." -ForegroundColor Yellow
Write-Host ""

$venvActivate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
    Write-Host "  [INFO] Virtual environment activated" -ForegroundColor Gray
}

$env:PYTHONPATH = $ScriptDir

$logsDir = Join-Path $ScriptDir "logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

if ($Verbose) {
    python -m agent_loop.orchestrator cycle --verbose
} else {
    python -m agent_loop.orchestrator cycle
}

$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "  [OK] Cycle complete." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next steps:" -ForegroundColor Cyan
    Write-Host "    1. Review  : logs\deployment_plan.json" -ForegroundColor White
    Write-Host "    2. Report  : .\run_supervised_report.ps1" -ForegroundColor White
    Write-Host "    3. Promote : follow instructions in deployment_plan.json (manual)" -ForegroundColor White
} else {
    Write-Host "  [ERROR] Cycle failed (exit $exitCode)" -ForegroundColor Red
    Write-Host "  Check logs\bot.log or run with -Verbose for details." -ForegroundColor Yellow
}

exit $exitCode
