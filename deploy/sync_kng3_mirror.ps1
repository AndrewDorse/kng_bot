# Sync **Docker runtime files only** from kng_bot3 into the KNG3 mirror checkout.
# KNG3 is a minimal SHAMAN-only image (root Dockerfile + KNG3/main.py).
# **Do not sync root main.py** from kng_bot3: KNG3 keeps its own entrypoint.
#
# Paths: deploy/KNG3_MIRROR.txt — edit MIRROR_LOCAL_PATH if your mirror moves.
#
# File list must stay aligned with KNG3's Dockerfile COPY lines only.

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
    "http_session.py",
    "clob_fak.py",
    "polymarket_ws.py",
    "shaman_v1_engine.py"
)

foreach ($f in $rootFiles) {
    $sp = Join-Path $src $f
    if (-not (Test-Path $sp)) { throw "Missing source file: $sp" }
    Copy-Item -Path $sp -Destination (Join-Path $dst $f) -Force
}

$paladinFiles = @(
    "shaman_v1_eval.py",
    "shaman_v1_rules.json"
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
