#!/usr/bin/env bash
# Build a macOS .app bundle via PyInstaller. Run:
#   uv run bash build_app.sh
#
# Output: dist/Video AI Editor.app  (arm64 ONLY — see MIN_MACOS below and the
#         DMG name in build_dmg.sh; a universal2 build is out of scope.)
# Caveats:
#   - ffmpeg + ffprobe ARE bundled (static, --add-binary); the build FAILS if
#     they cannot be found, because an app that ships without them cannot
#     decode a frame on a Mac with no Homebrew. realesrgan / rife / whisper-cli
#     are still optional drop-ins resolved from PATH or the models/ dir.
#   - Heavy ML libs (torch, demucs, mediapipe) are excluded to keep the bundle
#     small. Users who need those features run the dev `uv run video-ai-editor`
#     instead. (Bundling ffmpeg/ffprobe adds ~95MB and piper's espeak-ng-data
#     ~24MB on top of the old ~150MB — a working app is worth the download.)
#   - faster-whisper IS bundled (it used to be excluded) — see the
#     --collect-data faster_whisper block below. Captions are a headline
#     feature, not an optional extra, and excluding it left the shipped DMG
#     answering the Captions button with "run the app from source", which a
#     DMG recipient cannot do.
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

# --- ffmpeg + ffprobe ARE the app; bundle them -------------------------------
# The shipped DMG did not contain them, and `_pu.FFMPEG`/`FFPROBE` were BARE
# names resolved through PATH while config.py's PATH augmentation added only
# Homebrew/MacPorts directories — never the bundle's own. Result on an audited
# notarized build: on a Mac with no Homebrew the app launched, and importing,
# previewing, thumbnailing and exporting every single file failed. Bundling is
# only half the fix; `platformutil.resolve_tool()` is the other half (an
# --add-binary payload nobody looks for changes nothing).
#
# Source, in priority order. The winner is echoed, and a miss is FATAL: a
# silent skip here recreates exactly the blocker this exists to fix.
#   1. $VAE_FFMPEG_DIR                                  — explicitly staged
#   2. ~/.cache/video-ai-editor/ffmpeg-static/<plat>     — the staged cache
#   3. the `static-ffmpeg` PyPI package, fetched into (2)
#
# They must be STATIC. A Homebrew ffmpeg links ~40 dylibs under /opt/homebrew,
# none of which are in the bundle, so copying one in yields a binary that dies
# in dyld on every machine but this one — which would look exactly like the bug
# we are fixing, with an extra day of confusion. The otool gate below refuses
# anything that references a non-system library.
FFBIN_PLATFORM="darwin_$(uname -m)"
FFBIN_CACHE="$HOME/.cache/video-ai-editor/ffmpeg-static/$FFBIN_PLATFORM"
FFBIN_STAGE="$ROOT/build/ffmpeg-bundle"

ffbin_pair_ok() {   # $1 = dir holding both binaries
  [ -n "${1:-}" ] && [ -x "$1/ffmpeg" ] && [ -x "$1/ffprobe" ]
}

FFBIN_SRC=""
FFBIN_FROM=""
if ffbin_pair_ok "${VAE_FFMPEG_DIR:-}"; then
  FFBIN_SRC="$VAE_FFMPEG_DIR"; FFBIN_FROM="VAE_FFMPEG_DIR=$VAE_FFMPEG_DIR"
elif ffbin_pair_ok "$FFBIN_CACHE"; then
  FFBIN_SRC="$FFBIN_CACHE"; FFBIN_FROM="staged cache $FFBIN_CACHE"
else
  echo "[build] no staged ffmpeg pair — fetching via the static-ffmpeg package"
  # One path per line, not `print(*...)`: a cache path with a space in it would
  # otherwise be unsplittable.
  FFBIN_FETCH="$(uv run --with 'static-ffmpeg==3.0' python -c \
    'from static_ffmpeg import run
ff, fp = run.get_or_fetch_platform_executables_else_raise()
print(ff)
print(fp)' 2>/dev/null || true)"
  FFBIN_A="$(printf '%s\n' "$FFBIN_FETCH" | sed -n '1p')"
  FFBIN_B="$(printf '%s\n' "$FFBIN_FETCH" | sed -n '2p')"
  if [ -f "$FFBIN_A" ] && [ -f "$FFBIN_B" ]; then
    mkdir -p "$FFBIN_CACHE"
    cp -f "$FFBIN_A" "$FFBIN_CACHE/ffmpeg"
    cp -f "$FFBIN_B" "$FFBIN_CACHE/ffprobe"
    chmod 755 "$FFBIN_CACHE/ffmpeg" "$FFBIN_CACHE/ffprobe"
    FFBIN_SRC="$FFBIN_CACHE"; FFBIN_FROM="static-ffmpeg (fetched into $FFBIN_CACHE)"
  fi
