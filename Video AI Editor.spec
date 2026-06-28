# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = [('frontend/dist', 'frontend/dist'), ('fonts', 'fonts'), ('presets', 'presets')]
hiddenimports = ['uvicorn.lifespan.on', 'uvicorn.protocols.websockets.auto', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.logging', 'video_ai_editor.main']
datas += collect_data_files('webview')
datas += collect_data_files('open_clip')
hiddenimports += collect_submodules('video_ai_editor')
hiddenimports += collect_submodules('open_clip')


a = Analysis(
    ['src/video_ai_editor/desktop.py'],
    pathex=[],
    binaries=[],
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
app = BUNDLE(
    coll,
    name='Video AI Editor.app',
    icon=None,
    bundle_identifier='com.user.videoaieditor',
)
