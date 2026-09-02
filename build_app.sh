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
# The typecheck MUST be `tsc -b`, not `tsc --noEmit`. frontend/tsconfig.json is
# a solution file (`"files": []` + only `references`), so plain `tsc` builds a
# program of ZERO files and exits 0 — i.e. the old gate here type-checked
# nothing at all (verified: `npx tsc --noEmit --listFiles` prints 0 lines).
# Only build mode descends into tsconfig.app.json / tsconfig.node.json.
# This script used to avoid `tsc -b` because it failed on 4 pre-existing errors;
# those are fixed now, so the real gate is back on. Kept as two explicit steps
# rather than `npm run build` so a typecheck failure is distinguishable from a
# bundling failure in the log. vite build's esbuild transpile is what actually
# emits frontend/dist.
echo "[build] rebuilding frontend/dist"
rm -rf frontend/dist
(cd frontend && npx tsc -b --force && npx vite build)

# Bake the exact source revision into the bundle. There is no git inside a
# packaged .app, so config.build_id() reads this file; without it every shipped
# build is indistinguishable from every other (the reason three tester rounds
# re-reported already-fixed bugs against "v0.3.7").
BUILD_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
# A bare `-dirty` suffix does NOT identify a build. During an uncommitted fix
# round every rebuild reports the same `<sha>-dirty`, so "is the user running my
# fix?" — the one question this mechanism exists to answer — becomes
# unanswerable from the badge. It cost a round on the Windows side: a fix was
# verified in a new build, reported as still broken, and neither side could tell
# whether the same bits were on screen. Minute-resolution local time, appended
# ONLY when dirty, so a committed build keeps its clean `<sha>` identity.
# Mirrors build_win.ps1 — the two builds must stamp identity the same way, or
# the platform that doesn't gets the ambiguity back.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  BUILD_SHA="${BUILD_SHA}-dirty+$(date +%m%d%H%M)"
fi
echo "$BUILD_SHA" > BUILD_ID
echo "[build] BUILD_ID=$BUILD_SHA"

# ai/translate.py (MADLAD-400 via CTranslate2) replaced Argos Translate — see
# the module's own docstring and Video AI Editor.spec for the history. Argos
# needed an explicit collect-submodules flag for its own package name here because it
# loaded itself via a STRING (`importlib.import_module`), invisible to
# PyInstaller's static analysis on EITHER build path (this script does NOT
# use Video AI Editor.spec — CLAUDE.md — so the two needed the fix
# independently, and this one was found missing after the .spec already had
# it). `ctranslate2`/`sentencepiece` are plain static imports and need no
# equivalent collect. `huggingface_hub` DOES, despite also being a plain
# static import: it lazy-loads its own submodules via `__getattr__` rather
# than eager imports, so `huggingface_hub.snapshot_download` is a runtime
# attribute resolution invisible to PyInstaller's static walk — found by
# diffing the Windows `dist/` tree (the .spec fix landed first; this line
# mirrors it here for the same reason the argostranslate collect had to be
# independently added to both paths). This one is NOT Argos-specific
# clean-up — it also fixes faster-whisper's OWN model auto-download
# (`faster_whisper/utils.py` calls the identical `huggingface_hub.
# snapshot_download`), silently broken in every previous packaged build
# whenever a model wasn't already cached. This IS the one build (macOS) that
# additionally excludes torch below, and Argos's removal is what makes
# translation actually reachable here at all: Argos pulled in `stanza`
# (PyTorch-based) for sentence splitting, so on THIS build specifically the
# old "translate" feature was excluded transitively even though nothing
# declared it so. MADLAD needs no torch.
#
# PyInstaller's CLI mode WRITES a fresh "<name>.spec" into --specpath, which
# defaulted to the repo root — clobbering the committed `Video AI Editor.spec`
# that the Windows build (build_win.ps1) and tests/test_transcribe_backend.py
# ::test_spec_bundles_faster_whisper_data_files depend on. Emit the throwaway
# spec under build/ (git-ignored, .gitignore:8) instead. Relative --add-data
# SOURCES are resolved against the spec's directory, not the CWD (PyInstaller
# building/build_main.py, format_binaries_and_datas(workingdir=spec_dir)), so
# every source below is made absolute with $ROOT; destinations are unaffected.
# --workpath/--distpath still default to ./build and ./dist.
ROOT="$(pwd)"
SPEC_DIR="$ROOT/build/pyinstaller-spec"
mkdir -p "$SPEC_DIR"
uv run pyinstaller \
  --name "Video AI Editor" \
  --windowed \
  --noconfirm \
  --specpath "$SPEC_DIR" \
  --osx-bundle-identifier com.user.videoaieditor \
  --add-data "$ROOT/frontend/dist:frontend/dist" \
  --add-data "$ROOT/fonts:fonts" \
  --add-data "$ROOT/presets:presets" \
  --add-data "$ROOT/VERSION:." \
  --add-data "$ROOT/BUILD_ID:." \
  --hidden-import "uvicorn.lifespan.on" \
  --hidden-import "uvicorn.protocols.websockets.auto" \
  --hidden-import "uvicorn.loops.auto" \
  --hidden-import "uvicorn.protocols.http.auto" \
  --hidden-import "uvicorn.logging" \
  --hidden-import "video_ai_editor.main" \
  --collect-submodules video_ai_editor \
  --collect-submodules huggingface_hub \
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

# PyInstaller's CLI mode (used here, not the committed .spec — the generated
# one lands in $SPEC_DIR via --specpath, see above and CLAUDE.md) has no
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
  # PyInstaller's CLI mode has no version flag either, so CFBundleShortVersionString
  # ships as "0.0.0" (Finder "Get Info", crash reports, the DMG's own metadata)
  # unless it is stamped here — the .spec's `version=` kwarg only exists on
  # the spec (Windows) path. Same source of truth as /api/version: VERSION.
  APP_VERSION="$(tr -d '[:space:]' < VERSION)"
  for KEY in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$KEY $APP_VERSION" "$PLIST" 2>/dev/null \
      || /usr/libexec/PlistBuddy -c "Add :$KEY string $APP_VERSION" "$PLIST"
  done
  echo "[build] stamped Info.plist CFBundleShortVersionString/CFBundleVersion = $APP_VERSION"
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
