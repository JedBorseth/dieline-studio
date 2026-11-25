# Builds the engine as a single-file Windows console executable and puts it
# where Tauri's externalBin expects it:
#   app/src-tauri/binaries/dieline-engine-<rust host triple>.exe
#
# Run from any directory on the Windows build machine (needs uv and rustc):
#   powershell -ExecutionPolicy Bypass -File engine/scripts/build-sidecar.ps1
# Then build the installer from app/:
#   pnpm tauri build --config src-tauri/tauri.release.conf.json
#
# -WorkDir moves PyInstaller's large intermediate files out of the repo.

param(
    [string]$WorkDir = ""
)

$ErrorActionPreference = "Stop"
$engine = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $engine
if (-not $WorkDir) { $WorkDir = Join-Path $engine "build" }
$distDir = Join-Path $engine "dist"

$triple = ((rustc -vV | Select-String "^host:").ToString() -split " ")[1].Trim()
if (-not $triple) { throw "could not read the rust host triple from rustc -vV" }

Push-Location $engine
try {
    uv sync --group bundle
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

    # Console subsystem on purpose: the app talks to the engine over stdio and
    # starts it with CREATE_NO_WINDOW, so no console window appears.
    uv run pyinstaller `
        --noconfirm --clean --onefile --console `
        --name dieline-engine `
        --paths src `
        --collect-submodules keyring.backends `
        --copy-metadata keyring `
        --workpath $WorkDir `
        --specpath $WorkDir `
        --distpath $distDir `
        src/dieline_engine/__main__.py
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
}
finally {
    Pop-Location
}

$binDir = Join-Path $repo "app/src-tauri/binaries"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$target = Join-Path $binDir "dieline-engine-$triple.exe"
Copy-Item (Join-Path $distDir "dieline-engine.exe") $target -Force
Write-Host "Sidecar ready: $target"

# Smoke test: the engine must answer engine.info over stdio.
$reply = '{"jsonrpc":"2.0","id":1,"method":"engine.info"}' | & $target | Select-Object -Skip 1 -First 1
if ($reply -notmatch '"result"') { throw "sidecar smoke test failed: $reply" }
Write-Host "Smoke test passed."
