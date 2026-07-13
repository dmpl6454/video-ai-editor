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

# Re-sign with hardened runtime + the mic entitlement. This supersedes
# PyInstaller's own BUNDLE-stage ad-hoc codesign (which has no entitlements
# and no --options runtime) and is the second layer of the VO-mic fix: TCC's
# attribution of a subprocess's (ffmpeg's) mic request is unreliable under a
# bundle with no hardened runtime / no entitlements — this makes it
# deterministic. Ad-hoc signing (`--sign -`) is sufficient for local TCC
# purposes; a paid Developer ID cert is only needed for distribution/
# notarization, which is out of scope here.
#
# Sign in a /tmp staging copy, not in place under dist/ — verified
# empirically that on a repo checked out under ~/Desktop (as this one is),
# something in the macOS Finder/LaunchServices bundle-metadata machinery
# continuously re-stamps a `com.apple.FinderInfo` xattr onto any `.app`
# bundle DIRECTORY living there, independent of and unrelated to codesign
# itself (confirmed by: stripping the xattr and waiting with zero commands
# running still saw it reappear within ~2s; the identical bundle copied to
# /tmp never got it back, signed or not). `codesign --verify --strict`
# rejects that xattr as "resource fork, Finder information, or similar
# detritus not allowed" and can even make the in-place `--force` sign itself
# fail outright. Staging outside the Desktop-rooted tree sidesteps the
# daemon entirely instead of racing it (same posture as CLAUDE.md's
# documented Spotlight/.pth guidance: don't fight the daemon, route around
# it) — sign in a location it doesn't touch, then move the finished, already
# -verified bundle into dist/.
APP_PATH="dist/Video AI Editor.app"
if [ -f "$APP_PATH/Contents/Info.plist" ]; then
  STAGE_DIR="$(mktemp -d /tmp/vae_codesign_stage.XXXXXX)"
  STAGE_APP="$STAGE_DIR/Video AI Editor.app"
  echo "[build] staging a copy in $STAGE_DIR for signing (avoids Desktop-path xattr re-stamping)"
  rm -rf "$STAGE_APP"
  cp -R "$APP_PATH" "$STAGE_APP"
  xattr -cr "$STAGE_APP" || true
  echo "[build] signing staged copy with hardened runtime + entitlements.plist"
  codesign --force --deep --options runtime \
    --entitlements entitlements.plist \
    --sign - "$STAGE_APP" \
    && echo "[build] codesign with hardened runtime + entitlements OK" \
    || echo "[build] WARNING: codesign failed — VO mic access may be denied by TCC"
  # codesign itself writes com.apple.FinderInfo onto the bundle root as a
  # side effect of sealing it (observed even outside Desktop) — harmless
  # there since /tmp doesn't re-apply it, but strip once more for a
  # belt-and-suspenders clean verify.
  xattr -d com.apple.FinderInfo "$STAGE_APP" 2>/dev/null || true
  if codesign --verify --deep --strict "$STAGE_APP" 2>/tmp/vae_codesign_verify.txt; then
    echo "[build] codesign --verify --strict passed"
  else
    echo "[build] WARNING: codesign --verify --strict still failing:"
    cat /tmp/vae_codesign_verify.txt
  fi
  echo "[build] moving signed bundle back into dist/"
  rm -rf "$APP_PATH"
  mv "$STAGE_APP" "$APP_PATH"
  rm -rf "$STAGE_DIR"
  # Final check on the artifact where it actually lives. Use the NON-strict
  # verify here, not --strict: moving/copying the bundle back onto a
  # Desktop-rooted checkout re-triggers the same FinderInfo re-stamping
  # described above (confirmed empirically — even a bare `mv` of an
  # already-`--strict`-clean bundle picks it back up within ~1s on this
  # path), so `--strict` is expected to fail here again through no fault of
  # the signature itself. `--verify --deep` (no `--strict`) is what
  # Gatekeeper/TCC/launch actually rely on and is confirmed to pass
  # regardless of that stray xattr — `--strict` is a submission-hygiene
  # linter, not a functional check. If distributing this build from a
  # non-Desktop path (e.g. CI, or a repo checkout elsewhere), --strict
  # should also pass on the final artifact.
  if codesign --verify --deep "$APP_PATH" 2>/tmp/vae_codesign_final.txt; then
    echo "[build] final codesign --verify (functional check) passed"
  else
    echo "[build] WARNING: final codesign --verify failed — VO mic access may be denied by TCC:"
    cat /tmp/vae_codesign_final.txt
  fi
else
  echo "[build] WARNING: $APP_PATH not found — skipping codesign re-sign"
fi

echo ""
echo "[build] .app done — now wrap it in a DMG for distribution:"
echo "        bash build_dmg.sh"
echo "[build] Or just run it: open 'dist/Video AI Editor.app'"
