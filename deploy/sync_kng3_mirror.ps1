# Sync PALADIN / live bot sources from this repo (kng_bot3) into the KNG3 mirror checkout.
# Paths and URL are defined in deploy/KNG3_MIRROR.txt — edit that file if your mirror moves.

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
Write-Host "Sync kng_bot3 -> KNG3"
Write-Host "  SRC: $src"
Write-Host "  DST: $dst"

$files = @(
    "main.py",
    "config.py",
    "trader.py",
    "market_locator.py",
    "btc15_redeem_engine.py",
    "btc_price_feed.py",
    "signal_analyzer.py",
    "http_session.py",
    "polymarket_ws.py",
    "clob_fak.py",
    "paladin_live_engine.py",
    "paladin_v7_live_engine.py"
)

foreach ($f in $files) {
    $sp = Join-Path $src $f
    if (-not (Test-Path $sp)) { throw "Missing source file: $sp" }
    Copy-Item -Path $sp -Destination (Join-Path $dst $f) -Force
}

$paladinSrc = Join-Path $src "PALADIN"
$paladinDst = Join-Path $dst "PALADIN"
if (Test-Path $paladinDst) {
    Remove-Item -Recurse -Force $paladinDst
}
Copy-Item -Path $paladinSrc -Destination $paladinDst -Recurse -Force

Write-Host "Done. Next in KNG3: git status, git commit, git push origin main"
Write-Host "Tip: KNG3 Dockerfiles must COPY paladin_v7_live_engine.py (match kng_bot3 deploy/hostinger-docker/Dockerfile)."
