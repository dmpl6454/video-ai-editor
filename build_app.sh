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

# Always rebuild the frontend before packaging — pywebview opens dist/
# directly, and a stale-but-present dist/ from a previous build would
# otherwise silently ship an old frontend with none of this session's
# changes (this guard used to be `if [ ! -d frontend/dist ]`, which only
# built on a first run and thereafter trusted whatever was already there).
#
# Deliberately `tsc --noEmit && vite build`, NOT `npm run build` (`tsc -b`).
# `tsc -b` is project-references incremental build mode and is strictly
# stricter — it currently fails on this repo (FrameScrubber.tsx mp4box.js
# type mismatch, Properties.tsx a JSX `label` boolean-shorthand passed where
# `label?: string` is declared), pre-existing issues unrelated to whatever
# this script is packaging. This mirrors the documented CI/dev check (see
# CLAUDE.md); vite build's own esbuild transpile is what actually produces
# frontend/dist, and tsc --noEmit is a pure typecheck gate that doesn't
# additionally fail on the tsc -b-only errors.
echo "[build] rebuilding frontend/dist"
rm -rf frontend/dist
(cd frontend && npx tsc --noEmit && npx vite build)

uv run pyinstaller \
  --name "Video AI Editor" \
  --windowed \
  --noconfirm \
  --osx-bundle-identifier com.user.videoaieditor \
  --add-data "frontend/dist:frontend/dist" \
  --add-data "fonts:fonts" \
  --add-data "presets:presets" \
  --add-data "VERSION:." \
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
  --exclude-module torchaudio \
  --exclude-module open_clip \
  --exclude-module timm \
  --exclude-module transformers \
  --exclude-module pandas \
  --exclude-module sklearn \
  --exclude-module rembg \
  --exclude-module simple_lama_inpainting \
  --exclude-module noisereduce \
  src/video_ai_editor/desktop.py

# PyInstaller's CLI mode (used here, not the .spec — see CLAUDE.md) has no
# flag for arbitrary Info.plist keys, so NSMicrophoneUsageDescription is
# added as a post-build step. Without it, macOS TCC silently denies mic
# access and navigator.mediaDevices is undefined in the webview regardless
# of anything the JS side does (VoRecorder.tsx guards against that case, but
# Record Voiceover is simply unusable in the packaged app without this key).
PLIST="dist/Video AI Editor.app/Contents/Info.plist"
if [ -f "$PLIST" ]; then
  /usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 'Record a voiceover track for your video.'" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription 'Record a voiceover track for your video.'" "$PLIST"
  echo "[build] added NSMicrophoneUsageDescription to Info.plist"
else
  echo "[build] WARNING: $PLIST not found — mic usage description NOT added"
fi

echo ""
echo "[build] .app done — now wrap it in a DMG for distribution:"
echo "        bash build_dmg.sh"
echo "[build] Or just run it: open 'dist/Video AI Editor.app'"
