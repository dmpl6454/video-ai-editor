# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_dynamic_libs

# Single source of truth for the app version — keeps the macOS Info.plist
# (Finder "Get Info", CFBundleShortVersionString) in lockstep with the VERSION
# file and the runtime /api/version endpoint.
with open('VERSION') as _vf:
    _APP_VERSION = _vf.read().strip() or '0.0.0'

datas = [('frontend/dist', 'frontend/dist'), ('fonts', 'fonts'), ('presets', 'presets'), ('VERSION', '.')]
# BUILD_ID is written by build_app.sh / build_win.ps1 just before packaging and
# is what config.build_id() reads inside a frozen app (no git there). Guarded so
# a bare `pyinstaller "Video AI Editor.spec"` on a tree without it still builds —
# the app then just reports an empty build string.
if os.path.exists('BUILD_ID'):
    datas += [('BUILD_ID', '.')]
hiddenimports = ['uvicorn.lifespan.on', 'uvicorn.protocols.websockets.auto', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.logging', 'video_ai_editor.main']
datas += collect_data_files('webview')
datas += collect_data_files('open_clip')
# faster-whisper ships the Silero VAD model as a DATA FILE inside its own
# package (faster_whisper/assets/silero_vad_v6.onnx), not as Python code.
# PyInstaller collects the module graph but never a package's data files unless
# asked — so the packaged app imported faster_whisper fine and then died inside
# `model.transcribe(..., vad_filter=True)` with onnxruntime's NoSuchFile. That
# type is neither ValueError nor RuntimeError, so main.py's dispatch mapping
# passed it through as a bare HTTP 500: "CC Captions → internal server error,
# no captions", reproducing ONLY in the packaged build. transcribe.py also
# degrades to vad_filter=False now, but that costs caption quality — this line
# is what keeps the shipped app at full quality.
datas += collect_data_files('faster_whisper')
hiddenimports += collect_submodules('video_ai_editor')
hiddenimports += collect_submodules('open_clip')
# ai/translate.py (MADLAD-400 via CTranslate2) replaced Argos here — Argos
# needed an explicit collect_submodules line for its own package name that
# used to sit right here, because it loaded itself via a STRING
# (`importlib.import_module("argostranslate")`), invisible to PyInstaller's
# static analysis. `ctranslate2` and `sentencepiece` turned out fine as plain
# static imports (verified: both directories land in the built `_internal`
# tree; `ctranslate2` was already proven safe via faster-whisper). But
# `huggingface_hub` did NOT — verified by diffing the built `dist/` tree
# (same method the nvidia-cublas fix below used), it was silently ABSENT
# despite `ai/translate.py` and `faster_whisper/utils.py` both doing a plain
# top-level `import huggingface_hub`. The likely cause: huggingface_hub's own
# `__init__.py` lazy-loads its submodules via `__getattr__` rather than
# eager imports, so `huggingface_hub.snapshot_download` is a runtime
# attribute-resolution PyInstaller's static AST walk cannot see — the same
# class of blind spot as Argos's string-based import, just a different
# mechanism producing it. This means **faster-whisper's own Whisper-model
# auto-download was ALSO silently broken in every previous packaged build**
# whenever a model wasn't already cached — a pre-existing gap this only
# surfaced because MADLAD's on-demand download exercises the identical call
# and was actually tested end-to-end in the frozen exe, which the
# already-cached-model dev/CI path never does.
hiddenimports += collect_submodules('huggingface_hub')

binaries = []
if sys.platform == "win32":
    # pywebview's EdgeChromium/WebView2 backend loads .NET via pythonnet ('clr').
    hiddenimports += ['clr']

    # nvidia-cublas-cu12 / nvidia-cudnn-cu12 (the `cuda` dependency GROUP, `uv
    # sync --group cuda`) are pure DLL-carrier packages — nothing in this repo
    # ever does `import nvidia.cublas`, since ingest.transcribe.
    # _add_cuda_dll_dirs() finds them by globbing site-packages/nvidia/*/bin at
    # RUNTIME. PyInstaller's static analysis has no import to trace that glob
    # back to, so without this the packaged exe silently lost the entire GPU
    # transcription path on the first build after that feature landed — caught
    # by diffing this build's `dist/` tree against where `ctranslate2.dll`
    # (a package we DO import, and which therefore WAS bundled automatically)
    # landed, and finding no `nvidia/` directory at all. Not a crash — the
    # `_get_model` fallback ladder degrades to CPU/int8 cleanly — but the
    # measured ~11x speedup (95.2s -> 8.4s for 60s of audio) would have quietly
    # vanished from every packaged build with nothing in the log to say so.
    # Guarded: a build on a machine that skipped `--group cuda` must still
    # succeed, just without the GPU path (exactly like a build with no ffmpeg
    # on PATH still succeeds — this app never hard-fails on a missing
    # accelerator).
    for _cuda_pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            binaries += collect_dynamic_libs(_cuda_pkg)
        except Exception:
            pass


a = Analysis(
    ['src/video_ai_editor/desktop.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['mediapipe', 'demucs', 'pyannote', 'librosa', 'matplotlib', 'tkinter', 'pandas', 'sklearn', 'rembg', 'simple_lama_inpainting', 'noisereduce', 'transformers'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Video AI Editor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Video AI Editor',
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name='Video AI Editor.app',
        icon=None,
        bundle_identifier='com.user.videoaieditor',
        version=_APP_VERSION,
        info_plist={
            'CFBundleShortVersionString': _APP_VERSION,
            'CFBundleVersion': _APP_VERSION,
            'NSHighResolutionCapable': True,
        },
    )
