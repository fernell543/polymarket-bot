param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Polymarket Bot - Supervised Improvement Report" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$venvActivate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
}

$env:PYTHONPATH = $ScriptDir

python -m agent_loop.orchestrator report
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -ne 0) {
    Write-Host "  [INFO] No report available yet. Run a cycle first:" -ForegroundColor Yellow
    Write-Host "    .\run_supervised_cycle.ps1" -ForegroundColor White
}

exit $exitCode