fi

if ! ffbin_pair_ok "$FFBIN_SRC"; then
  echo "[build] FATAL: no ffmpeg+ffprobe to bundle — refusing to ship an app" >&2
  echo "        that cannot decode a single frame on a Mac without Homebrew." >&2
  echo "        Fix by either:" >&2
  echo "          * staging a STATIC pair and pointing VAE_FFMPEG_DIR at it, or" >&2
  echo "          * putting them in $FFBIN_CACHE, or" >&2
  echo "          * making 'uv run --with static-ffmpeg==3.0' work (network access)." >&2
  exit 1
fi

rm -rf "$FFBIN_STAGE"
mkdir -p "$FFBIN_STAGE"
# Copy rather than --add-binary straight from the source: the staged cache
# copies are mode 0551 (read-only), and owning the mode here keeps signing and
# any later re-run from tripping over it.
cp -f "$FFBIN_SRC/ffmpeg" "$FFBIN_STAGE/ffmpeg"
cp -f "$FFBIN_SRC/ffprobe" "$FFBIN_STAGE/ffprobe"
chmod 755 "$FFBIN_STAGE/ffmpeg" "$FFBIN_STAGE/ffprobe"

# Provenance gate. These two binaries are fetched from a third party and then
# carry OUR Developer ID signature into a notarized DMG, so "it downloaded and
# it is portable" is not enough — otool proves portability, not provenance.
# Digests are pinned in packaging/ffmpeg-static.sha256; a mismatch is fatal.
FFBIN_SUMS="$ROOT/packaging/ffmpeg-static.sha256"
if [ -f "$FFBIN_SUMS" ]; then
  if ( cd "$FFBIN_STAGE" && grep -v '^#' "$FFBIN_SUMS" | shasum -a 256 -c --status ); then
    echo "[build] ffmpeg/ffprobe digests match packaging/ffmpeg-static.sha256"
  else
    echo "[build] FATAL: bundled ffmpeg/ffprobe do NOT match the pinned digests in" >&2
    echo "        packaging/ffmpeg-static.sha256. Refusing to sign and notarize" >&2
    echo "        binaries nobody has vetted." >&2
    ( cd "$FFBIN_STAGE" && shasum -a 256 ffmpeg ffprobe ) >&2
    echo "        If this change is intentional, re-verify the new build's licence" >&2
    echo "        config (ffmpeg -version) and update the pin file." >&2
    exit 1
  fi
else
  echo "[build] WARNING: $FFBIN_SUMS missing — bundling unverified binaries" >&2
fi

echo "[build] bundling ffmpeg/ffprobe from: $FFBIN_FROM"
for FFBIN in ffmpeg ffprobe; do
  echo "[build]   $FFBIN: $(file -b "$FFBIN_STAGE/$FFBIN")"
  # `|| true` inside the substitution: grep -v exits 1 when it filters
  # everything out, which is the GOOD case, and pipefail would call that a
  # failure.
  FFBIN_NONSYS="$(otool -L "$FFBIN_STAGE/$FFBIN" | tail -n +2 | awk '{print $1}' \
    | grep -v -e '^/usr/lib/' -e '^/System/' || true)"
  if [ -n "$FFBIN_NONSYS" ]; then
    echo "[build] FATAL: $FFBIN is not portable — it links non-system libraries" >&2
    echo "        that are NOT in the bundle, so it would fail in dyld on any" >&2
    echo "        machine but this one:" >&2
    printf '          %s\n' $FFBIN_NONSYS >&2
    exit 1
  fi
done

# The oldest macOS this bundle can honestly claim. The static ffmpeg above is
# built against the macOS 12 SDK (`otool -l | grep -A3 LC_BUILD_VERSION` ->
# minos 12.0), and arm64 macOS starts at 11.0 anyway, so 12.0 is the binding
# constraint. Stamped into Info.plist below so Launch Services refuses an
# older system with a clear message instead of a dyld crash.
MIN_MACOS="12.0"
FFBIN_MINOS="$(otool -l "$FFBIN_STAGE/ffmpeg" 2>/dev/null \
  | awk '$1=="minos"{print $2; exit}' || true)"
