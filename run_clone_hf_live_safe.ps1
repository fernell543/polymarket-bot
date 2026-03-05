param(
    [string]$Wallet                  = $env:CLONE_WALLET,
    [double]$CombinedPriceMin        = 0.88,
    [double]$CombinedPriceMax        = 0.96,
    [int]   $HedgeTimeoutSecs        = 30,
    [int]   $HedgeTakerFallbackSecs  = 10,
    [double]$MaxSlippageBps          = 30.0,
    [int]   $CycleIntervalSecs       = 5,
    [int]   $MaxPositions            = 3,
    [string]$SizeMode                = "profile",
    [double]$SizeMinUsdc             = 2.0,
    [double]$SizeMaxUsdc             = 20.0,
    [double]$SizeLiquidityMult       = 0.8,
    [string]$Stage                   = "staircase_A",
    [switch]$Live
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

if (-not $Wallet) {
    Write-Host "ERROR: Provide -Wallet 0x... or set CLONE_WALLET env var" -ForegroundColor Red
    exit 1
}
if ($Wallet -notmatch '^0x[0-9a-fA-F]{40}$') {
    Write-Host "ERROR: Wallet address must be 0x followed by 40 hex characters" -ForegroundColor Red
    exit 1
}

$walletLower = $Wallet.ToLower()
$profilePath = "logs/clone_profile_$walletLower.json"

if (-not (Test-Path $profilePath)) {
    Write-Host "ERROR: Profile not found: $profilePath" -ForegroundColor Red
    Write-Host "Run first:" -ForegroundColor Yellow
    Write-Host "  .\run_clone_extract.ps1 -Wallet $Wallet" -ForegroundColor Yellow
    Write-Host "  .\run_clone_profile.ps1 -Wallet $Wallet" -ForegroundColor Yellow
    exit 1
}

if ($HedgeTakerFallbackSecs -ge $HedgeTimeoutSecs) {
    Write-Host "WARNING: HedgeTakerFallbackSecs >= HedgeTimeoutSecs — adjusting timeout" -ForegroundColor Yellow
    $HedgeTimeoutSecs = $HedgeTakerFallbackSecs + 5
}

$validSizeModes = @("profile", "adaptive")
if ($SizeMode -notin $validSizeModes) {
    Write-Host "ERROR: -SizeMode must be 'profile' or 'adaptive'" -ForegroundColor Red
    exit 1
}

$validStages = @("staircase_A", "staircase_B", "staircase_C", "production")
if ($Stage -notin $validStages) {
    Write-Host "ERROR: -Stage must be one of: $($validStages -join ', ')" -ForegroundColor Red
    exit 1
}

# Hard cap enforcement — staircase_A hard cap is $5, conservatively applied
$stageHardCaps = @{
    "staircase_A" = 5.0
    "staircase_B" = 20.0
    "staircase_C" = 50.0
    "production"  = 500.0
}
$stageHardCap = $stageHardCaps[$Stage]
if ($SizeMaxUsdc -gt $stageHardCap) {
    Write-Host "WARNING: -SizeMaxUsdc ($SizeMaxUsdc) exceeds stage cap ($stageHardCap) for $Stage — clamping" -ForegroundColor Yellow
    $SizeMaxUsdc = $stageHardCap
}

# Live mode requires explicit confirmation
$dryRun = "1"
$modeLabel = "DRY_RUN=1 (paper)"
if ($Live) {
    if (-not $env:PRIVATE_KEY) {
        Write-Host "ERROR: PRIVATE_KEY not set. Cannot run live without credentials." -ForegroundColor Red
        exit 1
    }
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host "  WARNING: LIVE TRADING MODE  (DRY_RUN=0)" -ForegroundColor Red
    Write-Host "  Stage    : $Stage" -ForegroundColor Red
    Write-Host "  Max size : $SizeMaxUsdc USDC per trade" -ForegroundColor Red
    Write-Host "  Max pos  : $MaxPositions" -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host ""
    $confirm = Read-Host "Type 'LIVE' to proceed, anything else to abort"
    if ($confirm -ne "LIVE") {
        Write-Host "Aborted." -ForegroundColor Gray
        exit 0
    }
    $dryRun = "0"
    $modeLabel = "LIVE (DRY_RUN=0)"
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET CLONE HF - LIVE-SAFE LAUNCHER" -ForegroundColor Cyan
Write-Host "  Target wallet:        $Wallet" -ForegroundColor Cyan
Write-Host "  Stage:                $Stage" -ForegroundColor Cyan
Write-Host "  Combined price band:  [$CombinedPriceMin, $CombinedPriceMax]" -ForegroundColor Cyan
Write-Host "  Hedge timeout:        ${HedgeTimeoutSecs}s" -ForegroundColor Cyan
Write-Host "  Taker fallback:       ${HedgeTakerFallbackSecs}s" -ForegroundColor Cyan
Write-Host "  Max slippage:         ${MaxSlippageBps} bps" -ForegroundColor Cyan
Write-Host "  Max positions:        $MaxPositions" -ForegroundColor Cyan
Write-Host "  Sizing mode:          $SizeMode" -ForegroundColor Cyan
Write-Host "  Size range:           [$SizeMinUsdc, $SizeMaxUsdc] USDC" -ForegroundColor Cyan
Write-Host "  Liquidity mult:       $SizeLiquidityMult" -ForegroundColor Cyan
Write-Host "  Mode:                 $modeLabel" -ForegroundColor $(if ($Live) { "Red" } else { "Green" })
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# Activate venv
# ---------------------------------------------------------------------------

if (Test-Path ".venv/Scripts/Activate.ps1") {
    . .venv/Scripts/Activate.ps1
} elseif (Test-Path "venv/Scripts/Activate.ps1") {
    . venv/Scripts/Activate.ps1
}

# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------

$env:DRY_RUN                         = $dryRun
$env:CLONE_ENABLED                   = "1"
$env:CLONE_HF_MODE_ENABLED           = "1"
$env:CLONE_WALLET                    = $Wallet
$env:CLONE_PROFILE_PATH              = $profilePath
$env:LIVE_DEPLOY_MODE                = $Stage

# HF configuration
$env:CLONE_COMBINED_PRICE_MIN        = "$CombinedPriceMin"
$env:CLONE_COMBINED_PRICE_MAX        = "$CombinedPriceMax"
$env:CLONE_HEDGE_TIMEOUT_SECS        = "$HedgeTimeoutSecs"
$env:CLONE_HEDGE_TAKER_FALLBACK_SECS = "$HedgeTakerFallbackSecs"
$env:CLONE_MAX_SLIPPAGE_BPS          = "$MaxSlippageBps"
$env:CLONE_CYCLE_INTERVAL_SECS       = "$CycleIntervalSecs"
$env:CLONE_HF_MAX_POSITIONS          = "$MaxPositions"

# Dynamic sizing
$env:CLONE_SIZE_MODE                 = $SizeMode
$env:CLONE_SIZE_MIN_USDC             = "$SizeMinUsdc"
$env:CLONE_SIZE_MAX_USDC             = "$SizeMaxUsdc"
$env:CLONE_SIZE_MATCH_TARGET         = "1"
$env:CLONE_SIZE_LIQUIDITY_MULT       = "$SizeLiquidityMult"

# Isolate HF clone — disable other noisy strategies
$env:MM_TARGET_MARKETS               = "0"
$env:CLONE_HF_PAPER_SIMULATE_PARTIAL = "0"

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

$allVars = @(
    "CLONE_ENABLED", "CLONE_HF_MODE_ENABLED", "CLONE_WALLET", "CLONE_PROFILE_PATH",
    "LIVE_DEPLOY_MODE",
    "CLONE_COMBINED_PRICE_MIN", "CLONE_COMBINED_PRICE_MAX",
    "CLONE_HEDGE_TIMEOUT_SECS", "CLONE_HEDGE_TAKER_FALLBACK_SECS",
    "CLONE_MAX_SLIPPAGE_BPS", "CLONE_CYCLE_INTERVAL_SECS", "CLONE_HF_MAX_POSITIONS",
    "CLONE_SIZE_MODE", "CLONE_SIZE_MIN_USDC", "CLONE_SIZE_MAX_USDC",
    "CLONE_SIZE_MATCH_TARGET", "CLONE_SIZE_LIQUIDITY_MULT",
    "MM_TARGET_MARKETS", "CLONE_HF_PAPER_SIMULATE_PARTIAL"
)

try {
    python bot.py
} finally {
    foreach ($v in $allVars) {
        Remove-Item "Env:\$v" -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "Session ended. Review with:" -ForegroundColor Gray
    Write-Host "  python -m analytics.performance_report" -ForegroundColor Gray
    Write-Host "  python -m analytics.fill_quality" -ForegroundColor Gray
    if ($Live) {
        Write-Host "  python -m analytics.live_proxy_validator" -ForegroundColor Gray
    }
}
