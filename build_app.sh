#!/usr/bin/env bash
# Build a macOS .app bundle via PyInstaller. Run:
#   uv run bash build_app.sh
#
# Output: dist/Video AI Editor.app
# Caveats:
#   - ffmpeg / piper / realesrgan binaries must be on PATH at runtime
#     (not bundled). For a redistributable build, add them via --add-binary.
#   - Heavy ML libs (torch, demucs, mediapipe, faster-whisper) are excluded
#     to keep the bundle small (~150MB). Users who need those features run
#     the dev `uv run video-ai-editor` instead.
#   - First launch may be slow as macOS verifies the unsigned bundle.

set -euo pipefail

# Make sure the frontend is built first — pywebview opens dist/ directly.
if [ ! -d frontend/dist ]; then
  echo "[build] frontend/dist missing — running npm run build"
  (cd frontend && npm run build)
fi

uv run pyinstaller \
  --name "Video AI Editor" \
  --windowed \
  --noconfirm \
  --osx-bundle-identifier com.user.videoaieditor \
  --add-data "frontend/dist:frontend/dist" \
  --add-data "fonts:fonts" \
  --add-data "presets:presets" \
  --hidden-import "uvicorn.lifespan.on" \
  --hidden-import "uvicorn.protocols.websockets.auto" \
  --hidden-import "uvicorn.loops.auto" \
  --hidden-import "uvicorn.protocols.http.auto" \
  --hidden-import "uvicorn.logging" \
  --hidden-import "video_ai_editor.main" \
  --collect-submodules video_ai_editor \
  --collect-data webview \
  --exclude-module torch \
  --exclude-module torchcodec \
  --exclude-module torchvision \
  --exclude-module mediapipe \
  --exclude-module demucs \
  --exclude-module faster_whisper \
  --exclude-module librosa \
  --exclude-module scipy \
  --exclude-module matplotlib \
  --exclude-module tkinter \
  --exclude-module pyannote \
  src/video_ai_editor/desktop.py

echo ""
echo "[build] Done. Open: open 'dist/Video AI Editor.app'"
