#!/usr/bin/env bash
# Wrap the built .app into a distributable DMG.
# Prereq: run `uv run bash build_app.sh` first to produce dist/Video AI Editor.app
#
#   bash build_dmg.sh
#
# Output: dist/Video-AI-Editor-<version>-AppleSilicon.dmg  (compressed, with an
# Applications symlink so users drag-to-install). The architecture is in the
# FILENAME and in the mounted volume's name because the bundle is arm64-only
# and nothing else told anyone: an Intel Mac cannot launch it at all, and the
# only signal a downloader got was a failure after the download. The tag is
# derived from the app's actual main executable, so it stays honest if a
# universal2 build ever lands.
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
STAGE="$(mktemp -d)/dmg"

if [ ! -d "$APP" ]; then
  echo "[dmg] $APP not found — run 'uv run bash build_app.sh' first." >&2
  exit 1
fi

# Read the architecture out of the built app rather than asserting it, so this
# script cannot end up promising "AppleSilicon" for a universal build (or the
# reverse). `file` is in the base system; `lipo` needs the Command Line Tools.
APP_EXE="$APP/Contents/MacOS/$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' \
  "$APP/Contents/Info.plist" 2>/dev/null || echo "Video AI Editor")"
ARCH_INFO="$(file -b "$APP_EXE" 2>/dev/null || true)"
ARCH_TAG=""; ARCH_HUMAN=""
case "$ARCH_INFO" in
  *arm64*x86_64*|*x86_64*arm64*) ARCH_TAG="Universal"; ARCH_HUMAN="Universal" ;;
  *arm64*)  ARCH_TAG="AppleSilicon"; ARCH_HUMAN="Apple Silicon" ;;
  *x86_64*) ARCH_TAG="Intel";        ARCH_HUMAN="Intel" ;;
esac

# VERSION is read the same way build_app.sh stamps Info.plist with it, so the
# DMG name, the bundle version and /api/version all come from one file.
VER="$(tr -d '[:space:]' < VERSION 2>/dev/null || true)"
DEFAULT_DMG="dist/Video-AI-Editor${VER:+-$VER}${ARCH_TAG:+-$ARCH_TAG}.dmg"
# The override is what build_notarize.sh uses (it wraps the STAPLED app and
# names the output itself, defaulting to dist/Video-AI-Editor.dmg). That path
# keeps working untouched; to carry this name through notarization, run it as
#   VAE_DMG="$DEFAULT_DMG" bash build_notarize.sh
DMG="${VAE_DMG:-$DEFAULT_DMG}"
mkdir -p "$(dirname "$DMG")"

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
# The volume name is the one piece of DMG chrome that already exists, and it is
# what a user reads in the Finder window they drag from — so the architecture
# goes there too rather than inventing a README/background. Verified that
# hdiutil accepts a 31-character volume name.
VOLNAME="Video AI Editor${ARCH_HUMAN:+ ($ARCH_HUMAN)}"
hdiutil create -volname "$VOLNAME" \
  -srcfolder "$STAGE" \
  -ov -format UDZO \
  "$DMG"

rm -rf "$STAGE"
echo ""
echo "[dmg] Done → $DMG ($(du -h "$DMG" | cut -f1))"
echo "[dmg] Volume name: $VOLNAME"
if [ "$ARCH_TAG" = "AppleSilicon" ]; then
  echo "[dmg] REQUIRES an Apple Silicon Mac (M1 or newer). Intel Macs cannot run"
  echo "      this build — say so wherever the download link lives."
fi
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
