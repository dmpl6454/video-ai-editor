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
if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Host "[build] frontend/dist missing — running npm run build"
    Push-Location frontend
    & npm run build
    Pop-Location
}

# Drive the cross-platform spec (BUNDLE is darwin-guarded; COLLECT yields the
# dist folder on Windows).
uv run pyinstaller --noconfirm "Video AI Editor.spec"

Write-Host ""
Write-Host "[build] Done -> dist\Video AI Editor\Video AI Editor.exe"
Write-Host "[build] Wrap it in an installer with Inno Setup or WiX for distribution."
