param(
    [string]$Wallet                  = $env:CLONE_WALLET,
    [double]$CombinedPriceMin        = 0.85,
    [double]$CombinedPriceMax        = 0.97,
    [int]   $HedgeTimeoutSecs        = 30,
    [int]   $HedgeTakerFallbackSecs  = 10,
    [double]$MaxSlippageBps          = 50.0,
    [int]   $CycleIntervalSecs       = 5,
    [int]   $MaxPositions            = 5,
    [switch]$SimulatePartial,
    [double]$Aggressiveness          = 1.0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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
    Write-Host "WARNING: HedgeTakerFallbackSecs ($HedgeTakerFallbackSecs) >= HedgeTimeoutSecs ($HedgeTimeoutSecs)" -ForegroundColor Yellow
    Write-Host "         Adjusting HedgeTimeoutSecs to $($HedgeTakerFallbackSecs + 5)s" -ForegroundColor Yellow
    $HedgeTimeoutSecs = $HedgeTakerFallbackSecs + 5
}

$simLabel = if ($SimulatePartial) { "YES" } else { "NO" }
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  POLYMARKET WALLET CLONE - HF HEDGE MODE (PAPER)" -ForegroundColor Cyan
Write-Host "  Target wallet:        $Wallet" -ForegroundColor Cyan
Write-Host "  Combined price band:  [$CombinedPriceMin, $CombinedPriceMax]" -ForegroundColor Cyan
Write-Host "  Hedge timeout:        ${HedgeTimeoutSecs}s" -ForegroundColor Cyan
Write-Host "  Taker fallback:       ${HedgeTakerFallbackSecs}s" -ForegroundColor Cyan
Write-Host "  Max slippage:         ${MaxSlippageBps} bps" -ForegroundColor Cyan
Write-Host "  Cycle interval:       ${CycleIntervalSecs}s" -ForegroundColor Cyan
Write-Host "  Max positions:        $MaxPositions" -ForegroundColor Cyan
Write-Host "  Simulate partial:     $simLabel" -ForegroundColor Cyan
Write-Host "  Aggressiveness:       $Aggressiveness" -ForegroundColor Cyan
Write-Host "  Mode:                 DRY_RUN=1 (paper)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

if (Test-Path ".venv/Scripts/Activate.ps1") {
    . .venv/Scripts/Activate.ps1
} elseif (Test-Path "venv/Scripts/Activate.ps1") {
    . venv/Scripts/Activate.ps1
}

$env:DRY_RUN                         = "1"
$env:CLONE_ENABLED                   = "1"
$env:CLONE_HF_MODE_ENABLED           = "1"
$env:CLONE_WALLET                    = $Wallet
$env:CLONE_PROFILE_PATH              = $profilePath
$env:CLONE_AGGRESSIVENESS            = "$Aggressiveness"
$env:CLONE_COMBINED_PRICE_MIN        = "$CombinedPriceMin"
$env:CLONE_COMBINED_PRICE_MAX        = "$CombinedPriceMax"
$env:CLONE_HEDGE_TIMEOUT_SECS        = "$HedgeTimeoutSecs"
$env:CLONE_HEDGE_TAKER_FALLBACK_SECS = "$HedgeTakerFallbackSecs"
$env:CLONE_MAX_SLIPPAGE_BPS          = "$MaxSlippageBps"
$env:CLONE_CYCLE_INTERVAL_SECS       = "$CycleIntervalSecs"
$env:CLONE_HF_MAX_POSITIONS          = "$MaxPositions"
$env:CLONE_HF_PAPER_SIMULATE_PARTIAL = $(if ($SimulatePartial) { "1" } else { "0" })

# Isolate HF clone behavior: disable market-maker order spam during clone tuning
$env:MM_TARGET_MARKETS               = "0"

try {
    python bot.py
} finally {
    $hfVars = @(
        "CLONE_ENABLED", "CLONE_HF_MODE_ENABLED", "CLONE_WALLET", "CLONE_PROFILE_PATH",
        "CLONE_AGGRESSIVENESS", "CLONE_COMBINED_PRICE_MIN", "CLONE_COMBINED_PRICE_MAX",
        "CLONE_HEDGE_TIMEOUT_SECS", "CLONE_HEDGE_TAKER_FALLBACK_SECS",
        "CLONE_MAX_SLIPPAGE_BPS", "CLONE_CYCLE_INTERVAL_SECS",
        "CLONE_HF_MAX_POSITIONS", "CLONE_HF_PAPER_SIMULATE_PARTIAL",
        "MM_TARGET_MARKETS"
    )
    foreach ($v in $hfVars) {
        Remove-Item "Env:\$v" -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "Session ended. Review logs then run:" -ForegroundColor Gray
    Write-Host "  python -m analytics.performance_report" -ForegroundColor Gray
}
