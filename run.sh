#!/bin/bash
# Launch Video AI Editor.
#
# Uses PYTHONPATH=src instead of relying on the editable-install .pth file,
# because Spotlight's metadata daemon (com.apple.metadata.mdflagwriter) marks
# .pth files with the macOS hidden flag (UF_HIDDEN) within ~1s, and Python
# 3.13+ skips hidden .pth files — which silently breaks the editable install
# and causes `ModuleNotFoundError: No module named 'video_ai_editor'`.
# PYTHONPATH sidesteps the .pth mechanism entirely, so this launch is immune.
#
# Usage:  bash run.sh            (or ./run.sh after chmod +x)

set -euo pipefail
cd "$(dirname "$0")"

VENV_PY=".venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "No venv found. Run:  uv sync --python 3.13 --all-extras --group dev"
  exit 1
fi

exec env PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$VENV_PY" -m video_ai_editor.desktop "$@"
