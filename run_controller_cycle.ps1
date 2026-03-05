param(
    [switch]$Supervised,
    [switch]$Status,
    [switch]$Verbose
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (Test-Path ".venv/Scripts/Activate.ps1") {
    . .venv/Scripts/Activate.ps1
} elseif (Test-Path "venv/Scripts/Activate.ps1") {
    . venv/Scripts/Activate.ps1
}

# Set control mode
if ($Supervised) {
    $env:CONTROL_MODE = "supervised"
    $modeLabel = "SUPERVISED (auto-apply within allowlist)"
} else {
    $env:CONTROL_MODE = "manual"
    $modeLabel = "MANUAL (read-only — no auto-apply)"
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET BOT — SUPERVISED CONTROLLER" -ForegroundColor Cyan
Write-Host "  Mode : $modeLabel" -ForegroundColor $(if ($Supervised) { "Yellow" } else { "Green" })
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

if ($Supervised) {
    Write-Host "WARNING: supervised mode will write parameter updates to:" -ForegroundColor Yellow
    Write-Host "  logs/applied_params.json" -ForegroundColor Yellow
    Write-Host "Hard risk limits (RISK_DAILY_LOSS_LIMIT, RISK_MAX_DRAWDOWN, staircase" -ForegroundColor Yellow
    Write-Host "size caps, DRY_RUN, wallet keys) are NEVER modified." -ForegroundColor Yellow
    Write-Host ""
}

try {
    if ($Status) {
        python -m agent_loop.controller status
    } else {
        $verboseFlag = if ($Verbose) { "--verbose" } else { "" }
        if ($verboseFlag) {
            python -m agent_loop.controller run --verbose
        } else {
            python -m agent_loop.controller run
        }
    }
} finally {
    Remove-Item "Env:\CONTROL_MODE" -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "Audit log : logs/controller_audit.jsonl" -ForegroundColor Gray
    if (-not $Status) {
        Write-Host "Applied   : logs/applied_params.json (if any changes)" -ForegroundColor Gray
        Write-Host ""
        Write-Host "NOTE: Restart the bot to pick up any applied parameters." -ForegroundColor Gray
    }
}
