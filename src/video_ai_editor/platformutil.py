"""Cross-platform helpers. The ONE place OS differences live.

macOS and Windows both import from here; every OS-conditional decision in the
codebase should route through a function in this module rather than an inline
`sys.platform` check, so platform behavior stays auditable and testable.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Spread into every subprocess.run/Popen/check_output/check_call as
# `**_pu.SUBPROCESS_FLAGS`. On Windows, a windowed parent (frozen exe built
# with console=False, or pythonw) spawning a console child (ffmpeg/ffprobe/
# whisper-cli/...) pops up a visible terminal window for every task unless the
# call passes creationflags=subprocess.CREATE_NO_WINDOW. On macOS/Linux this is
# an empty dict, so the spread is a no-op and behavior is byte-identical.
#
# NOTE: the dict-spread raises TypeError if a call site ALSO passes its own
# creationflags= kwarg (duplicate keyword). No site does today — a future site
# that needs extra creation flags must drop the spread and OR the flag in
# manually: creationflags=subprocess.CREATE_NO_WINDOW | <extra> (guarded for
# Windows, since CREATE_NO_WINDOW only exists there).
# tests/test_subprocess_no_window.py statically enforces that every subprocess
# call site under src/video_ai_editor carries one of the two forms.
SUBPROCESS_FLAGS: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
)


def exe_name(name: str) -> str:
    """Append `.exe` on Windows for a bare binary name (idempotent)."""
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def _bundle_payload_dirs() -> list[Path]:
    """Directories a FROZEN PyInstaller build unpacks its collected payload to.

    Empty when running from source — which is what keeps everything below a
    strict widening: with no candidates, every resolver here degrades to the
    bare-name/PATH behaviour it has always had, byte for byte.

    macOS `--windowed` onedir (what `build_app.sh` produces): since PyInstaller
    6, `BUNDLE` relocates every BINARY entry into `Contents/Frameworks` and
    cross-links it into `Contents/Resources`, and the bootloader moves
    `sys._MEIPASS` along with them (PyInstaller `building/osx.py`, "we have
    effectively relocated the sys._MEIPASS directory from Contents/MacOS into
    Contents/Frameworks"). So a `--add-binary "ffmpeg:."` payload is a real
    file at `<_MEIPASS>/ffmpeg`, with a symlink at `../Resources/ffmpeg`.

    The other candidates are cheap (`is_file` on a handful of paths, once at
    import) and exist so a future packaging change cannot silently un-fix this:
    the Windows onedir layout puts the payload in `_internal/` next to the exe,
    and a onefile build unpacks it to a temp `_MEIPASS` that is not next to the
    executable at all.
    """
    if not getattr(sys, "frozen", False):
        return []
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        m = Path(meipass)
        cands += [m, m.parent / "Resources", m.parent / "Frameworks"]
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except OSError:                     # pragma: no cover - unreadable argv[0]
        exe_dir = None
    if exe_dir is not None:
        cands += [exe_dir, exe_dir / "_internal",
                  exe_dir.parent / "Frameworks", exe_dir.parent / "Resources"]
    out: list[Path] = []
    seen: set[str] = set()
    for d in cands:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


# Resolved once at import: `sys.frozen`/`sys._MEIPASS` never change during a
# run, and this is on the import path of every entry point.
BUNDLE_PAYLOAD_DIRS: list[Path] = _bundle_payload_dirs()


def bundled_binary(name: str) -> str | None:
    """Absolute path to `name` shipped INSIDE this frozen bundle, else None.

    Always None from source, so callers keep their existing behaviour there.
    """
    for d in BUNDLE_PAYLOAD_DIRS:
        cand = d / exe_name(name)
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:                 # pragma: no cover - unreadable dir
            continue
    return None


def bundled_bin_dir() -> Path | None:
    """Directory the bundled ffmpeg actually lives in, or None.

    `config.py` appends it to PATH so code that resolves a bare name itself
    (a third-party library, a subprocess that only inherits PATH) finds the
    shipped copy too.
    """
    ff = bundled_binary("ffmpeg")
    return Path(ff).parent if ff else None


def resolve_tool(name: str) -> str:
    """How every ffmpeg/ffprobe invocation in this codebase names its binary.

    Order: an explicit `VAI_FFMPEG` / `VAI_FFPROBE` override, then the copy
    bundled inside a frozen app (ABSOLUTE path), then the bare name for PATH
    to resolve — i.e. exactly what this module did before, once nothing is
    bundled.

    The bundled copy has to win, and it has to win *here*: `--add-binary`ing
    ffmpeg into the .app changes nothing on its own, because `FFMPEG` was a
    BARE NAME and `config._augment_path_for_gui_launch()` only ever added
    Homebrew/MacPorts directories to PATH — never the bundle's own. That gap
    is why a notarized DMG on a Mac with no Homebrew could not import,
    preview, thumbnail or export a single frame: the app launched fine and
    every ffmpeg call raised FileNotFoundError.
    """
    override = os.environ.get(f"VAI_{name.upper()}", "").strip()
    if override:
        return override
    return bundled_binary(name) or exe_name(name)


# Resolved once at import. From source these are still the bare names
# ("ffmpeg"/"ffprobe", or the .exe forms on Windows) resolved via PATH; in a
# frozen build they are absolute paths into the bundle.
FFMPEG = resolve_tool("ffmpeg")
FFPROBE = resolve_tool("ffprobe")


def ffmpeg_filter_path(path: Path | str) -> str:
    """Escape a filesystem path for embedding inside an ffmpeg *filtergraph*
    option value (e.g. `vidstabdetect=result=<here>`, `sendcmd=f=<here>`,
    `movie=filename=<here>`).

    This is NOT the same as passing a path as an ffmpeg `-i` argv element (that
    needs no escaping). Inside a filtergraph, `:` separates filter options and
    `\\` is an escape char, so a raw Windows path like `C:\\Users\\x\\a.trf`
    is mangled by the parser. The robust, empirically-verified form is:
      1. Convert `\\` to `/` — ffmpeg accepts forward slashes on Windows, which
         removes every backslash-as-escape hazard.
      2. Escape each remaining `:` (the drive-letter colon) as `\\\\:` — the
         only escaping that survives ffmpeg's two-pass filtergraph parser
         (single-backslash and single-quoting both fail).
    On POSIX a normal path has no backslashes and no colon, so it passes
    through unchanged (a rare stray colon is still escaped defensively)."""
    s = str(path).replace("\\", "/")
    return s.replace(":", "\\\\:")


def find_binary(name: str, extra_dirs: list[Path]) -> str | None:
    """Locate a native binary cross-platform.

    0. A copy shipped inside a frozen bundle (nothing there from source, so
       this step is a no-op on the dev path).
    1. `shutil.which(exe_name(name))` — respects PATH, adds `.exe` on Windows.
    2. Each dir in `extra_dirs` (both `name` and `exe_name(name)`).
    Returns the resolved path string, or None if nowhere found.
    """
    bundled = bundled_binary(name)
    if bundled:
        return bundled
    found = shutil.which(exe_name(name))
    if found:
        return found
    for d in extra_dirs:
        for cand in (Path(d) / exe_name(name), Path(d) / name):
            if cand.exists():
                return str(cand)
    return None


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


def read_text_utf8(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def read_text_config(path: Path | str) -> str:
    """Read a small hand-editable config file, tolerating a UTF-8 BOM.

    For files a WINDOWS user or a Windows build script may have written:
    `.env`, `VERSION`, `BUILD_ID`. Notepad and PowerShell 5.1's
    `Set-Content -Encoding utf8` both prepend a BOM (`EF BB BF`), and neither
    `read_text(encoding="utf-8")` nor `.strip()` removes it — a BOM is not
    whitespace. The leading `\\ufeff` then lands *inside* the first value:

      - `.env` -> the first key parses as `\\ufeffANTHROPIC_API_KEY`, so the real
        key is never set and the app reports it missing while the file plainly
        contains it (chat pane silently disabled).
      - `BUILD_ID` -> the version badge and `/api/version` report a sha that is
        not byte-equal to any git object, defeating the release-identity
        mechanism whose whole purpose is making "which build?" answerable.

    `utf-8-sig` decodes BOM-less UTF-8 byte-for-byte identically, so this is
    strictly more tolerant than `read_text_utf8` — never different. It is a
    separate helper rather than a change to `read_text_utf8` because that one is
    used for media/EDL/sidecar reads where silently eating a leading `\\ufeff`
    would be a content change, not a fix.
    """
    return Path(path).read_text(encoding="utf-8-sig")


def write_text_utf8(path: Path | str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def part_path(dst: Path) -> Path:
    """A unique sibling temp path for an in-progress write of `dst`.

    ffmpeg's `-y` truncates its output to 0 bytes and writes progressively, so
    pointing it straight at the final path means a concurrent reader — or a
    process killed mid-write — sees a torn file. Worse for content-addressed
    caches: the final name IS the cache key, so a truncated file becomes a
    permanently-valid-looking cache hit. Write here, then swap in with
    `replace_with_retry`.

    ffmpeg terminated by SIGTERM (or by a Windows console close, which its
    CtrlHandler maps to SIGTERM) flushes and writes a COMPLETE trailer, so the
    partial file is fully decodable and no "is it valid?" check can spot it —
    staging is the only reliable defence.

    PID + thread id keep concurrent writers of the same destination from
    clobbering each other. The suffix MUST be preserved: ffmpeg picks its muxer
    from the output path's extension, so a `.mov` staged as `.part.mp4` would be
    muxed as MP4 and then merely renamed.
    """
    return dst.with_name(
        f".{dst.stem}.{os.getpid()}.{threading.get_ident()}.part{dst.suffix}")


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


def rmtree_with_retry(path: Path | str,
                      attempts: int = 10, delay: float = 0.1) -> None:
    """shutil.rmtree with retry/backoff for Windows mandatory file locking.

    On Windows, a directory containing a file with any open handle (e.g. a
    Starlette FileResponse still streaming a previews/*.mp4 or exports/*.mp4,
    an in-flight render's *.part.mp4, or a lingering AV/indexer scan) cannot
    be deleted — shutil.rmtree(ignore_errors=False) raises PermissionError/
    OSError partway through, leaving the tree partially deleted. A short
    backoff lets the other handle-holder finish, mirroring
    replace_with_retry/unlink_with_retry above. On POSIX an open file can be
    unlinked while still held open, so rmtree normally succeeds on the first
    try there regardless."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except FileNotFoundError:
            # Already gone (e.g. a partial previous rmtree finished the job,
            # or a concurrent delete raced us) — nothing left to remove.
            return
        except (PermissionError, OSError) as e:  # pragma: no cover - Windows-timing path
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]
