# Sync **Docker runtime files only** from kng_bot3 into the KNG3 mirror checkout.
# KNG3 is a minimal repo (root Dockerfile + v7-only main.py). Do NOT copy the full PALADIN
# research tree or monolithic main.py — that breaks the image (missing modules / bloat).
#
# Paths: deploy/KNG3_MIRROR.txt — edit MIRROR_LOCAL_PATH if your mirror moves.
#
# File list must stay aligned with KNG3's Dockerfile COPY lines only.
# Docker / compose for production: repo at MIRROR_LOCAL_PATH in KNG3_MIRROR.txt — not under kng_bot3/deploy.

$ErrorActionPreference = "Stop"
$deployDir = $PSScriptRoot
$repoRoot = Split-Path -Parent $deployDir
$mirrorFile = Join-Path $deployDir "KNG3_MIRROR.txt"
if (-not (Test-Path $mirrorFile)) {
    throw "Missing deploy/KNG3_MIRROR.txt next to this script."
}
$dst = $null
Get-Content $mirrorFile | ForEach-Object {
    $line = $_.TrimStart([char]0xFEFF)
    if ($line -match '^\s*MIRROR_LOCAL_PATH=(.+)$') {
        $dst = $matches[1].Trim()
    }
}
if (-not $dst) { throw "KNG3_MIRROR.txt must contain MIRROR_LOCAL_PATH=..." }
if (-not (Test-Path (Join-Path $dst ".git"))) {
    throw "Mirror path is not a git repo: $dst"
}

$src = $repoRoot
Write-Host "Sync kng_bot3 -> KNG3 (Docker runtime files ONLY)"
Write-Host "  SRC: $src"
Write-Host "  DST: $dst"

$rootFiles = @(
    "config.py",
    "trader.py",
    "market_locator.py",
    "btc_price_feed.py",
    "http_session.py",
    "polymarket_ws.py",
    "clob_fak.py",
    "paladin_v7_live_engine.py",
    "btc15_redeem_engine.py",
    "paladin_live_engine.py",
    "signal_analyzer.py"
)

foreach ($f in $rootFiles) {
    $sp = Join-Path $src $f
    if (-not (Test-Path $sp)) { throw "Missing source file: $sp" }
    Copy-Item -Path $sp -Destination (Join-Path $dst $f) -Force
}

$paladinFiles = @(
    "paladin_engine.py",
    "paladin_v7.py",
    "simulate_paladin_window.py",
    "paladin_sim_config.json"
)
$paladinDstDir = Join-Path $dst "PALADIN"
if (-not (Test-Path $paladinDstDir)) {
    New-Item -ItemType Directory -Path $paladinDstDir | Out-Null
}
foreach ($f in $paladinFiles) {
    $sp = Join-Path (Join-Path $src "PALADIN") $f
    if (-not (Test-Path $sp)) { throw "Missing source file: $sp" }
    Copy-Item -Path $sp -Destination (Join-Path $paladinDstDir $f) -Force
}

Write-Host "Done. In KNG3: git status, git diff, then commit + push."
Write-Host "Do NOT run git add -A on KNG3 unless you intend to ship non-Docker artifacts."