if [ -n "$FFBIN_MINOS" ]; then
  echo "[build]   bundled ffmpeg minos=$FFBIN_MINOS; Info.plist will claim $MIN_MACOS"
  if [ "$(printf '%s\n%s\n' "$FFBIN_MINOS" "$MIN_MACOS" | sort -V | tail -1)" != "$MIN_MACOS" ]; then
    echo "[build] WARNING: the bundled ffmpeg needs macOS $FFBIN_MINOS but the" >&2
    echo "        bundle claims $MIN_MACOS — raise MIN_MACOS in build_app.sh." >&2
  fi
fi

# --- faster-whisper IS bundled; captions are not an optional extra ------------
# This build used to list faster-whisper among the excludes below, so the DMG
# answered the Captions button with transcribe.py's "run the app from source
# (`uv sync --all-extras`)" — advice a DMG recipient cannot act on, on the one
# feature they are most likely to reach for. whisper.cpp was the documented
# escape hatch and is not a real one either: Homebrew's `whisper-cli` is a
# 43-byte wrapper around @rpath/libwhisper + libggml, so shipping it would mean
# dylib surgery AND a new ggml auto-downloader.
#
# It does NOT drag torch in: faster-whisper runs on ctranslate2 (already
# bundled for MADLAD translation), and the only torch reference on the path is
# ctranslate2/specs/model_spec.py's, which is inside a try/except ImportError.
# Verified by running the real transcribe() path with every module excluded
# below made unimportable — a full decode with word timestamps, zero leakage.
# The torch exclusion below therefore stays exactly as it was.
#
# The three added flags are each load-bearing, and none of them is implied by
# the import that pulls the package in:
#   --collect-data faster_whisper  Silero VAD ships as a DATA FILE inside the
#     package (faster_whisper/assets/silero_vad_v6.onnx). PyInstaller collects
#     the module graph but never a package's data, so without this the app
#     imports faster_whisper fine and dies inside `transcribe(vad_filter=True)`
#     with onnxruntime's NoSuchFile — the exact bug the Windows .spec's
#     collect_data_files('faster_whisper') exists to stop. transcribe.py's
#     _DECODE_MODES ladder degrades to vad_filter=False, so the cost is caption
#     QUALITY rather than the feature; this line is what keeps full quality.
#   --collect-submodules faster_whisper  every submodule is statically imported
#     from its __init__ today, so this is cheap (7 modules) insurance against
#     that changing upstream rather than a fix for anything.
#   --hidden-import onnxruntime  vad.py imports it INSIDE SileroVADModel.
#     __init__, and the hook that collects onnxruntime's provider dylibs only
#     runs for a package that is in the graph. onnxruntime is already in this
#     bundle via piper, so this costs nothing here and is not something to rely
#     on staying true.
# `av` (decode_audio), `tokenizers` and `huggingface_hub` (already collected
# above, for the model auto-download) come in as plain static imports.
uv run pyinstaller \
  --name "Video AI Editor" \
  --windowed \
  --noconfirm \
  --specpath "$SPEC_DIR" \
  --osx-bundle-identifier com.user.videoaieditor \
  --add-data "$ROOT/frontend/dist:frontend/dist" \
  --add-data "$ROOT/fonts:fonts" \
  --add-data "$ROOT/presets:presets" \
  --add-data "$ROOT/packaging/THIRD-PARTY-NOTICES.md:." \
  --add-data "$ROOT/VERSION:." \
  --add-data "$ROOT/BUILD_ID:." \
  --add-binary "$FFBIN_STAGE/ffmpeg:." \
  --add-binary "$FFBIN_STAGE/ffprobe:." \
  --hidden-import "uvicorn.lifespan.on" \
  --hidden-import "uvicorn.protocols.websockets.auto" \
  --hidden-import "uvicorn.loops.auto" \
  --hidden-import "uvicorn.protocols.http.auto" \
  --hidden-import "uvicorn.logging" \
  --hidden-import "video_ai_editor.main" \
  --collect-submodules video_ai_editor \
  --collect-submodules huggingface_hub \
  --collect-submodules faster_whisper \
  --hidden-import onnxruntime \
  --collect-data webview \
  --collect-data piper \
  --collect-data faster_whisper \
  --exclude-module torch \
  --exclude-module torchcodec \
  --exclude-module torchvision \
  --exclude-module mediapipe \
  --exclude-module demucs \
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

