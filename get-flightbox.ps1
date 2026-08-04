# Flightbox bootstrapper (Windows) — run from the ROOT of the repo you want governed:
#
#   iwr -useb https://raw.githubusercontent.com/miguelsalcedo01/flightbox/main/get-flightbox.ps1 | iex
#
# Fetches the flightbox skill, stamps the factory into the current repo, and
# preps .env. Idempotent: existing files are skipped, never overwritten.
# Override the source with $env:FLIGHTBOX_REPO (a URL or a local checkout path).
$ErrorActionPreference = "Stop"

$repo = if ($env:FLIGHTBOX_REPO) { $env:FLIGHTBOX_REPO } else { "https://github.com/miguelsalcedo01/flightbox" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required" }
if (-not (Get-Command uv  -ErrorAction SilentlyContinue)) { throw "uv is required - https://docs.astral.sh/uv/" }

$tmp = $null
if (Test-Path (Join-Path $repo ".claude/skills/flightbox")) {
    $src = $repo                                 # local checkout, no network
} else {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("flightbox-" + [IO.Path]::GetRandomFileName())
    Write-Host "fetching flightbox from $repo ..."
    git clone --quiet --depth 1 $repo $tmp
    $src = $tmp
}

try {
    New-Item -ItemType Directory -Force ".claude/skills" | Out-Null
    if (Test-Path ".claude/skills/flightbox") { Remove-Item -Recurse -Force ".claude/skills/flightbox" }
    # tar (bsdtar, ships with Windows 10+), not Copy-Item: a used checkout has
    # node_modules/ and dist/ inside the visualizer — build artifacts, not the product.
    $skillsDir = Join-Path $src ".claude/skills"
    tar -C $skillsDir -cf - --exclude 'node_modules' --exclude 'dist' --exclude '__pycache__' flightbox |
        tar -C ".claude/skills" -xf -
    if ($LASTEXITCODE -ne 0) { throw "copying the skill failed" }

    uv run .claude/skills/flightbox/scripts/install.py
    if ($LASTEXITCODE -ne 0) { throw "install.py failed" }

    if (-not (Test-Path ".env") -and (Test-Path ".env.sample")) {
        Copy-Item ".env.sample" ".env"
        Write-Host "created .env - set your provider API keys in it"
    }

    Write-Host ""
    Write-Host "flightbox is stamped. next:"
    Write-Host "  1. edit .env (API keys)"
    Write-Host "  2. just demo          # two cheap read-only runs, end to end"
    Write-Host "  3. just obs           # the replay UI (needs bun)"
} finally {
    if ($tmp -and (Test-Path $tmp)) { Remove-Item -Recurse -Force $tmp }
}
