param(
    [string]$DryRun          = "1",
    [string]$Profile         = "auto",
    [double]$ConfThreshold   = 0.0,
    [double]$MinEdge         = 0.0,
    [double]$DailyLossLimit  = 50.0,
    [double]$MaxDrawdown     = 100.0,
    [int]   $MaxConsecLosses = 3,
    [int]   $CooldownSecs    = 300,
    [double]$KsTier1Api      = 0.25,
    [double]$KsTier2Api      = 0.50,
    [double]$KsTier3Api      = 0.75,
    [double]$KsTier1Stale    = 60,
    [double]$KsTier2Stale    = 120,
    [double]$KsTier3Stale    = 600
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET BOT - PAPER SUPER MODE" -ForegroundColor Cyan
Write-Host "  DRY_RUN=1 | QUANT_MODE_ENABLED=1" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  Profile:         $Profile"
Write-Host "  Daily loss cap:  $DailyLossLimit USDC (paper)"
Write-Host "  Max drawdown:    $MaxDrawdown USDC (paper)"
Write-Host ""

$venvActivate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
    Write-Host "  [INFO] Virtual environment activated" -ForegroundColor Gray
}

$env:DRY_RUN = $DryRun
$env:QUANT_MODE_ENABLED = "1"
$env:PARAM_PROFILE = $Profile
$env:RISK_DAILY_LOSS_LIMIT = "$DailyLossLimit"
$env:RISK_MAX_DRAWDOWN = "$MaxDrawdown"
$env:RISK_MAX_CONSECUTIVE_LOSSES = "$MaxConsecLosses"
$env:RISK_COOLDOWN_SECS = "$CooldownSecs"

$env:KS_TIER1_API_ERROR_RATE = "$KsTier1Api"
$env:KS_TIER2_API_ERROR_RATE = "$KsTier2Api"
$env:KS_TIER3_API_ERROR_RATE = "$KsTier3Api"
$env:KS_TIER1_STALE_SECS = "$KsTier1Stale"
$env:KS_TIER2_STALE_SECS = "$KsTier2Stale"
$env:KS_TIER3_STALE_SECS = "$KsTier3Stale"

if ($ConfThreshold -gt 0) { $env:SIGNAL_CONFIDENCE_THRESHOLD = "$ConfThreshold" }
if ($MinEdge -gt 0) { $env:EXEC_MIN_EDGE = "$MinEdge" }

Write-Host "Starting bot... (Ctrl+C to stop)" -ForegroundColor Green
Write-Host ""

python bot.py

Write-Host ""
Write-Host "Bot stopped." -ForegroundColor Yellow
Write-Host ""
Write-Host "Post-session analysis:" -ForegroundColor Cyan
Write-Host "  python -m analytics.performance_report"
Write-Host "  python -m analytics.fill_quality"
Write-Host "  .\run_paper_optimize.ps1"
Write-Host "  .\run_walkforward.ps1"