# --- prove the binaries actually landed --------------------------------------
# `--add-binary` is a request, not a receipt, and a bundle that quietly lost
# ffmpeg is indistinguishable from a working one until a user tries to import a
# video. PyInstaller 6 relocates BINARY entries into Contents/Frameworks and
# cross-links them into Contents/Resources (building/osx.py) — the two places
# platformutil.bundled_binary() looks — so anywhere else is a packaging change
# that has silently un-fixed this, and is fatal here rather than at runtime on
# someone's machine.
APP_DIR="dist/Video AI Editor.app"
for FFBIN in ffmpeg ffprobe; do
  FFBIN_IN_APP=""
  for FFBIN_CAND in "$APP_DIR/Contents/Frameworks/$FFBIN" \
                    "$APP_DIR/Contents/Resources/$FFBIN"; do
    if [ -f "$FFBIN_CAND" ]; then FFBIN_IN_APP="$FFBIN_CAND"; break; fi
  done
  if [ -z "$FFBIN_IN_APP" ]; then
    echo "[build] FATAL: $FFBIN is not in the bundle where the app looks for it." >&2
    echo "        Found instead:" >&2
    find "$APP_DIR" -maxdepth 4 -name "$FFBIN" -print >&2 || true
    echo "        If PyInstaller's layout moved, teach" >&2
    echo "        platformutil._bundle_payload_dirs() the new location too." >&2
    exit 1
  fi
  # BUNDLE already chmods BINARY entries to 0755; make it explicit anyway, so a
  # non-executable copy fails the BUILD rather than every ffmpeg call at runtime.
  chmod 755 "$FFBIN_IN_APP"
  [ -x "$FFBIN_IN_APP" ] || { echo "[build] FATAL: $FFBIN_IN_APP is not executable" >&2; exit 1; }
  echo "[build] bundled ${FFBIN_IN_APP#$APP_DIR/} ($(du -h "$FFBIN_IN_APP" | cut -f1))"
done

# piper's espeak-ng dictionaries (--collect-data piper). Without them
# `tts_voiceover` does not fail — espeak-ng calls exit(1) and takes the whole
# app process with it, and check_features reported TTS "available" the whole
# time. ai/features.py::_tts_ok now probes for this same file, so a bundle that
# loses it degrades to "unavailable" instead of crashing; this check keeps it
# from being lost in the first place.
# (Resources is where DATA lands; the Frameworks path is the cross-link, and is
# checked too so a layout flip warns about nothing.)
ESPEAK_TAB=""
for ESPEAK_CAND in "$APP_DIR/Contents/Resources/piper/espeak-ng-data/phontab" \
                   "$APP_DIR/Contents/Frameworks/piper/espeak-ng-data/phontab"; do
  if [ -f "$ESPEAK_CAND" ]; then ESPEAK_TAB="$ESPEAK_CAND"; break; fi
done
if [ -n "$ESPEAK_TAB" ]; then
  echo "[build] bundled piper espeak-ng-data ($(du -sh "$(dirname "$ESPEAK_TAB")" | cut -f1))"
else
  echo "[build] FATAL: piper espeak-ng-data missing from the bundle." >&2
  echo "        espeak-ng calls exit(1) when it cannot find phontab, which takes the" >&2
  echo "        WHOLE app process down — a user loses the editor by clicking a button." >&2
  echo "        Fatal, not a warning, for the same reason the ffmpeg gate is: a bundle" >&2
  echo "        that can hard-kill the app must not be shippable." >&2
  echo "        Fix: ensure --collect-data piper is passed and piper is importable." >&2
  exit 1
fi

# --- prove faster-whisper actually landed -------------------------------------
# Same rule as the ffmpeg gate: --collect-* is a REQUEST, not a receipt. A
# bundle that quietly lost faster-whisper is indistinguishable from a working
# one until a recipient presses Captions and is told to "run the app from
# source" — the very failure this build now exists to fix. Fatal, not a warning,
# because that message cannot be acted on from a DMG.
#
# faster_whisper is PURE PYTHON, so its code lives compressed in the PYZ
# archive inside the executable and leaves NO directory in the .app to look for
# (`strings` on the exe finds nothing either — verified). Three receipts, each
# covering what the others cannot:
#
# 1. Its DATA. Losing this costs caption quality silently (see _DECODE_MODES).
FW_VAD=""
for FW_CAND in "$APP_DIR/Contents/Resources/faster_whisper/assets/silero_vad_v6.onnx" \
               "$APP_DIR/Contents/Frameworks/faster_whisper/assets/silero_vad_v6.onnx"; do
  if [ -f "$FW_CAND" ]; then FW_VAD="$FW_CAND"; break; fi
