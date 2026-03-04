<#
.SYNOPSIS
    Show the latest supervised cycle scorecard and recommendation.

.DESCRIPTION
    Reads logs/supervised_decisions.jsonl and logs/supervised_state.json,
    then prints a formatted summary of the most recent cycle's metrics,
    violations, recommendation, and deployment plan instructions.

.EXAMPLE
    .\run_supervised_report.ps1
#>
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Polymarket Bot — Supervised Improvement Report" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Activate venv if present
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
