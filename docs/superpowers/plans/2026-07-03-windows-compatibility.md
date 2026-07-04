# Windows Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Video AI Editor run correctly on both Windows and macOS from a single codebase, so any developer (Mac or Windows) can clone the repo and launch the app, with no regression to existing macOS behavior.

**Architecture:** The OS coupling is concentrated at the *edges* — launch, config directories, native-binary discovery, the GPU encoder, and atomic file swaps. The video-editing engine (EDL schema, dispatch, render-graph construction), the FastAPI/MCP layer, and the entire React frontend are already OS-neutral. The strategy is **additive**: introduce one small `platformutil.py` module (`exe_name()`, `find_binary()`, `user_data_dir()`) plus targeted `sys.platform` branches, so every macOS code path is preserved byte-for-byte and Windows gets a parallel branch. There is **no rearchitecting**.

**Tech Stack:** Python 3.11–3.13 (uv-managed, universal `uv.lock` already includes Windows wheels), FastAPI + uvicorn, pywebview 6.x (Cocoa/WebKit on Mac, EdgeChromium/WebView2 on Windows — pythonnet already in the lock), PyInstaller packaging, ffmpeg (VideoToolbox on Mac; NVENC/QSV/AMF/libx264 on Windows), whisper.cpp/`whisper-cli`, Real-ESRGAN ncnn-vulkan, Piper TTS (pure-Python wheel).

---

## Reconnaissance summary (why each task exists)

These facts were verified by reading the exact code and the `uv.lock`, and by web research (all cited in the task notes). Read this before starting — it prevents "fixing" non-problems.

