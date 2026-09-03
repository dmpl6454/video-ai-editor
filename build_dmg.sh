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

# Defaults unchanged; the overrides exist for build_notarize.sh, which must
# wrap the STAPLED app (a notarization ticket lives inside the bundle, so the
# DMG has to be built after stapling, from that exact bundle) and, for its
# ad-hoc dry run, works on a copy under /tmp without touching dist/.
APP="${VAE_APP:-dist/Video AI Editor.app}"
DMG="${VAE_DMG:-dist/Video-AI-Editor.dmg}"
STAGE="$(mktemp -d)/dmg"

if [ ! -d "$APP" ]; then
  echo "[dmg] $APP not found — run 'uv run bash build_app.sh' first." >&2
  exit 1
fi

mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
# cp -R and hdiutil both preserve xattrs, so a Desktop-rooted dist/ would
# ship com.apple.FinderInfo (re-stamped by Finder — build_app.sh explains)
# inside the image and onto the user's /Applications copy, where
# `codesign --verify --strict` then reports "detritus not allowed" even
# though Gatekeeper is fine. Strip the copy; the seal lives in
# Contents/_CodeSignature, not in xattrs, so signatures and a stapled ticket
# survive. com.apple.provenance cannot be removed (xattr exits 0 and leaves
# it) and is harmless — spctl accepts a notarized app carrying it.
APP_COPY="$STAGE/$(basename "$APP")"
xattr -cr "$APP_COPY" 2>/dev/null || true
xattr -d com.apple.FinderInfo "$APP_COPY" 2>/dev/null || true
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
# The right-click/quarantine advice is for the ad-hoc dev build only. When
# build_notarize.sh calls this on a Developer-ID-signed (soon notarized)
# bundle the hint is wrong and would end up copied into release notes, so
# gate it on the payload's actual signature. Capture, then match: never
# `cmd | grep -q` under pipefail (SIGPIPE turns a match into a failure).
SIG_INFO="$(codesign -dv "$APP" 2>&1 || true)"
if [[ "$SIG_INFO" == *"Signature=adhoc"* ]]; then
  echo "[dmg] First launch is unsigned — right-click → Open, or:"
  echo "      xattr -dr com.apple.quarantine '/Applications/Video AI Editor.app'"
fi
