# run_overnight.ps1
# =================
# Overnight paper-trading session with timestamped log capture.
#
# Features:
#   - DRY_RUN=1 enforced — no real orders placed
#   - QUANT_MODE_ENABLED=1 — all risk/signal systems active
#   - Tee output to logs\overnight_<timestamp>.log for post-session review
#   - Runs preflight checks before starting the bot
#   - Prints post-session analysis commands when the bot stops
#
# Usage:
#   .\run_overnight.ps1
#   .\run_overnight.ps1 -Profile conservative
#   .\run_overnight.ps1 -DailyLossLimit 30 -MaxDrawdown 60
#
# To stop gracefully:
#   Press Ctrl+C once — Python catches SIGINT and flushes logs cleanly.
#   If the window is closed directly the log file will still be intact
#   (tee flushes on every line).
#
# After the session:
#   python -m analytics.performance_report
#   python -m analytics.fill_quality
#   .\run_paper_optimize.ps1
#   .\run_walkforward.ps1

param(
    # Profile: "auto" | "conservative" | "balanced" | "aggressive"
    [string]$Profile             = "auto",

    # Risk limits (conservative overnight defaults — tighter than run_paper_super)
    [double]$DailyLossLimit      = 30.0,
    [double]$MaxDrawdown         = 60.0,
    [int]   $MaxConsecLosses     = 3,
    [int]   $CooldownSecs        = 300,

    # Kill-switch thresholds (paper — relaxed vs live)
    [double]$KsTier1Api          = 0.25,
    [double]$KsTier2Api          = 0.50,
    [double]$KsTier3Api          = 0.75,
    [double]$KsTier1Stale        = 60,
    [double]$KsTier2Stale        = 120,
    [double]$KsTier3Stale        = 600,

    # Skip preflight check (not recommended)
    [switch]$SkipPreflight
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

$timestamp  = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir     = Join-Path $PSScriptRoot "logs"
$logFile    = Join-Path $logDir "overnight_$timestamp.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET BOT — OVERNIGHT PAPER SESSION" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Mode:            DRY_RUN=1  (NO real orders)" -ForegroundColor Green
Write-Host "  Quant:           QUANT_MODE_ENABLED=1"
Write-Host "  Profile:         $Profile"
Write-Host "  Daily loss cap:  $DailyLossLimit USDC (paper)"
Write-Host "  Max drawdown:    $MaxDrawdown USDC (paper)"
Write-Host "  Log file:        $logFile"
Write-Host ""
Write-Host "  To stop: press Ctrl+C once — Python flushes logs cleanly." -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

$env:DRY_RUN                     = "1"
$env:QUANT_MODE_ENABLED          = "1"
$env:PARAM_PROFILE               = $Profile
$env:RISK_DAILY_LOSS_LIMIT       = "$DailyLossLimit"
$env:RISK_MAX_DRAWDOWN           = "$MaxDrawdown"
$env:RISK_MAX_CONSECUTIVE_LOSSES = "$MaxConsecLosses"
$env:RISK_COOLDOWN_SECS          = "$CooldownSecs"

$env:KS_TIER1_API_ERROR_RATE     = "$KsTier1Api"
$env:KS_TIER2_API_ERROR_RATE     = "$KsTier2Api"
$env:KS_TIER3_API_ERROR_RATE     = "$KsTier3Api"
$env:KS_TIER1_STALE_SECS         = "$KsTier1Stale"
$env:KS_TIER2_STALE_SECS         = "$KsTier2Stale"
$env:KS_TIER3_STALE_SECS         = "$KsTier3Stale"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

if (-not $SkipPreflight) {
    Write-Host "Running preflight checks..." -ForegroundColor Cyan
    python preflight.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Preflight FAILED. Fix issues above before running overnight." -ForegroundColor Red
        Write-Host "To skip preflight (not recommended): .\run_overnight.ps1 -SkipPreflight" -ForegroundColor Yellow
        exit 1
    }
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Run bot — tee stdout+stderr to log file
# ---------------------------------------------------------------------------

Write-Host "Starting bot... output tee'd to $logFile" -ForegroundColor Green
Write-Host ""

$startTime = Get-Date
python bot.py 2>&1 | Tee-Object -FilePath $logFile
$exitCode  = $LASTEXITCODE
$elapsed   = (Get-Date) - $startTime

# ---------------------------------------------------------------------------
# Post-session summary
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Session ended at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "  Duration:  $([math]::Floor($elapsed.TotalHours))h $($elapsed.Minutes)m $($elapsed.Seconds)s"
Write-Host "  Exit code: $exitCode"
Write-Host "  Log:       $logFile"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Post-session analysis:" -ForegroundColor Cyan
Write-Host "  python -m analytics.performance_report"
Write-Host "  python -m analytics.fill_quality"
Write-Host "  .\run_paper_optimize.ps1"
Write-Host "  .\run_walkforward.ps1"
Write-Host ""
