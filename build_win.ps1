# build_win.ps1 — Build the Windows app folder via PyInstaller.
#
#   powershell -ExecutionPolicy Bypass -File build_win.ps1
#
# Output: dist\Video AI Editor\Video AI Editor.exe  (+ supporting DLLs/data)
# Notes:
#   - ffmpeg/whisper-cli/realesrgan are NOT bundled; they must be on PATH or in
#     the per-OS model dirs at runtime (same policy as the macOS build).
#   - The Microsoft Edge WebView2 Runtime must be present on the target machine
#     (preinstalled on Win11 / most Win10; else ship the Evergreen bootstrapper).
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Build the frontend first — pywebview serves frontend/dist.
# UNCONDITIONALLY, mirroring build_app.sh: the old `if (-not (Test-Path ...))`
# guard meant a bundle left over from an earlier checkout satisfied it forever,
# so the packaged Windows app shipped a stale UI (this is why three tester
# rounds re-reported already-fixed frontend bugs).
Write-Host "[build] rebuilding frontend/dist from scratch"
if (Test-Path "frontend\dist") { Remove-Item -Recurse -Force "frontend\dist" }
Push-Location frontend
& npm run build
# $ErrorActionPreference='Stop' does NOT trip on a native executable's non-zero
# exit, so a failed npm build would otherwise sail on and PyInstaller would
# package whatever (nothing) is in dist.
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm run build failed ($LASTEXITCODE)" }
Pop-Location

# Bake the exact source revision in — there is no git inside the packaged app,
# so config.build_id() reads this file. Mirrors build_app.sh.
$BuildSha = (& git rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $BuildSha) { $BuildSha = "unknown" }
# A `-dirty` suffix alone does NOT identify a build. During an uncommitted fix
# round every rebuild reports the same `<sha>-dirty`, so "is the user running my
# fix?" is unanswerable from the badge — which is the exact question this whole
# mechanism exists to answer, and it cost a round: a fix was verified in the new
# build, reported as still broken, and neither side could tell whether the same
# bits were on screen. The stamp is minute-resolution local time, appended ONLY
# when dirty, so a committed build keeps its clean `<sha>` identity.
if ((& git status --porcelain 2>$null)) {
  $BuildSha = "$BuildSha-dirty+" + (Get-Date -Format "MMddHHmm")
}
# Write BOM-LESS. `Set-Content -Encoding utf8` is BOM-less only in PowerShell 7+;
# under PowerShell 5.1 — which is what `powershell -File build_win.ps1` resolves
# to, the documented launch command — it prepends EF BB BF. `.strip()` does not
# remove a BOM, so config.build_id() reported "<BOM>c93af1e-dirty": a sha that
# matches no git object, in the one mechanism that exists to make "which build?"
# answerable. build_id() is BOM-tolerant now too; this keeps the file clean.
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "BUILD_ID"), $BuildSha,
    (New-Object System.Text.UTF8Encoding $false))
Write-Host "[build] BUILD_ID=$BuildSha"

# Drive the cross-platform spec (BUNDLE is darwin-guarded; COLLECT yields the
# dist folder on Windows).
uv run pyinstaller --noconfirm "Video AI Editor.spec"
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed ($LASTEXITCODE)" }

# Assert the packaged UI IS the one just built. Same rule as the /api/version
# build id and desktop.py's mtime check: what ships must equal what runs.
# A frozen app that serves a stale bundle is invisible from the outside — the
# window opens, every route answers 200 — and it reads as "the fix didn't work",
# which has already cost multiple re-investigation rounds here. Comparing the
# content-hashed asset names is enough: Vite renames on every content change.
$srcAssets = Get-ChildItem "frontend\dist\assets" -Filter *.js -EA SilentlyContinue |
    Select-Object -ExpandProperty Name | Sort-Object
$outAssets = Get-ChildItem "dist\Video AI Editor\_internal\frontend\dist\assets" `
    -Filter *.js -EA SilentlyContinue | Select-Object -ExpandProperty Name | Sort-Object
if (-not $outAssets) { throw "packaged app contains no frontend bundle" }
if (Compare-Object $srcAssets $outAssets) {
    throw ("packaged frontend is STALE: built [$($srcAssets -join ', ')] " +
           "but bundled [$($outAssets -join ', ')]")
}
Write-Host "[build] packaged UI matches the freshly built bundle ($($srcAssets -join ', '))"

Write-Host ""
Write-Host "[build] Done -> dist\Video AI Editor\Video AI Editor.exe"
Write-Host "[build] Wrap it in an installer with Inno Setup or WiX for distribution."
