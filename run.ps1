# run.ps1 — Launch Video AI Editor on Windows.
#
# Mirrors run.sh: uses PYTHONPATH=src instead of the editable-install .pth so
# launch behavior matches macOS. (The macOS Spotlight .pth hidden-flag bug does
# not exist on Windows, but PYTHONPATH is harmless and keeps parity.)
#
# Usage:  powershell -ExecutionPolicy Bypass -File run.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Error "No venv found. Run:  uv sync --python 3.13 --all-extras --group dev"
    exit 1
}

$env:PYTHONPATH = (Join-Path $PSScriptRoot "src") +
    $(if ($env:PYTHONPATH) { ";" + $env:PYTHONPATH } else { "" })

& $venvPy -m video_ai_editor.desktop @args