done
if [ -z "$FW_VAD" ]; then
  echo "[build] FATAL: faster-whisper's Silero VAD asset is not in the bundle." >&2
  echo "        (DATA lands in Contents/Resources; Frameworks is the cross-link.)" >&2
  echo "        Without it every caption run falls back to vad_filter=False —" >&2
  echo "        transcription still works, but at reduced quality, silently." >&2
  echo "        Fix: ensure --collect-data faster_whisper is passed." >&2
  exit 1
fi
echo "[build] bundled faster_whisper assets ($(du -sh "$(dirname "$FW_VAD")" | cut -f1))"

# 2. Its NATIVE dependencies. `av` (faster_whisper.audio.decode_audio) and
#    `tokenizers` carry extension modules, so unlike faster_whisper itself they
#    DO leave a directory — and nothing else in this app imports either one, so
#    finding them is proof that faster-whisper's own import graph was walked
#    rather than just its data copied. Missing, the import fails at runtime even
#    though the Python code is present.
for FW_NATIVE in av tokenizers; do
  FW_NATIVE_DIR=""
  for FW_CAND in "$APP_DIR/Contents/Frameworks/$FW_NATIVE" \
                 "$APP_DIR/Contents/Resources/$FW_NATIVE"; do
    if [ -d "$FW_CAND" ]; then FW_NATIVE_DIR="$FW_CAND"; break; fi
  done
  if [ -z "$FW_NATIVE_DIR" ]; then
    echo "[build] FATAL: $FW_NATIVE is not in the bundle, so faster-whisper cannot" >&2
    echo "        import there — captions would fail at runtime with the Python" >&2
    echo "        code present and no clue why." >&2
    echo "        Check that faster-whisper has not returned to the exclude list." >&2
    exit 1
  fi
  echo "[build] bundled $FW_NATIVE ($(du -sh "$FW_NATIVE_DIR" | cut -f1))"
done

# 3. The module code itself, which neither of the above can show. PYZ-00.toc is
#    PyInstaller's OWN record of every module it put in the archive, written by
#    the run that just succeeded. A layout change that moves it is not a broken
#    bundle, so that case warns; a toc that exists and does NOT list the module
#    is exactly the silent loss this block is for.
PYZ_TOC="build/Video AI Editor/PYZ-00.toc"
if [ -f "$PYZ_TOC" ]; then
  if grep -q "'faster_whisper.transcribe'" "$PYZ_TOC"; then
    echo "[build] PYZ archive lists faster_whisper.transcribe"
  else
    echo "[build] FATAL: faster_whisper is NOT in the PYZ archive — the app would" >&2
    echo "        raise ImportError and tell the user to run from source, which is" >&2
    echo "        impossible from a DMG. Its data/native deps landed, so this is a" >&2
    echo "        module-graph problem: check the exclude list and the collect flags." >&2
    exit 1
  fi
else
  echo "[build] WARNING: $PYZ_TOC not found — cannot confirm the PYZ holds" >&2
  echo "        faster_whisper (PyInstaller's work-dir layout may have moved)." >&2
fi

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
  # State the architecture requirement instead of leaving it silent. This build
  # is arm64-ONLY (a universal2 build would need every wheel in the venv as a
  # fat binary); on an Intel Mac it simply cannot launch, and nothing told the
  # user so. LSMinimumSystemVersion is the honest half of that we can express in
  # the plist: it makes Launch Services refuse an older macOS with a readable
  # message rather than letting dyld fail, and $MIN_MACOS is the bundled
  # ffmpeg's own minimum (checked above). The architecture itself is announced
  # in the DMG's NAME and volume label — see build_dmg.sh.
  /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion $MIN_MACOS" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string $MIN_MACOS" "$PLIST"
  echo "[build] stamped Info.plist LSMinimumSystemVersion = $MIN_MACOS (arm64-only build)"
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
