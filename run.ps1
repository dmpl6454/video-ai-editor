# run.ps1 — Launch Video AI Editor on Windows.
#
# Mirrors run.sh: uses PYTHONPATH=src instead of the editable-install .pth so
# launch behavior matches macOS. (The macOS Spotlight .pth hidden-flag bug does
# not exist on Windows, but PYTHONPATH is harmless and keeps parity.)
#
# Usage:  powershell -ExecutionPolicy Bypass -File run.ps1
#         powershell -ExecutionPolicy Bypass -File run.ps1 -NoConsole
#
# IMPORTANT — this console window OWNS the app. Closing it sends CTRL_CLOSE to
# the process tree, which ffmpeg's handler maps to SIGTERM, so an in-flight
# render dies mid-write. (That is the "I closed the terminal and then the video
# showed an error" report: the toast even quoted ffmpeg's own
# "Exiting normally, received signal 15".) Leave it open, or use -NoConsole.
#
# NOTE: per-task terminal windows are already gone — every child process is
# spawned with CREATE_NO_WINDOW (platformutil.SUBPROCESS_FLAGS). This window is
# the launcher's own, present for the whole session, not one per action.
#
# -NoConsole is OPT-IN on purpose: pythonw has no console at all, and a
# windowless launch is the same shape as the detached launch that has been
# reported to leave the pywebview window "Not Responding" — a scenario that has
# not been root-caused yet. Diagnostics survive either way now: the app also logs
# to %APPDATA%\Video AI Editor\logs\app.log.
param([switch]$NoConsole)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$exeName = if ($NoConsole) { "pythonw.exe" } else { "python.exe" }
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\$exeName"
if (-not (Test-Path $venvPy)) {
    if ($NoConsole) {
        Write-Error "No pythonw.exe in .venv\Scripts. Re-run without -NoConsole."
    } else {
        Write-Error "No venv found. Run:  uv sync --python 3.13 --all-extras --group dev"
    }
    exit 1
}

$env:PYTHONPATH = (Join-Path $PSScriptRoot "src") +
    $(if ($env:PYTHONPATH) { ";" + $env:PYTHONPATH } else { "" })

& $venvPy -m video_ai_editor.desktop @args
