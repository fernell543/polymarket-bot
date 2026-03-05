# run_live_staircase.ps1
# ======================
# Micro-live staircase deployment launcher for Windows.
#
# Usage:
#   .\run_live_staircase.ps1              # Stage A, paper (DRY_RUN=1)
#   .\run_live_staircase.ps1 -Stage A -Live  # Stage A, LIVE (DRY_RUN=0)
#   .\run_live_staircase.ps1 -Stage B -Live  # Stage B, LIVE
#   .\run_live_staircase.ps1 -CheckGate      # Check gate for current stage
#   .\run_live_staircase.ps1 -Status         # Show staircase state
#   .\run_live_staircase.ps1 -ProxyReport    # Run live-proxy validator on logs
#
# Hard risk guardrails injected by this script (non-overridable):
#   RISK_DAILY_LOSS_LIMIT, RISK_MAX_DRAWDOWN, kill-switch Tier 3

param(
    [ValidateSet("A","B","C","status","proxy")][string]$Stage = "A",
    [switch]$Live,           # if omitted → DRY_RUN=1 (paper)
    [switch]$CheckGate,      # evaluate promotion gate and exit
    [switch]$Status,         # show staircase state and exit
    [switch]$ProxyReport,    # run live_proxy_validator and exit
    [string]$OutputReport = "logs/live_proxy_report.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ---------------------------------------------------------------------------
# Status / gate / proxy sub-commands
# ---------------------------------------------------------------------------
if ($Status) {
    Write-Host "=== Staircase Status ===" -ForegroundColor Cyan
    python scripts/live_staircase.py status
    exit 0
}

if ($CheckGate) {
    Write-Host "=== Gate Check ===" -ForegroundColor Cyan
    python scripts/live_staircase.py check-gate
    exit 0
}

if ($ProxyReport) {
    Write-Host "=== Live-Proxy Validation ===" -ForegroundColor Cyan
    python -m analytics.live_proxy_validator --output $OutputReport
    exit 0
}

# ---------------------------------------------------------------------------
# Stage config
# ---------------------------------------------------------------------------
$StageConfigs = @{
    "A" = @{
        EnvMode      = "staircase_A"
        MaxSize      = "5.0"
        MaxPositions = "3"
        Description  = "Stage A — tiny size (\$5 max, 3 positions)"
        Color        = "Yellow"
    }
    "B" = @{
        EnvMode      = "staircase_B"
        MaxSize      = "20.0"
        MaxPositions = "5"
        Description  = "Stage B — small size (\$20 max, 5 positions)"
        Color        = "Cyan"
    }
    "C" = @{
        EnvMode      = "staircase_C"
        MaxSize      = "50.0"
        MaxPositions = "10"
        Description  = "Stage C — scale candidate (\$50 max, 10 positions)"
        Color        = "Green"
    }
}

$Cfg = $StageConfigs[$Stage]
if (-not $Cfg) {
    Write-Error "Unknown stage: $Stage"
    exit 1
}

# ---------------------------------------------------------------------------
# Safety confirmation for live mode
# ---------------------------------------------------------------------------
$DryRun = if ($Live) { "0" } else { "1" }

if ($Live) {
    Write-Host ""
    Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
    Write-Host "  LIVE TRADING MODE — REAL FUNDS AT RISK" -ForegroundColor Red
    Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Stage:     $Stage  ($($Cfg.Description))" -ForegroundColor $Cfg.Color
    Write-Host "Max size:  `$$($Cfg.MaxSize) USDC per trade" -ForegroundColor $Cfg.Color
    Write-Host "Max pos:   $($Cfg.MaxPositions) positions" -ForegroundColor $Cfg.Color
    Write-Host ""
    Write-Host "Hard guardrails (enforced by bot, cannot be removed):" -ForegroundColor White
    Write-Host "  RISK_DAILY_LOSS_LIMIT = \$100" -ForegroundColor White
    Write-Host "  RISK_MAX_DRAWDOWN     = \$200" -ForegroundColor White
    Write-Host "  Kill-switch Tier 3    = NO-TRADE (automatic)" -ForegroundColor White
    Write-Host ""
    $Confirm = Read-Host "Type 'LIVE' to confirm and launch Stage $Stage in LIVE mode"
    if ($Confirm -ne "LIVE") {
        Write-Host "Aborted." -ForegroundColor Yellow
        exit 0
    }
}

# ---------------------------------------------------------------------------
# Environment setup (stage-specific + guardrails)
# ---------------------------------------------------------------------------
$env:LIVE_DEPLOY_MODE           = $Cfg.EnvMode
$env:SIZE_MAX_USDC              = $Cfg.MaxSize
$env:CLONE_MAX_OPEN_POSITIONS   = $Cfg.MaxPositions
$env:LIVE_CONTROLS_ENABLED      = "1"
$env:DRY_RUN                    = $DryRun

# HARD GUARDRAILS — always inject, non-overridable
$env:RISK_DAILY_LOSS_LIMIT      = "100"
$env:RISK_MAX_DRAWDOWN          = "200"

# Enable clone strategy (primary live strategy)
$env:CLONE_ENABLED              = "1"
$env:QUANT_MODE_ENABLED         = "1"

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("=" * 60) -ForegroundColor $Cfg.Color
Write-Host "  LIVE STAIRCASE — $($Cfg.Description)" -ForegroundColor $Cfg.Color
Write-Host ("=" * 60) -ForegroundColor $Cfg.Color
Write-Host "  Mode:        $(if ($Live) { 'LIVE (DRY_RUN=0)' } else { 'PAPER (DRY_RUN=1)' })"
Write-Host "  Max size:    `$$($Cfg.MaxSize) USDC"
Write-Host "  Max pos:     $($Cfg.MaxPositions)"
Write-Host "  Daily limit: `$100 (hard guardrail)"
Write-Host "  Max DD:      `$200 (hard guardrail)"
Write-Host ("=" * 60) -ForegroundColor $Cfg.Color
Write-Host ""

# ---------------------------------------------------------------------------
# Run bot
# ---------------------------------------------------------------------------
$StartTime = Get-Date
Write-Host "Starting bot at $StartTime" -ForegroundColor White

try {
    python bot.py
    $ExitCode = $LASTEXITCODE
} catch {
    Write-Host "Bot crashed: $_" -ForegroundColor Red
    $ExitCode = 1
}

$EndTime = Get-Date
$Elapsed = $EndTime - $StartTime

Write-Host ""
Write-Host ("=" * 60) -ForegroundColor $Cfg.Color
Write-Host "  Stage $Stage session ended"
Write-Host "  Elapsed: $($Elapsed.TotalHours.ToString('F1'))h"
Write-Host "  Exit code: $ExitCode"
Write-Host ("=" * 60) -ForegroundColor $Cfg.Color

# ---------------------------------------------------------------------------
# Post-session: gate check + proxy report
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Running post-session checks..." -ForegroundColor Cyan

# Live-proxy validation
Write-Host "  Live-proxy validator..." -ForegroundColor White
python -m analytics.live_proxy_validator --output $OutputReport 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Proxy report: $OutputReport" -ForegroundColor Green
}

# Gate check
Write-Host "  Gate check for Stage $Stage..." -ForegroundColor White
python scripts/live_staircase.py check-gate --stage $Stage

Write-Host ""
Write-Host "To check gate manually:  python scripts/live_staircase.py check-gate" -ForegroundColor DarkGray
Write-Host "To view proxy report:    python -m analytics.live_proxy_validator" -ForegroundColor DarkGray
Write-Host "To launch next stage:    .\run_live_staircase.ps1 -Stage B $(if ($Live) { '-Live' })" -ForegroundColor DarkGray
Write-Host ""

exit $ExitCode
