#!/usr/bin/env bash
# Wrap the built .app into a distributable DMG.
# Prereq: run `uv run bash build_app.sh` first to produce dist/Video AI Editor.app
#
#   bash build_dmg.sh
#
# Output: dist/Video-AI-Editor.dmg  (compressed, with an Applications symlink
# so users drag-to-install).
#
# What's in it: the editor UI + timeline + ffmpeg-based editing + the MCP
# server. Heavy AI (CLIP search, large-v3 captions, diarization, upscale,
# slow-mo) is NOT bundled — those need the dev env (`uv run video-ai-editor`)
# plus system binaries (ffmpeg, whisper-cli, the ggml/rife/esrgan models).
set -euo pipefail

APP="dist/Video AI Editor.app"
DMG="dist/Video-AI-Editor.dmg"
STAGE="$(mktemp -d)/dmg"

if [ ! -d "$APP" ]; then
  echo "[dmg] $APP not found — run 'uv run bash build_app.sh' first." >&2
  exit 1
fi

mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -volname "Video AI Editor" \
  -srcfolder "$STAGE" \
  -ov -format UDZO \
  "$DMG"

rm -rf "$STAGE"
echo ""
echo "[dmg] Done → $DMG ($(du -h "$DMG" | cut -f1))"
echo "[dmg] To install: open '$DMG', drag the app to Applications."
echo "[dmg] First launch is unsigned — right-click → Open, or:"
echo "      xattr -dr com.apple.quarantine '/Applications/Video AI Editor.app'"