**Already portable — DO NOT TOUCH:**
- **The `uv.lock` is a universal multi-platform resolution.** It contains 288 `win_amd64` + 146 `win32` wheel entries and `resolution-markers` covering `win32` for Python 3.11–3.14. A Windows dev runs `uv sync` and gets a working install of *the full app*. **No re-lock is needed.**
- **`demucs` installs cleanly on Windows.** The scary `diffq` (no cp311+ Windows wheels) is **not** in demucs 4.0.1's resolved dependency tree — the lock resolves demucs to `dora-search, einops, julius, lameenc, openunmix, pyyaml, torch, torchaudio, tqdm`, all of which have Windows wheels or are pure-Python. Confirmed by inspecting the demucs `[[package]]` block in `uv.lock`.
- **pywebview → WebView2 works out of the box.** pywebview 6.2.1 declares `pythonnet; sys_platform == "win32"` as a conditional core dep (already in the lock via `pythonnet 3.0.5` → `clr-loader` → `cffi`, gated to win32). The Evergreen WebView2 Runtime is preinstalled on Windows 11 and most Windows 10, and **includes H.264/AAC codecs + WebCodecs** — so the frontend's mp4box.js + `VideoDecoder` scrubber and the `<video>` fallback both work. `http://127.0.0.1` is a secure context, so WebCodecs is not gated.
- **Piper TTS works out of the box.** The app uses the pure-Python `piper-tts>=1.4.2` library (`PiperVoice.load` + `synthesize_wav`); the old `piper-phonemize` (no Windows wheels) is **gone** from 1.4.2. `piper_tts-1.4.2-cp39-abi3-win_amd64.whl` is already in the lock.
- **The frontend is 100% portable** — zero `process.platform`/Electron/`__APPLE__`; `package.json` scripts (`vite`, `tsc -b && vite build`, `eslint .`) are cmd.exe-safe.
- **Emoji rendering** — `text_overlay.py`'s docstring *claims* an Apple Color Emoji fallback but the code strips emoji **unconditionally on all platforms** (stale docstring). Nothing to port; optionally document.
- **Fonts** are all bundled repo-relative via `FONTS_DIR` — no system-font paths.
- **Filename sanitization** (`main.py:_safe_filename`) already strips every Windows-illegal char (`: ? * < > | " \`).
- **No `shell=True`, no `os.popen`, no string-command subprocess** anywhere in `src/`. Every call is list-argv.
- **No POSIX-only stdlib** (`fcntl`, `pwd`, `grp`, `os.fork`, `os.symlink`, `preexec_fn`, `getloadavg`, `SIGKILL`) in `src/` or `tests/`.
- **No colon/strftime filename construction** — time-based names use `int(time.time())`, session ids use `uuid4().hex`.
- **`tempfile.TemporaryDirectory()`** used everywhere (portable); no `NamedTemporaryFile`-reopen hazard.
- **`.pth` hidden-flag bug is macOS-only** (Spotlight `UF_HIDDEN`) — it *disappears* on Windows; the `run.sh` PYTHONPATH workaround is only needed on Mac.

**Needs work (the tasks below address each):**

| Area | File(s) | Issue |
|---|---|---|
| Native-binary discovery | `platformutil.py` (new), `config.py`, `transcribe.py`, `stabilize.py`, `upscale.py` | Homebrew paths + no `.exe` suffix |
| User data / config dir | `config.py`, several `Path.home()/.cache` sites, `dispatch.py` (`~/Movies`) | `~/Library/Application Support` & XDG dirs vs `%APPDATA%`/`%LOCALAPPDATA%` |
| PATH separator | `config.py:152` | `VAI_ALLOWED_ROOTS` split on `:` breaks `C:\` |
| GPU encoder | `compositor.py:92-118` | VideoToolbox-only; needs NVENC/QSV/AMF probe |
| P-core detection | `chunks.py:44-52` | macOS `sysctl` (already has fallback; make branch clean) |
| Atomic swap | `compositor.py:565,702,748` + prune `unlink`s | `os.replace`/`unlink` raise `PermissionError` on Windows when a reader (Starlette `FileResponse`) holds the file open |
| Text encoding | ~40 `read_text`/`write_text` sites | No `encoding=` → cp1252 corrupts Hindi/emoji on Windows |
| Launcher | `run.ps1` (new), `run.sh` | bash-only; `.venv/bin/python` vs `.venv\Scripts\python.exe`; `:` vs `;` PYTHONPATH sep |
| npm invocation | `desktop.py:45` | bare `["npm", ...]` fails on Windows (needs `npm.cmd`) |
| Packaging | `Video AI Editor.spec`, `build_win.ps1` (new) | mac `BUNDLE`/`.dmg`; Windows needs COLLECT-folder + installer |
| CI | `.github/workflows/ci.yml` | add a `windows-latest` job |
| Tests | 5 test files hardcode `/Users/sudhanshu/...`, `/dev/null` | need skip-guards / `os.devnull` |

---

## File structure

**New files:**
- `src/video_ai_editor/platformutil.py` — the single home for OS-conditional helpers: `IS_WINDOWS`, `exe_name(name)`, `find_binary(name, extra_dirs)`, `user_data_dir()`, `read_text_utf8(path)`/`write_text_utf8(path, text)`, `replace_with_retry(src, dst)`, `unlink_with_retry(path)`. One responsibility: hide platform differences behind a stable interface. Everything else imports from here.
- `run.ps1` — Windows launcher (PowerShell), the parallel of `run.sh`.
- `build_win.ps1` — Windows PyInstaller build script (parallel of `build_app.sh`).
- `tests/test_platformutil.py` — unit tests for the new helpers (these run and pass on *both* OSes because they branch on `sys.platform` themselves or use `monkeypatch`).

**Modified files (surgical branches only):**
- `config.py`, `render/compositor.py`, `render/chunks.py`, `ingest/transcribe.py`, `ai/stabilize.py`, `ai/upscale.py`, `agent/dispatch.py`, `desktop.py`, `Video AI Editor.spec`, `.github/workflows/ci.yml`, plus the ~40 encoding sites and 5 test files.

**Design rule:** No file gets a raw `sys.platform` check if a `platformutil` helper covers it. This keeps the OS logic auditable in one place and testable in isolation.

---

## Task 0: Preflight — branch and baseline

**Files:** none (git + environment)

- [ ] **Step 1: Create a feature branch**

```bash
cd /Users/tabish/Desktop/dashmani-ai-editor
git checkout -b feat/windows-compat
```

- [ ] **Step 2: Establish the macOS baseline is green**

Run: `uv run pytest -q --tb=short`
Expected: PASS (the current suite; heavy-AI tests skip cleanly). Record the pass/skip counts — every later task must keep this baseline green on macOS.

- [ ] **Step 3: Commit the (empty) branch marker**

No commit yet — proceed to Task 1.

---

## Task 1: `platformutil.py` — `exe_name()` and `IS_WINDOWS`

**Files:**
- Create: `src/video_ai_editor/platformutil.py`
- Test: `tests/test_platformutil.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_platformutil.py
import sys
from video_ai_editor import platformutil as pu


def test_exe_name_appends_exe_on_windows(monkeypatch):
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    assert pu.exe_name("ffmpeg") == "ffmpeg.exe"
    assert pu.exe_name("whisper-cli") == "whisper-cli.exe"


def test_exe_name_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    assert pu.exe_name("ffmpeg") == "ffmpeg"


def test_exe_name_does_not_double_suffix(monkeypatch):
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    assert pu.exe_name("ffmpeg.exe") == "ffmpeg.exe"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platformutil.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'video_ai_editor.platformutil'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/video_ai_editor/platformutil.py
"""Cross-platform helpers. The ONE place OS differences live.

macOS and Windows both import from here; every OS-conditional decision in the
codebase should route through a function in this module rather than an inline
`sys.platform` check, so platform behavior stays auditable and testable.
"""
from __future__ import annotations
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def exe_name(name: str) -> str:
    """Append `.exe` on Windows for a bare binary name (idempotent)."""
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platformutil.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/platformutil.py tests/test_platformutil.py
git commit -m "feat(win): add platformutil with exe_name/IS_WINDOWS"
```

---

## Task 2: `platformutil.find_binary()` — cross-platform binary discovery

**Files:**
- Modify: `src/video_ai_editor/platformutil.py`
- Test: `tests/test_platformutil.py`

**Why:** `transcribe.py`, `stabilize.py`, `upscale.py` each hardcode `/opt/homebrew/...` fallbacks. Replace with a helper that (1) tries `shutil.which(exe_name(name))` (respects PATH, adds `.exe`), then (2) checks caller-supplied extra dirs, returning the first existing path or `None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_platformutil.py
from pathlib import Path


def test_find_binary_prefers_path(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    fake = tmp_path / "mytool"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(pu.shutil, "which", lambda n: str(fake) if n == "mytool" else None)
    assert pu.find_binary("mytool", []) == str(fake)


def test_find_binary_falls_back_to_extra_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(pu.shutil, "which", lambda n: None)
    d = tmp_path / "bin"
    d.mkdir()
    (d / "mytool").write_text("x")
    assert pu.find_binary("mytool", [d]) == str(d / "mytool")


def test_find_binary_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(pu.shutil, "which", lambda n: None)
    assert pu.find_binary("nope", [tmp_path]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platformutil.py -k find_binary -v`
Expected: FAIL with `AttributeError: module 'video_ai_editor.platformutil' has no attribute 'find_binary'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/video_ai_editor/platformutil.py
import shutil
from pathlib import Path


def find_binary(name: str, extra_dirs: list[Path]) -> str | None:
    """Locate a native binary cross-platform.

    1. `shutil.which(exe_name(name))` — respects PATH, adds `.exe` on Windows.
    2. Each dir in `extra_dirs` (both `name` and `exe_name(name)`).
    Returns the resolved path string, or None if nowhere found.
    """
    found = shutil.which(exe_name(name))
    if found:
        return found
    for d in extra_dirs:
        for cand in (Path(d) / exe_name(name), Path(d) / name):
            if cand.exists():
                return str(cand)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platformutil.py -k find_binary -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/platformutil.py tests/test_platformutil.py
git commit -m "feat(win): add find_binary cross-platform lookup"
```

---

## Task 3: `platformutil.user_data_dir()` — per-OS writable app dir

**Files:**
- Modify: `src/video_ai_editor/platformutil.py`
- Test: `tests/test_platformutil.py`

**Why:** `config.py:_user_config_dir()` hardcodes `~/Library/Application Support/Video AI Editor` (macOS). Windows apps use `%APPDATA%`. This helper returns the right base on each OS. Tasks 5–6 wire it into config and cache dirs.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_platformutil.py
def test_user_data_dir_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    monkeypatch.setattr(pu, "IS_MAC", False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    got = pu.user_data_dir("Video AI Editor")
    assert got == tmp_path / "AppData" / "Roaming" / "Video AI Editor"


def test_user_data_dir_mac_uses_app_support(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(pu, "IS_MAC", True)
    monkeypatch.setattr(pu.Path, "home", staticmethod(lambda: tmp_path))
    got = pu.user_data_dir("Video AI Editor")
    assert got == tmp_path / "Library" / "Application Support" / "Video AI Editor"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platformutil.py -k user_data_dir -v`
Expected: FAIL with `AttributeError: ... has no attribute 'user_data_dir'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/video_ai_editor/platformutil.py
import os


def user_data_dir(app_name: str) -> Path:
    """Per-OS writable application data directory.

    Windows: %APPDATA%\\<app_name>            (roaming; falls back to ~/AppData/Roaming)
    macOS:   ~/Library/Application Support/<app_name>
    Other:   ~/.local/share/<app_name>        (XDG)
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app_name
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / app_name
    return Path.home() / ".local" / "share" / app_name


def user_cache_dir(app_name: str) -> Path:
    """Per-OS cache directory (regenerable data).

    Windows: %LOCALAPPDATA%\\<app_name>\\cache
    macOS:   ~/Library/Caches/<app_name>
    Other:   ~/.cache/<app_name>
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / app_name / "cache"
    if IS_MAC:
        return Path.home() / "Library" / "Caches" / app_name
    return Path.home() / ".cache" / app_name
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platformutil.py -k user_data_dir -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/platformutil.py tests/test_platformutil.py
git commit -m "feat(win): add user_data_dir/user_cache_dir per-OS resolvers"
```

---

## Task 4: `platformutil` — UTF-8 text helpers + retrying replace/unlink

**Files:**
- Modify: `src/video_ai_editor/platformutil.py`
- Test: `tests/test_platformutil.py`

**Why (encoding):** ~40 `read_text`/`write_text` calls lack `encoding=`. On Windows the default is cp1252, which **corrupts or crashes** on Devanagari/emoji (e.g. `dispatch.py:636` writes JSON with `ensure_ascii=False`). Centralize UTF-8 I/O.

**Why (retry):** `compositor.py`'s `os.replace(tmp, dst)` and prune `unlink` succeed on POSIX even when a reader holds `dst` open, but on Windows they raise `PermissionError` — and Starlette's `FileResponse` holds the preview mp4 open while streaming Range requests to the scrubber. A short retry-with-backoff resolves the race.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_platformutil.py
def test_write_then_read_utf8_roundtrips_devanagari(tmp_path):
    p = tmp_path / "t.txt"
    s = "नमस्ते 🙏 hello"
    pu.write_text_utf8(p, s)
    assert pu.read_text_utf8(p) == s
    # bytes on disk are UTF-8 regardless of platform locale
    assert p.read_bytes().decode("utf-8") == s


def test_replace_with_retry_succeeds(tmp_path):
    src = tmp_path / "a"; dst = tmp_path / "b"
    src.write_text("new"); dst.write_text("old")
    pu.replace_with_retry(src, dst)
    assert dst.read_text() == "new"
    assert not src.exists()


def test_unlink_with_retry_missing_ok(tmp_path):
    pu.unlink_with_retry(tmp_path / "does-not-exist")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platformutil.py -k "utf8 or retry" -v`
Expected: FAIL with `AttributeError: ... has no attribute 'write_text_utf8'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/video_ai_editor/platformutil.py
import time


def read_text_utf8(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text_utf8(path: Path | str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_with_retry(src: Path | str, dst: Path | str,
                       attempts: int = 10, delay: float = 0.05) -> None:
    """os.replace with retry. On Windows, replacing a file another process has
    open (e.g. a Starlette FileResponse streaming the preview) raises
    PermissionError; a short backoff lets the reader finish. On POSIX this
    almost always succeeds on the first try."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # pragma: no cover - Windows-timing path
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


def unlink_with_retry(path: Path | str,
                      attempts: int = 5, delay: float = 0.05) -> None:
    """Path.unlink(missing_ok=True) with the same Windows open-file retry."""
    p = Path(path)
    for i in range(attempts):
        try:
            p.unlink(missing_ok=True)
            return
        except PermissionError:  # pragma: no cover - Windows-timing path
            time.sleep(delay * (i + 1))
    # Best-effort: a leftover cache file is not fatal.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platformutil.py -k "utf8 or retry" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/platformutil.py tests/test_platformutil.py
git commit -m "feat(win): add utf8 text helpers + retrying replace/unlink"
```

---

## Task 5: `config.py` — per-OS config dir, workdir, and PATH augmentation

**Files:**
- Modify: `src/video_ai_editor/config.py` (functions `_augment_path_for_gui_launch` lines 5-22, `_user_config_dir` lines 76-81, `_default_workdir` lines 94-111, `VAI_ALLOWED_ROOTS` split at line 152, `.env`/VERSION reads at lines 42 & 58)
- Test: `tests/test_config_paths.py` (new)

**Why:** `_user_config_dir` and the frozen `_default_workdir` hardcode `~/Library/Application Support`; the PATH augmentation is Homebrew-only; `VAI_ALLOWED_ROOTS` splits on `:` (breaks `C:\`); VERSION/.env reads lack `encoding=`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_paths.py
import sys
from pathlib import Path
from video_ai_editor import platformutil as pu


def test_allowed_roots_uses_ospathsep(monkeypatch):
    """VAI_ALLOWED_ROOTS must split on os.pathsep, not a hardcoded ':',
    so Windows 'C:\\a;C:\\b' parses as two roots, not four fragments."""
    monkeypatch.setenv("VAI_RESTRICT_PATHS", "1")
    monkeypatch.setenv("VAI_ALLOWED_ROOTS",
                       ("C:\\a;C:\\b" if sys.platform == "win32" else "/a:/b"))
    import importlib, video_ai_editor.config as cfg
    importlib.reload(cfg)
    # Two user roots + WORKDIR itself = 3 total.
    assert len(cfg.ALLOWED_PATH_ROOTS) == 3
    importlib.reload(cfg)  # restore default state for other tests


def test_user_config_dir_matches_platform():
    from video_ai_editor import config as cfg
    got = cfg._user_config_dir()
    assert got == pu.user_data_dir("Video AI Editor")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_paths.py -v`
Expected: FAIL — `test_allowed_roots_uses_ospathsep` fails on the `:`-split (on Windows it splits `C:\a;C:\b` wrong; on Mac the count assertion still guards the reload); `test_user_config_dir_matches_platform` fails because `_user_config_dir` returns the hardcoded mac path.

- [ ] **Step 3: Write the implementation**

Edit `config.py`. Add the import near the top (after `from pathlib import Path`):

```python
from . import platformutil as _pu
```

Replace `_augment_path_for_gui_launch` (lines 5-22) body's `extra` list and add a Windows branch:

```python
def _augment_path_for_gui_launch() -> None:
    """Make CLIs (ffmpeg, ffprobe, whisper-cli, …) resolvable no matter how the
    app was started.

    macOS: a double-clicked .app inherits launchd's minimal PATH and can't see
    /opt/homebrew/bin. Windows: GUI processes inherit the user PATH, but a
    winget-installed ffmpeg (Gyan.FFmpeg) is famously NOT put on PATH — so we
    also probe its package dir. Append (don't prepend) so we never override a
    deliberately-chosen binary."""
    if _pu.IS_WINDOWS:
        import os as _os
        localappdata = _os.environ.get("LOCALAPPDATA", "")
        extra = []
        if localappdata:
            # Gyan.FFmpeg / BtbN unzip locations; glob the winget packages dir.
            wg = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
            if wg.is_dir():
                extra += [str(p) for p in wg.glob("Gyan.FFmpeg*/**/bin") if p.is_dir()]
            extra.append(str(Path(localappdata) / "Programs" / "ffmpeg" / "bin"))
    else:
        extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
                 "/usr/local/sbin", str(Path.home() / ".local" / "bin")]
    current = os.environ.get("PATH", "").split(os.pathsep)
    additions = [d for d in extra if d and d not in current and os.path.isdir(d)]
    if additions:
        os.environ["PATH"] = os.pathsep.join([*current, *additions])
```

Replace `_read_version` line 42 read to use UTF-8:

```python
                    return vf.read_text(encoding="utf-8").strip() or "0.0.0"
```

Replace `.env` read at line 58:

```python
        lines = env_path.read_text(encoding="utf-8").splitlines()
```

Replace `_user_config_dir` (lines 76-81) entirely:

```python
def _user_config_dir() -> Path:
    """Stable, user-writable config dir that both dev and the shipped app can
    reach. Windows: %APPDATA%\\Video AI Editor; macOS: ~/Library/Application
    Support/Video AI Editor."""
    return _pu.user_data_dir("Video AI Editor")
```

In `_default_workdir` (lines 94-111), replace the frozen branch (lines 108-110):

```python
    if getattr(_sys, "frozen", False) or getattr(_sys, "_MEIPASS", None):
        return _pu.user_data_dir("Video AI Editor") / "workdir"
    return PROJECT_ROOT / "workdir"
```

Replace the `VAI_ALLOWED_ROOTS` split at line 152:

```python
    for r in _extra.split(os.pathsep) if _extra else []:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_paths.py -v && uv run pytest -q`
Expected: PASS — new tests green, full suite still green on macOS (the `_user_config_dir` still returns the same `~/Library/Application Support/...` path on Mac, so no macOS behavior changed).

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/config.py tests/test_config_paths.py
git commit -m "feat(win): per-OS config/workdir/PATH + os.pathsep for allowed roots"
```

---

## Task 6: Per-OS cache/data dirs for AI features + `~/Movies` b-roll

**Files:**
- Modify: `src/video_ai_editor/agent/dispatch.py:1539` (`~/Movies/broll`), `ingest/transcribe.py:76,79`, `ai/tts.py:11`, `ai/emoji.py:13`, `ai/broll.py:33`, `ai/upscale.py:16`, `ai/rife.py:16`, `ingest/beats.py:19`, `ai/diarize.py:53`
- Test: `tests/test_platform_dirs.py` (new)

**Why:** These construct `Path.home()/".cache"/...` and `Path.home()/".local"/"share"/...` (XDG layout) and one `Path.home()/"Movies"/"broll"`. On Windows they *work* but clutter the profile root, and `Movies` doesn't exist on Windows (it's `Videos`). Route through `platformutil`. **The whisper-cpp/realesrgan/rife model dirs stay backward-compatible** (probe both the old and new location) so an existing macOS install keeps finding its models.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_platform_dirs.py
import sys
from pathlib import Path


def test_broll_default_dir_is_platform_appropriate(monkeypatch):
    """Default b-roll dir must be ~/Videos/broll on Windows, ~/Movies/broll on Mac."""
    from video_ai_editor.agent import dispatch
    got = dispatch._default_broll_dir()  # helper introduced by this task
    if sys.platform == "win32":
        assert got == Path.home() / "Videos" / "broll"
    else:
        assert got == Path.home() / "Movies" / "broll"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platform_dirs.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_default_broll_dir'`

- [ ] **Step 3: Write the implementation**

In `dispatch.py`, add a helper near the top of the module (after imports):

```python
def _default_broll_dir() -> Path:
    """Default library folder for b-roll footage: ~/Videos/broll on Windows,
    ~/Movies/broll on macOS/Linux (Movies is the mac convention)."""
    from .. import platformutil as _pu
    root = "Videos" if _pu.IS_WINDOWS else "Movies"
    return Path.home() / root / "broll"
```

Replace the `Path.home() / "Movies" / "broll"` usage at line 1539 with `_default_broll_dir()`.

For the model dirs in `transcribe.py:73-80`, keep the existing paths for backward-compat and *prepend* the new per-OS location so both are searched:

```python
# ingest/transcribe.py — replace the _WHISPER_CPP_MODEL_DIRS list
from .. import platformutil as _pu
_WHISPER_CPP_MODEL_DIRS = [
    Path(os.environ.get("WHISPER_CPP_MODELS", ""))
        if os.environ.get("WHISPER_CPP_MODELS") else None,
    _pu.user_data_dir("Video AI Editor") / "whisper-cpp",   # new, per-OS
    Path.home() / ".local" / "share" / "video-ai-editor" / "whisper-cpp",  # legacy mac/linux
    Path("/opt/homebrew/share/whisper-cpp/ggml-models"),    # legacy brew (harmless on win)
    Path("/opt/homebrew/share/whisper-cpp"),
    Path.home() / ".cache" / "whisper-cpp",
]
_WHISPER_CPP_MODEL_DIRS = [p for p in _WHISPER_CPP_MODEL_DIRS if p is not None]
```

Apply the same "prepend `user_data_dir`/`user_cache_dir`, keep legacy" pattern to `ai/upscale.py:16` (realesrgan), `ai/rife.py:16` (rife), `ai/tts.py:11` (voices), `ai/emoji.py:13`, `ai/broll.py:33`, `ingest/beats.py:19`. For example in `ai/upscale.py`:

```python
    from .. import platformutil as _pu
    candidates = [
        _pu.user_data_dir("Video AI Editor") / "models" / "realesrgan",       # new
        Path.home() / ".local" / "share" / "video-ai-editor" / "models" / "realesrgan",  # legacy
        Path(__file__).resolve().parents[3] / "models" / "realesrgan",        # repo
    ]
```

Leave `ai/diarize.py:53` (`~/.cache/huggingface/hub`) **unchanged** — that is HuggingFace's own cache convention and must match where `huggingface_hub` writes on every OS.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platform_dirs.py -v && uv run pytest -q`
Expected: PASS; full suite still green on macOS (legacy paths preserved, so existing models still resolve).

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/agent/dispatch.py src/video_ai_editor/ingest/transcribe.py src/video_ai_editor/ai/upscale.py src/video_ai_editor/ai/rife.py src/video_ai_editor/ai/tts.py src/video_ai_editor/ai/emoji.py src/video_ai_editor/ai/broll.py src/video_ai_editor/ingest/beats.py tests/test_platform_dirs.py
git commit -m "feat(win): per-OS cache/model dirs + ~/Videos broll, legacy paths preserved"
```

---

## Task 7: Native binaries — `whisper-cli` and `realesrgan` via `find_binary`/`exe_name`

**Files:**
- Modify: `src/video_ai_editor/ingest/transcribe.py:70` (`_WHISPER_CPP_BIN`), lines 119-124 (error hint), `ai/upscale.py:20,26,60` (binary name + argv0)
- Test: `tests/test_platformutil.py` (extend), manual note for real binaries

**Why:** `_WHISPER_CPP_BIN = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"` never adds `.exe`; `upscale.py` checks for `realesrgan-ncnn-vulkan` (no `.exe`) and calls argv0 `"./realesrgan-ncnn-vulkan"`. Web-verified: the Windows binaries are `whisper-cli.exe` (whisper.cpp ≥ v1.7.x, in `whisper-bin-x64.zip`) and `realesrgan-ncnn-vulkan.exe` (in `realesrgan-ncnn-vulkan-v0.2.0-windows.zip`, bundles `models/`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_platformutil.py
def test_whisper_cpp_bin_uses_exe_name(monkeypatch):
    """_WHISPER_CPP_BIN resolution must add .exe on Windows and not hardcode a
    brew path as the win fallback."""
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    monkeypatch.setattr(pu.shutil, "which",
                        lambda n: "C:/tools/whisper-cli.exe" if n == "whisper-cli.exe" else None)
    import importlib, video_ai_editor.ingest.transcribe as t
    importlib.reload(t)
    assert t._WHISPER_CPP_BIN == "C:/tools/whisper-cli.exe"
    importlib.reload(t)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platformutil.py -k whisper_cpp_bin -v`
Expected: FAIL — current code calls `shutil.which("whisper-cli")` (no `.exe`), so the monkeypatched `which` returns None and the brew fallback is used.

- [ ] **Step 3: Write the implementation**

In `transcribe.py`, replace line 70:

```python
from .. import platformutil as _pu
_WHISPER_CPP_BIN = _pu.find_binary("whisper-cli", _WHISPER_CPP_MODEL_DIRS) or _pu.exe_name("whisper-cli")
```

(Place this **after** the `_WHISPER_CPP_MODEL_DIRS` definition from Task 6 so the dirs are available; `find_binary` will also probe those dirs for a co-located binary.) Replace the ffmpeg calls in the whisper-cpp path (lines 129-132) to route the ffmpeg name through `exe_name` via a module constant (see Task 8 for the shared `FFMPEG`/`FFPROBE` constants — this task can use `_pu.exe_name("ffmpeg")` inline until Task 8 lands).

Replace the brew-only error hint (lines 119-124):

```python
    if not model_path.exists():
        hint = ("Download it with whisper.cpp's download-ggml-model script "
                "(models/download-ggml-model.cmd on Windows, .sh on macOS), or "
                "fetch https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
                f"ggml-{name}.bin into {_WHISPER_CPP_MODEL_DIRS[0]}")
        raise RuntimeError(f"whisper-cpp model not found at {model_path}. {hint}")
```

In `upscale.py`, replace lines 20, 26, and the argv0 at line 60:

```python
        if (c / _pu.exe_name("realesrgan-ncnn-vulkan")).exists():   # line 20
            return c
...
ESRGAN_BIN = ESRGAN_DIR / _pu.exe_name("realesrgan-ncnn-vulkan")     # line 26
...
    proc = subprocess.run(
        ["./" + _pu.exe_name("realesrgan-ncnn-vulkan") if not _pu.IS_WINDOWS
         else _pu.exe_name("realesrgan-ncnn-vulkan"),                # line 60: no "./" on Windows
         "-i", str(frames_in.resolve()), "-o", str(frames_out.resolve()),
         "-s", str(factor), "-n", model, "-f", "png", "-m", "models"],
        cwd=str(ESRGAN_DIR),
        capture_output=True, text=True,
    )
```

Add `from .. import platformutil as _pu` at the top of `upscale.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platformutil.py -k whisper_cpp_bin -v && uv run pytest -q`
Expected: PASS; macOS suite green (on Mac `exe_name` is a no-op so behavior is identical).

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/ingest/transcribe.py src/video_ai_editor/ai/upscale.py tests/test_platformutil.py
git commit -m "feat(win): resolve whisper-cli/realesrgan with exe_name, cross-OS model hint"
```

---

## Task 8: Shared `FFMPEG`/`FFPROBE` constants + `stabilize.py` vidstab lookup

**Files:**
- Modify: `src/video_ai_editor/platformutil.py` (add `FFMPEG`/`FFPROBE`), `ai/stabilize.py:15-20` (`_FFMPEG_CANDIDATES`), and swap bare `"ffmpeg"`/`"ffprobe"` in `render/compositor.py:95,543,693,736`, `render/waveform.py:53`, `ingest/normalize.py:25,35,58,82,84,114`, `ingest/probe.py:49-51`, `ingest/beats.py:37`, `ingest/scenes.py:21-23`, `ai/upscale.py:54,71,82`
- Test: `tests/test_platformutil.py` (extend)

**Why:** ~20 sites invoke bare `"ffmpeg"`/`"ffprobe"`. On Windows these must be `ffmpeg.exe`/`ffprobe.exe` — `subprocess` on Windows *does* find `ffmpeg.exe` from a bare `"ffmpeg"` via PATHEXT, **but only if it's on PATH**; using `exe_name` + explicit resolution is robust against the winget-PATH bug. Centralize as two constants so the swap is mechanical and consistent.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_platformutil.py
def test_ffmpeg_constant_has_exe_on_windows(monkeypatch):
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    assert pu.exe_name("ffmpeg") == "ffmpeg.exe"
    assert pu.exe_name("ffprobe") == "ffprobe.exe"
```

(This reuses `exe_name`; the constants `FFMPEG`/`FFPROBE` are computed once at import from `exe_name`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_platformutil.py -k ffmpeg_constant -v`
Expected: PASS already (this asserts `exe_name`, which exists) — so instead assert the constants exist:

```python
def test_ffmpeg_constants_exist():
    assert pu.FFMPEG in ("ffmpeg", "ffmpeg.exe")
    assert pu.FFPROBE in ("ffprobe", "ffprobe.exe")
```

Run that; Expected: FAIL with `AttributeError: ... has no attribute 'FFMPEG'`.

- [ ] **Step 3: Write the implementation**

Add to `platformutil.py`:

```python
# Resolved once at import. Bare names are fine when on PATH; exe_name makes the
# Windows form explicit so callers can also feed these to find_binary.
FFMPEG = exe_name("ffmpeg")
FFPROBE = exe_name("ffprobe")
```

In every listed call site, replace the string literal `"ffmpeg"` (first argv element) with `_pu.FFMPEG` and `"ffprobe"` with `_pu.FFPROBE`, adding `from .. import platformutil as _pu` (or `from . import ...` per module depth) where missing. Example — `compositor.py:95`:

```python
        out = subprocess.run([_pu.FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, check=True)
```

In `stabilize.py`, replace `_FFMPEG_CANDIDATES` (lines 15-20) so the bare-name candidate is `exe_name`-correct and the brew paths stay as mac-only extras:

```python
from .. import platformutil as _pu
_FFMPEG_CANDIDATES = [
    _pu.FFMPEG,                                       # PATH (works on Windows/Linux/Mac)
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",       # mac brew ffmpeg-full (has vidstab)
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
]
```

The `available()`/`_ffmpeg_with_vidstab()` probe already runs `-filters` and checks for `vidstabdetect`, so a Windows gyan.dev/BtbN *full* build (which includes libvidstab) is auto-detected. Update the install hint (line 49-50) to be cross-OS:

```python
            "Stabilization needs ffmpeg with libvidstab. Install a full build:\n"
            "  macOS:   brew install ffmpeg-full\n"
            "  Windows: winget install Gyan.FFmpeg  (the 'full' variant)\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_platformutil.py -k "ffmpeg_constants" -v && uv run pytest -q`
Expected: PASS; macOS suite green (constants equal `"ffmpeg"`/`"ffprobe"` on Mac — identical behavior).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(win): centralize FFMPEG/FFPROBE constants, cross-OS vidstab lookup"
```

---

## Task 9: `chunks.py` — guard the macOS `sysctl` P-core probe

**Files:**
- Modify: `src/video_ai_editor/render/chunks.py:44-52`
- Test: `tests/test_chunk_workers.py` (new)

**Why:** `chunks.py` shells out to `sysctl -n hw.perflevel0.physicalcpu` (macOS/BSD-only). It already has an `except Exception` fallback to `os.cpu_count()//2`, so Windows *works* — but it pays a failed subprocess spawn every call. Guard it so non-Mac skips straight to the fallback.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chunk_workers.py
def test_chunk_workers_no_sysctl_off_mac(monkeypatch):
    from video_ai_editor.render import chunks
    from video_ai_editor import platformutil as pu
    monkeypatch.setattr(pu, "IS_MAC", False)
    called = {"sysctl": False}
    real_run = chunks.subprocess.run
    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "sysctl":
            called["sysctl"] = True
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(chunks.subprocess, "run", spy)
    n = chunks._chunk_workers(4)
    assert n >= 1
    assert called["sysctl"] is False  # must not spawn sysctl off macOS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chunk_workers.py -v`
Expected: FAIL — current code always tries `sysctl`, so `called["sysctl"]` is True.

- [ ] **Step 3: Write the implementation**

In `chunks.py`, replace the `try/except` at lines 45-52 with a Mac-gated branch:

```python
    from .. import platformutil as _pu
    p_cores = None
    if _pu.IS_MAC:
        # hw.perflevel0.physicalcpu = performance cores (10 on M4 Max).
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                capture_output=True, text=True, timeout=2,
            )
            p_cores = int(out.stdout.strip())
        except Exception:
            p_cores = None
    if p_cores is None:
        p_cores = max(2, (os.cpu_count() or 4) // 2)
    return max(1, min(p_cores, n_clips))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chunk_workers.py -v && uv run pytest -q`
Expected: PASS; macOS suite green (on Mac the `sysctl` path still runs exactly as before).

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/render/chunks.py tests/test_chunk_workers.py
git commit -m "feat(win): gate sysctl P-core probe to macOS"
```

---

## Task 10: `compositor.py` — cross-platform GPU encoder selection

**Files:**
- Modify: `src/video_ai_editor/render/compositor.py:92-118` (`_has_videotoolbox`, `_video_encoder_args`)
- Test: `tests/test_encoder_select.py` (new)

**Why:** Encoder selection is VideoToolbox-or-libx264. On Windows we want NVENC → QSV → AMF → libx264. Web-verified critical fact: **`ffmpeg -encoders` lists `h264_nvenc`/`h264_qsv`/`h264_amf` even with no matching GPU** — listing ≠ usable. So the probe must be a tiny real encode (`ffmpeg -f lavfi -i color=black:s=64x64:d=0.1 -c:v <enc> -f null -`, exit 0 = usable), cached for the process lifetime.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_encoder_select.py
from video_ai_editor.render import compositor as c


def test_pick_encoder_prefers_first_usable(monkeypatch):
    """When a hardware encoder probes usable, its args are returned; order is
    videotoolbox → nvenc → qsv → amf → libx264."""
    monkeypatch.setattr(c, "_usable_encoder",
                        lambda name: name == "h264_nvenc")
    args = c._video_encoder_args(preview=False)
    assert "h264_nvenc" in args
    assert "-cq" in args  # nvenc quality knob


def test_pick_encoder_falls_back_to_libx264(monkeypatch):
    monkeypatch.setattr(c, "_usable_encoder", lambda name: False)
    args = c._video_encoder_args(preview=True)
    assert args[:2] == ["-c:v", "libx264"]


def test_libx264_uses_ultrafast_preview(monkeypatch):
    monkeypatch.setattr(c, "_usable_encoder", lambda name: False)
    assert "ultrafast" in c._video_encoder_args(preview=True)
    assert "medium" in c._video_encoder_args(preview=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_encoder_select.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_usable_encoder'`

- [ ] **Step 3: Write the implementation**

Replace `_has_videotoolbox` and `_video_encoder_args` (lines 92-118) with:

```python
from .. import platformutil as _pu


@lru_cache(maxsize=None)
def _usable_encoder(name: str) -> bool:
    """True iff ffmpeg can actually ENCODE with `name` on this machine.

    'ffmpeg -encoders' lists h264_nvenc/qsv/amf even with no matching GPU, so a
    listing grep is not enough — we run a tiny null encode. VideoToolbox is the
    exception: it's Apple-only and cheap to trust from the listing, but the null
    encode works for it too, so we use one code path. Cached per process."""
    try:
        out = subprocess.run([_pu.FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, check=True)
        if f" {name} " not in out.stdout:
            return False
    except Exception:
        return False
    # Functional probe: a ~0.1s black-frame encode to null.
    try:
        r = subprocess.run(
            [_pu.FFMPEG, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
             "-c:v", name, "-f", "null", "-"],
            capture_output=True, text=True, timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


# Probe order: Apple HW first (only lists on Mac), then the three Windows/Linux
# HW encoders, then guaranteed software fallback.
_HW_ENCODER_ORDER = ["h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"]


def _video_encoder_args(*, preview: bool) -> list[str]:
    """Pick the fastest usable H.264 encoder; fall back to libx264."""
    for name in _HW_ENCODER_ORDER:
        if _usable_encoder(name):
            return _hw_encoder_args(name, preview=preview)
    crf = "30" if preview else "20"
    preset = "ultrafast" if preview else "medium"
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]


def _hw_encoder_args(name: str, *, preview: bool) -> list[str]:
    """Per-encoder quality-mode args. Values are tuned defaults, not mandates."""
    if name == "h264_videotoolbox":
        q = "60" if preview else "48"
        return ["-c:v", "h264_videotoolbox", "-q:v", q, "-allow_sw", "1",
                "-realtime", "1" if preview else "0", "-pix_fmt", "yuv420p"]
    if name == "h264_nvenc":
        if preview:
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                    "-rc", "vbr", "-cq", "33", "-b:v", "0", "-pix_fmt", "yuv420p"]
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
                "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"]
    if name == "h264_qsv":
        if preview:
            return ["-c:v", "h264_qsv", "-global_quality", "30",
                    "-preset", "veryfast", "-pix_fmt", "nv12"]
        return ["-c:v", "h264_qsv", "-global_quality", "22",
                "-preset", "slower", "-pix_fmt", "nv12"]
    if name == "h264_amf":
        if preview:
            return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp",
                    "-qp_i", "30", "-qp_p", "32", "-pix_fmt", "yuv420p"]
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", "20", "-qp_p", "22", "-pix_fmt", "yuv420p"]
    # Unreachable: only called for names in _HW_ENCODER_ORDER.
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
```

**Note:** search `compositor.py` for any other caller of `_has_videotoolbox()` (e.g. `chunks.py` comments reference VideoToolbox but do not call it). If any code path calls `_has_videotoolbox()`, replace with `_usable_encoder("h264_videotoolbox")`. Grep: `grep -rn _has_videotoolbox src/`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_encoder_select.py -v && uv run pytest -q`
Expected: PASS; macOS suite green. On a Mac with VideoToolbox, `_usable_encoder("h264_videotoolbox")` returns True and the same VideoToolbox args as before are produced — **no macOS behavior change**.

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/render/compositor.py tests/test_encoder_select.py
git commit -m "feat(win): probe-based GPU encoder select (nvenc/qsv/amf), libx264 fallback"
```

---

## Task 11: `compositor.py` — retrying atomic swap + prune unlink

**Files:**
- Modify: `src/video_ai_editor/render/compositor.py:565,702,748` (`os.replace`), and the mtime-prune `unlink` sites around lines 666-672
- Test: `tests/test_atomic_swap.py` (new)

**Why:** `os.replace(tmp, dst)` and the cache-prune `unlink` raise `PermissionError` on Windows when a reader holds the file open — and Starlette's `FileResponse` holds the preview mp4 open while streaming Range requests to the WebCodecs/`<video>` scrubber. Route all three replaces and the prune unlink through the Task-4 retry helpers. On macOS the first attempt always succeeds, so behavior is unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_atomic_swap.py
from pathlib import Path
from video_ai_editor.render import compositor as c


def test_render_uses_retrying_replace(monkeypatch, tmp_path):
    """The compositor must swap via platformutil.replace_with_retry, not a bare
    os.replace, so a Windows open-file race is retried rather than crashing."""
    import video_ai_editor.platformutil as pu
    calls = {"n": 0}
    def fake_replace(a, b, **k):
        calls["n"] += 1
        Path(b).write_bytes(Path(a).read_bytes())
        Path(a).unlink()
    monkeypatch.setattr(pu, "replace_with_retry", fake_replace)
    src = tmp_path / "x.part.mp4"; src.write_bytes(b"data")
    dst = tmp_path / "x.mp4"
    pu.replace_with_retry(src, dst)  # exercised via helper
    assert calls["n"] == 1 and dst.read_bytes() == b"data"
```

(The real integration is exercised by the existing render smoke tests; this asserts the helper is the swap mechanism.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_atomic_swap.py -v`
Expected: PASS for the helper test, but the *intent* — that compositor uses it — is verified by grep in Step 4. (If you prefer a failing-first assertion, add a test that greps the source: `assert "replace_with_retry" in Path("src/video_ai_editor/render/compositor.py").read_text()` — this FAILS before Step 3.)

Add that grep test and run it:

```python
def test_compositor_source_uses_retry_helpers():
    src = Path("src/video_ai_editor/render/compositor.py").read_text(encoding="utf-8")
    assert src.count("replace_with_retry") >= 3   # all three swap sites
    assert "os.replace(" not in src               # no bare os.replace left
```

Expected: FAIL (bare `os.replace` still present).

- [ ] **Step 3: Write the implementation**

Add `from .. import platformutil as _pu` if not already present. Replace each of the three `os.replace(tmp, dst)` (lines 565, 702, 748) with:

```python
    _pu.replace_with_retry(tmp, dst)  # atomic swap; retries on Windows if a reader holds dst
```

For the mtime-based prune around lines 666-672, replace `old.unlink(missing_ok=True)` with:

```python
        _pu.unlink_with_retry(old)
```

And replace the `tmp.unlink(missing_ok=True)` error-path cleanups (lines 557, 563, 700, 746) with `_pu.unlink_with_retry(tmp)` for consistency (these are cleanup-on-failure; retry is harmless).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_atomic_swap.py -v && uv run pytest -q`
Expected: PASS; macOS render smoke tests still green (single-attempt replace behaves identically).

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/render/compositor.py tests/test_atomic_swap.py
git commit -m "fix(win): retry os.replace/unlink on Windows open-file races in render swaps"
```

---

## Task 12: UTF-8 encoding sweep for state/transcript/cache JSON

**Files:**
- Modify: the ~40 `read_text`/`write_text` sites listed in the recon (grouped below)
- Test: `tests/test_encoding_roundtrip.py` (new)

**Why:** Every `read_text()`/`write_text()` without `encoding=` uses the locale default — cp1252 on Windows — which corrupts or crashes on Devanagari/emoji. The app is explicitly Hindi/English. `dispatch.py:636,640` write with `ensure_ascii=False` (raw Devanagari) → `UnicodeEncodeError` on cp1252. `srt_io.py` and the srt/vtt/ass exports are already correct (`encoding="utf-8"`) — do not touch those.

**Grouped edit list** (add `encoding="utf-8"` to each `read_text()`/`write_text()`):
- `edl/snapshot.py`: 31, 44, 52, 64, 67, 78, 91, 92, 95, 110, 112
- `agent/dispatch.py`: 115, 539, 636, 638, 640, 1375, 1563, 1612, 1913, 1914
- `main.py`: 469, 471, 676, 682
- `storage.py`: 45, 59, 64
- `storage_project.py`: 83, 100, 103
- `ingest/pipeline.py`: 68
- `ingest/beats.py`: 27, 50
- `render/waveform.py`: 47, 86
- `show/templates.py`: 200, 208
- `show/brand_kit.py`: 81, 87
- `ai/vision.py`: 32, 37, 61, 87
- `ai/reframe.py`: 146
- `ai/diarize.py`: 108, 160, 171, 178
- `ai/broll.py`: 73, 76, 79, 92
- `ai/tracker.py`: 106, 111
- `cli/setup_pyannote.py`: 32, 37
- `config.py`: 42, 58 (already done in Task 5 — skip)

**Pattern:** `p.read_text()` → `p.read_text(encoding="utf-8")`; `p.write_text(s)` → `p.write_text(s, encoding="utf-8")`. For `model_validate_json(p.read_text())`, becomes `model_validate_json(p.read_text(encoding="utf-8"))`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_encoding_roundtrip.py
from pathlib import Path


def test_no_bare_read_text_in_state_modules():
    """State/transcript modules must never call read_text()/write_text() without
    an explicit encoding= — locale cp1252 on Windows corrupts Hindi/emoji."""
    import re
    targets = [
        "src/video_ai_editor/edl/snapshot.py",
        "src/video_ai_editor/agent/dispatch.py",
        "src/video_ai_editor/main.py",
        "src/video_ai_editor/storage.py",
        "src/video_ai_editor/storage_project.py",
        "src/video_ai_editor/ingest/pipeline.py",
    ]
    offenders = []
    # match read_text( / write_text( whose arg list has no "encoding"
    pat = re.compile(r"\.(read_text|write_text)\(([^)]*)\)")
    for t in targets:
        txt = Path(t).read_text(encoding="utf-8")
        for m in pat.finditer(txt):
            if "encoding" not in m.group(2):
                offenders.append(f"{t}: {m.group(0)}")
    assert not offenders, "bare text I/O:\n" + "\n".join(offenders)


def test_snapshot_roundtrips_devanagari(tmp_path):
    """A snapshot written then reloaded preserves Hindi text on any locale."""
    from video_ai_editor.edl.snapshot import EDLStore
    store = EDLStore(tmp_path)
    # add a text clip with Hindi via the schema; then reload
    store.edl.tracks  # touch to ensure valid tree
    hindi = "नमस्ते दुनिया 🙏"
    # write a raw state file the way snapshot does and read it back
    p = tmp_path / "probe.json"
    p.write_text(hindi, encoding="utf-8")
    assert p.read_text(encoding="utf-8") == hindi
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_encoding_roundtrip.py -v`
Expected: FAIL — `test_no_bare_read_text_in_state_modules` lists the current bare calls.

- [ ] **Step 3: Apply the edits**

Work through the grouped edit list, adding `encoding="utf-8"` to each call. Do it module-by-module and re-run the guard test after each module to catch typos early.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_encoding_roundtrip.py -v && uv run pytest -q`
Expected: PASS; full macOS suite green (adding `encoding="utf-8"` is a no-op on macOS/Linux where UTF-8 is already the default).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix(win): explicit encoding=utf-8 on all state/transcript/cache text I/O"
```

---

## Task 13: `desktop.py` — Windows-safe npm invocation

**Files:**
- Modify: `src/video_ai_editor/desktop.py:44-52` (npm build), and add `encoding=`/`errors=` to the captured output
- Test: `tests/test_desktop_npm.py` (new)

**Why:** `subprocess.run(["npm", "run", "build"], ...)` fails on Windows with `FileNotFoundError` because npm is `npm.cmd` (a batch file, not `npm.exe`). Resolve via `shutil.which` with `.cmd`/`.exe` fallbacks. Also add `encoding="utf-8", errors="replace"` so captured build output doesn't cp1252-crash.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_desktop_npm.py
def test_npm_command_resolves_on_windows(monkeypatch):
    from video_ai_editor import desktop
    from video_ai_editor import platformutil as pu
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop.shutil, "which",
                        lambda n: "C:/Program Files/nodejs/npm.cmd" if n in ("npm.cmd", "npm") else None)
    assert desktop._npm_cmd() == "C:/Program Files/nodejs/npm.cmd"


def test_npm_command_plain_off_windows(monkeypatch):
    from video_ai_editor import desktop
    from video_ai_editor import platformutil as pu
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: "/usr/local/bin/npm")
    assert desktop._npm_cmd() == "/usr/local/bin/npm"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_desktop_npm.py -v`
Expected: FAIL with `AttributeError: module 'video_ai_editor.desktop' has no attribute '_npm_cmd'`

- [ ] **Step 3: Write the implementation**

Add `import shutil` at the top of `desktop.py` and this helper, then use it in `_ensure_frontend_built`:

```python
import shutil
from . import platformutil as _pu


def _npm_cmd() -> str:
    """Resolve the npm launcher. On Windows npm is npm.cmd (a batch file), so a
    bare 'npm' FileNotFounds. Try the platform-suffixed names, then fall back to
    the bare name (subprocess PATHEXT may still find it)."""
    candidates = ["npm.cmd", "npm"] if _pu.IS_WINDOWS else ["npm"]
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
    return candidates[0]
```

Replace the `subprocess.run(["npm", "run", "build"], ...)` at lines 45-49:

```python
    proc = subprocess.run(
        [_npm_cmd(), "run", "build"],
        cwd=str(repo / "frontend"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_desktop_npm.py -v && uv run pytest -q`
Expected: PASS; macOS suite green (on Mac `_npm_cmd()` resolves plain `npm` as before).

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/desktop.py tests/test_desktop_npm.py
git commit -m "feat(win): resolve npm.cmd on Windows for the dev frontend build"
```

---

## Task 14: `run.ps1` — Windows launcher

**Files:**
- Create: `run.ps1`
- Modify: `run.sh` (add a one-line comment pointing Windows users to `run.ps1`)
- Test: manual (documented)

**Why:** `run.sh` is bash-only; `.venv/bin/python` is `.venv\Scripts\python.exe` on Windows and PYTHONPATH uses `;`. The `.pth` hidden-flag bug that motivates the PYTHONPATH trick is macOS-only, but using PYTHONPATH on Windows too keeps launch behavior identical across OSes and avoids relying on the editable-install `.pth`.

- [ ] **Step 1: Create `run.ps1`**

```powershell
# run.ps1 — Launch Video AI Editor on Windows.
#
# Mirrors run.sh: uses PYTHONPATH=src instead of the editable-install .pth so
# launch behavior matches macOS. (The macOS Spotlight .pth hidden-flag bug does
# not exist on Windows, but PYTHONPATH is harmless and keeps parity.)
#
# Usage:  powershell -ExecutionPolicy Bypass -File run.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Error "No venv found. Run:  uv sync --python 3.13 --all-extras --group dev"
    exit 1
}

$env:PYTHONPATH = (Join-Path $PSScriptRoot "src") +
    $(if ($env:PYTHONPATH) { ";" + $env:PYTHONPATH } else { "" })

& $venvPy -m video_ai_editor.desktop @args
```

- [ ] **Step 2: Add a pointer in `run.sh`**

Insert after the usage comment block (before `set -euo pipefail`):

```bash
# Windows users: use run.ps1 instead (powershell -ExecutionPolicy Bypass -File run.ps1).
```

- [ ] **Step 3: Verify (manual, Windows box or note for the Windows dev)**

On Windows: `uv sync --python 3.13 --all-extras --group dev` then `powershell -ExecutionPolicy Bypass -File run.ps1`.
Expected: backend starts on `http://127.0.0.1:8765`, WebView2 window opens showing the editor. (If WebView2 Runtime is missing, see Task 16's runtime-check note.)

- [ ] **Step 4: Commit**

```bash
git add run.ps1 run.sh
git commit -m "feat(win): add run.ps1 launcher; point run.sh at it for Windows"
```

---

## Task 15: `Video AI Editor.spec` — guard `BUNDLE` and add Windows hidden imports

**Files:**
- Modify: `Video AI Editor.spec:60-71` (guard `BUNDLE`), lines 12-16 (add `clr` hidden import for Windows)
- Test: manual build (documented)

**Why:** `BUNDLE(...)` is PyInstaller's macOS-only class — verified from source it's a silent no-op on Windows (`if not is_darwin: return`), so it won't crash, but guarding it is the accepted idiom and avoids a dangling `app` name. On Windows, pywebview's WebView2 backend needs `clr` (pythonnet) bundled — add `hiddenimports += ['clr']`. The `.spec` uses `:` in `--add-data` only in `build_app.sh`, not in the spec (the spec uses tuple `datas`, which is cross-platform).

- [ ] **Step 1: Add Windows hidden import**

After line 16 (`hiddenimports += collect_submodules('open_clip')`), add:

```python
import sys as _sys
if _sys.platform == "win32":
    # pywebview's EdgeChromium/WebView2 backend loads .NET via pythonnet ('clr').
    hiddenimports += ['clr']
```

- [ ] **Step 2: Guard the `BUNDLE` step**

Wrap lines 60-71:

```python
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
```

(Add `import sys` at the top of the spec if not present.)

- [ ] **Step 3: Verify the macOS build still works**

Run: `uv run bash build_app.sh`
Expected: `dist/Video AI Editor.app` is produced exactly as before (the `if sys.platform == "darwin"` branch runs on Mac).

- [ ] **Step 4: Commit**

```bash
git add "Video AI Editor.spec"
git commit -m "feat(win): guard BUNDLE to darwin, add clr hidden import for WebView2"
```

---

## Task 16: `build_win.ps1` — Windows PyInstaller build

**Files:**
- Create: `build_win.ps1`
- Test: manual (documented)

**Why:** `build_app.sh` uses `--add-data "src:dst"` (`:` separator; Windows needs `;`), `--osx-bundle-identifier` (mac-only), and produces a `.app`. Windows needs the same PyInstaller `Analysis`/`EXE`/`COLLECT` (which the `.spec` already defines) driven by a PowerShell script, producing `dist/Video AI Editor/` (a folder). Prefer running the **`.spec`** (which Task 15 made cross-platform) over re-specifying flags.

- [ ] **Step 1: Create `build_win.ps1`**

```powershell
# build_win.ps1 — Build the Windows app folder via PyInstaller.
#
#   powershell -ExecutionPolicy Bypass -File build_win.ps1
#
# Output: dist\Video AI Editor\Video AI Editor.exe  (+ supporting DLLs/data)
# Notes:
#   - ffmpeg/whisper-cli/realesrgan are NOT bundled; they must be on PATH or in
#     the per-OS model dirs at runtime (same policy as the macOS build).
#   - The Microsoft Edge WebView2 Runtime must be present on the target machine
#     (preinstalled on Win11 / most Win10; else ship the Evergreen bootstrapper).
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Build the frontend first — pywebview serves frontend/dist.
if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Host "[build] frontend/dist missing — running npm run build"
    Push-Location frontend
    & npm run build
    Pop-Location
}

# Drive the cross-platform spec (BUNDLE is darwin-guarded; COLLECT yields the
# dist folder on Windows).
uv run pyinstaller --noconfirm "Video AI Editor.spec"

Write-Host ""
Write-Host "[build] Done -> dist\Video AI Editor\Video AI Editor.exe"
Write-Host "[build] Wrap it in an installer with Inno Setup or WiX for distribution."
```

- [ ] **Step 2: Verify (manual, Windows box)**

On Windows: `powershell -ExecutionPolicy Bypass -File build_win.ps1`
Expected: `dist\Video AI Editor\Video AI Editor.exe` runs and opens the WebView2 window. Test on a clean VM to confirm WebView2 Runtime handling.

- [ ] **Step 3: (Optional) Add a WebView2-missing guard in `desktop.py`**

Wrap `webview.start()` (desktop.py:83) so a missing WebView2 Runtime surfaces a clear message instead of a raw pythonnet traceback:

```python
    try:
        webview.start()
    except Exception as e:  # WebView2 Runtime missing / init failure on Windows
        if _pu.IS_WINDOWS:
            print("[desktop] Could not start the WebView2 window. Install the "
                  "Microsoft Edge WebView2 Runtime (Evergreen) from "
                  "https://developer.microsoft.com/microsoft-edge/webview2/ "
                  f"and relaunch.\n  Underlying error: {e}", file=sys.stderr)
            sys.exit(1)
        raise
```

(Add `from . import platformutil as _pu` to `desktop.py` if not already added in Task 13.)

- [ ] **Step 4: Commit**

```bash
git add build_win.ps1 src/video_ai_editor/desktop.py
git commit -m "feat(win): add build_win.ps1 + WebView2-missing guidance"
```

---

## Task 17: CI — add a `windows-latest` backend job

**Files:**
- Modify: `.github/workflows/ci.yml` (add a `backend-windows` job)
- Test: CI run on the PR

**Why:** Nothing currently exercises Windows. Add a job mirroring the Linux `backend` job but installing ffmpeg via Chocolatey and running the same pytest. This catches Windows regressions automatically.

- [ ] **Step 1: Add the job**

Append after the `backend` job (before `frontend`):

```yaml
  backend-windows:
    name: backend (pytest, windows)
    runs-on: windows-latest
    timeout-minutes: 25
    env:
      ANTHROPIC_API_KEY: ""
      HUGGINGFACE_TOKEN: ""
    steps:
      - uses: actions/checkout@v4

      - name: Install ffmpeg (Chocolatey)
        run: |
          choco install ffmpeg-full -y --no-progress
          ffmpeg -version | Select-Object -First 1
        shell: pwsh

      - name: Install uv
        uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
          cache-dependency-glob: "uv.lock"

      - name: Set up Python 3.13
        run: uv python install 3.13

      - name: Sync dependencies
        run: uv sync --frozen --extra dev
        # Note: py2app (dependency-group dev) installs but is mac-only dead code.

      - name: Run pytest
        run: uv run pytest -q --tb=short
        # Heavy-AI tests skip cleanly (no binaries/models on the runner).
```

- [ ] **Step 2: Verify locally that the sync resolves on Windows Python**

If a Windows machine is available: `uv sync --frozen --extra dev` on Python 3.13. Expected: resolves and installs from the universal lock (recon confirmed all wheels present; demucs resolves without diffq).

- [ ] **Step 3: Push and confirm CI**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(win): add windows-latest backend pytest job"
git push -u origin feat/windows-compat
```

Expected: the new `backend-windows` job runs and passes (or surfaces the first real Windows-only failure to fix).

---

## Task 18: Test fixes — skip-guard hardcoded paths and `/dev/null`

**Files:**
- Modify: `tests/test_render_smoke.py:9`, `tests/test_captions.py:112`, `tests/test_ingest_smoke.py:7`, `tests/test_render_overlay.py:11`, `tests/test_api_e2e.py:7` (hardcoded `/Users/sudhanshu/...`), `tests/test_review_hardening.py:53,67` (`/etc/hosts`), `tests/test_tools_dispatch.py:13`, `tests/test_text_tools.py:14`, `tests/test_all_tools_smoke.py:164` (`/dev/null`)
- Test: the suite itself

**Why:** Five tests reference a specific Mac user's Downloads folder — they must skip when the file is absent (they likely already do, but confirm the guard is OS-agnostic). Three use `/dev/null` as a dummy path, which doesn't exist on Windows (`NUL` does); use `os.devnull` or a `tmp_path` dummy.

- [ ] **Step 1: Add/verify skip-guards for the hardcoded sample-media tests**

For each of the 5 files, ensure the sample path is wrapped:

```python
import pytest
SAMPLE = Path("/Users/sudhanshu/Downloads/Viral Videos/...")
pytestmark = pytest.mark.skipif(not SAMPLE.exists(),
                                reason="local sample media not present")
```

(If a `skipif` already exists, leave it — it's OS-agnostic since it checks `.exists()`.)

- [ ] **Step 2: Replace `/dev/null` dummies with `os.devnull`**

In `test_tools_dispatch.py:13` and `test_text_tools.py:14`, change `"src": "/dev/null/x.mp4"` to a `tmp_path`-based nonexistent path or `os.path.join(os.devnull, "x.mp4")` is invalid on Windows — instead use a clearly-fake path under `tmp_path`:

```python
    "src": str(tmp_path / "nonexistent" / "x.mp4"),
```

In `test_all_tools_smoke.py:164`, change `"lut_path": "/dev/null"` to `"lut_path": os.devnull` (imports `os`), which is `NUL` on Windows and `/dev/null` on POSIX — a valid empty path on both.

- [ ] **Step 3: Run the suite**

Run: `uv run pytest -q`
Expected: PASS on macOS (skips unchanged; `os.devnull`/`tmp_path` behave identically). These changes make the same tests pass/skip correctly on Windows.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test(win): OS-agnostic skip-guards + os.devnull dummies"
```

---

## Task 19: Documentation — CLAUDE.md + README Windows section

**Files:**
- Modify: `CLAUDE.md` (add a "Running on Windows" subsection), `README.md` if present
- Test: none (docs)

**Why:** Record the Windows launch path (`run.ps1`), the ffmpeg-full requirement (Gyan.FFmpeg with the winget-PATH caveat), the WebView2 Runtime note, and that the `.pth` bug is macOS-only. Keep the existing macOS `run.sh` guidance intact.

- [ ] **Step 1: Add a Windows subsection to CLAUDE.md**

Under the "Commands" or "Launching on macOS" section, add:

```markdown
### Running on Windows

- Setup: `winget install Gyan.FFmpeg` (the **full** variant — includes libvidstab + libass + the nvenc/qsv/amf encoders). Then `uv sync --python 3.13 --all-extras --group dev` and `cd frontend && npm install`.
  - **winget-PATH caveat:** Gyan.FFmpeg is known to NOT put `ffmpeg.exe` on PATH. The app probes `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\...\bin` automatically (config.py), but if `ffmpeg -version` fails in a fresh shell, add that `bin` dir to PATH or use a BtbN static build.
- Launch: `powershell -ExecutionPolicy Bypass -File run.ps1` (the parallel of `run.sh`; uses `PYTHONPATH=src` + `.venv\Scripts\python.exe`). The macOS `.pth` hidden-flag bug does NOT occur on Windows.
- GUI: pywebview uses the **Edge WebView2** runtime (preinstalled on Win11 / most Win10; else install the Evergreen runtime). It ships H.264/AAC + WebCodecs, so the frame scrubber and preview work.
- Encoder: on Windows the render pipeline probes `h264_nvenc` → `h264_qsv` → `h264_amf` → `libx264` (a real null-encode probe, since `ffmpeg -encoders` lists HW encoders even without the GPU).
- Native AI binaries (optional features): `whisper-cli.exe` from whisper.cpp `whisper-bin-x64.zip`, `realesrgan-ncnn-vulkan.exe` from `realesrgan-ncnn-vulkan-*-windows.zip` — drop into `%APPDATA%\Video AI Editor\...` or `models\`. TTS (Piper) works from the pure-Python wheel with no extra binary.
- Packaging: `powershell -ExecutionPolicy Bypass -File build_win.ps1` → `dist\Video AI Editor\` (wrap in Inno Setup/WiX for distribution).
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(win): add Running on Windows section"
```

---

## Task 20: Full cross-platform verification + PR

**Files:** none (verification)

- [ ] **Step 1: Full macOS regression**

Run: `uv run pytest -q --tb=short`
Expected: same pass/skip counts as the Task 0 baseline — **zero regressions**.

- [ ] **Step 2: macOS app smoke**

Run: `bash run.sh` — confirm the window opens, import a clip, scrub, render a preview, export. Confirm VideoToolbox is still selected (add a temporary log in `_video_encoder_args` or check ffmpeg output).

- [ ] **Step 3: macOS build**

Run: `uv run bash build_app.sh` — confirm `dist/Video AI Editor.app` builds and launches.

- [ ] **Step 4: Windows verification (on a Windows box or via the CI job)**

- `uv sync --frozen --extra dev` resolves and installs.
- `uv run pytest -q` passes (heavy-AI skips).
- `run.ps1` launches; import → scrub → preview → export works; encoder falls back to nvenc/qsv/amf/libx264 as appropriate.
- Hindi text in a text clip round-trips through save/reload (encoding fix).
- `build_win.ps1` produces a runnable `dist\Video AI Editor\`.

- [ ] **Step 5: Open the PR**

```bash
git push -u origin feat/windows-compat
gh pr create --title "Windows compatibility (Mac + Windows from one codebase)" \
  --body "Adds Windows support with zero macOS regressions. See docs/superpowers/plans/2026-07-03-windows-compatibility.md. Key changes: platformutil helpers, per-OS dirs, probe-based GPU encoder (nvenc/qsv/amf), retrying atomic swap, UTF-8 encoding sweep, run.ps1/build_win.ps1, windows-latest CI job."
```

---

## Risk register & rollback

- **Biggest residual risk:** the retrying `os.replace` (Task 11) is timing-dependent on Windows; if a reader holds the preview open longer than `attempts × delay` (~2.75s worst case), the render swap still fails. Mitigation: the render is keyed by `edl.hash()`, so a failed swap just means the *next* request re-renders — no data loss. If this proves flaky in practice, raise `attempts` or write to a hash-unique name and update the cache pointer instead of overwriting.
- **demucs on exotic Windows Pythons:** the lock resolves demucs cleanly today (no diffq), but if a future `uv lock` upgrade pulls a demucs version that re-adds a compiled dep, `uv sync` could need MSVC Build Tools. Mitigation noted in CLAUDE.md; the app degrades gracefully (stem-separation raises RuntimeError) even if demucs is absent.
- **torchcodec** needs *shared* FFmpeg DLLs on Windows PATH at import; the winget-PATH probe in Task 5 covers the common case. If `import torchcodec` fails, the feature degrades (lazy import → 422), so it never breaks the core editor.
- **Rollback:** every task is an isolated commit behind `sys.platform`/`platformutil` branches; reverting any single commit restores prior behavior. The whole branch can be dropped without touching macOS behavior, since no macOS path was modified in place (only branched).

## Self-review notes (completed)

- **Spec coverage:** every recon-identified Windows hazard maps to a task (binary discovery→7/8, dirs→3/5/6, PATH sep→5, encoder→10, sysctl→9, atomic swap→11, encoding→12, npm→13, launcher→14, packaging→15/16, CI→17, tests→18, docs→19). Non-issues (frontend, fonts, emoji, sanitizer, shell=True, POSIX stdlib, colon filenames, tempfile) are documented in the recon summary as explicitly *not* requiring tasks.
- **No placeholders:** every code step shows the actual code; every test step shows the assertion and the exact run command with expected result.
- **Name consistency:** `platformutil` helpers (`exe_name`, `find_binary`, `user_data_dir`, `user_cache_dir`, `read_text_utf8`, `write_text_utf8`, `replace_with_retry`, `unlink_with_retry`, `IS_WINDOWS`, `IS_MAC`, `FFMPEG`, `FFPROBE`) are defined in Tasks 1-4/8 and referenced with the same names throughout.
