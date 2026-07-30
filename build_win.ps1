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
if ((& git status --porcelain 2>$null)) { $BuildSha = "$BuildSha-dirty" }
Set-Content -Path "BUILD_ID" -Value $BuildSha -NoNewline -Encoding utf8
Write-Host "[build] BUILD_ID=$BuildSha"

# Drive the cross-platform spec (BUNDLE is darwin-guarded; COLLECT yields the
# dist folder on Windows).
uv run pyinstaller --noconfirm "Video AI Editor.spec"

Write-Host ""
Write-Host "[build] Done -> dist\Video AI Editor\Video AI Editor.exe"
Write-Host "[build] Wrap it in an installer with Inno Setup or WiX for distribution."
